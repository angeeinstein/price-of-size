"""Crawl scheduling: which fetch jobs a node gets, and when it may send them.

Rate limits are enforced here, centrally, per (domain, egress group): every
fetch job is handed out with a relative `delay_s`, its slot in that domain's
schedule. Nodes behind the same public IP share one schedule, so extra nodes
at one site add CPU but not request rate; nodes at other sites get their own
budget (setting `rate_limit_scope`). An optional hard cap per domain
(`max_total_rate_per_domain`) adds a second schedule across all groups.

Delays are relative, so node clocks do not matter. A slot is consumed when the
job is handed out; if the node then disappears the slot is simply wasted,
which errs on the polite side.

Before the first page of a domain, its robots.txt is fetched (as a job of its
own, through the same schedule). With `respect_robots_txt` on, disallowed URLs
fail without being fetched.
"""

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator import runtime_settings
from pos_coordinator.crawl import ratecontrol, robots, urls
from pos_coordinator.models import DomainEgressState, DomainState, Job, Node
from pos_shared.ids import uuid7

log = logging.getLogger(__name__)

FETCH_TYPES = ("fetch_http", "fetch_robots", "fetch_sitemap")
PAGE_TYPES = ("fetch_http", "fetch_sitemap")  # subject to robots.txt rules
LOOKAHEAD_S = 60.0
JITTER = 0.2  # slots are spaced interval * U(1 - JITTER, 1 + JITTER)
ROBOTS_PRIORITY = 100
MAX_DOMAINS_PER_LEASE = 50
# Slow start: until a domain has answered this many pages, at most one fetch for it is
# in flight, so a CAPTCHA or a 429 on first contact stops everything at once.
SLOW_START_PAGES = 10
MAX_BATCH_PER_DOMAIN = 10  # jobs of one domain per lease call, at most
ROBOTS_SKIP_LOOKAHEAD = 20
INSERT_CHUNK = 5000  # rows per statement (bind parameters are limited to 65,535)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


@dataclass(frozen=True)
class CrawlConfig:
    respect_robots_txt: bool
    rate_limit_scope: str
    base_rate_rps: float
    max_rate_rps: float
    max_total_rate_per_domain: float | None
    user_agent: str
    lease_s: int
    keep_html_first_pages: int
    keep_html_sample: float
    html_quota_ok: bool
    block: dict[str, Any] = field(default_factory=dict)  # block_* settings

    @classmethod
    def from_settings(cls, cfg: dict[str, Any], html_quota_ok: bool) -> "CrawlConfig":
        return cls(
            respect_robots_txt=bool(cfg["respect_robots_txt"]),
            rate_limit_scope=cfg["rate_limit_scope"],
            base_rate_rps=float(cfg["base_rate_rps"]),
            max_rate_rps=float(cfg["max_rate_rps"]),
            max_total_rate_per_domain=cfg["max_total_rate_per_domain"],
            user_agent=cfg["user_agent"],
            lease_s=int(cfg["job_lease_s"]),
            keep_html_first_pages=int(cfg["html_keep_first_pages"]),
            keep_html_sample=float(cfg["html_keep_sample_fraction"]),
            html_quota_ok=html_quota_ok,
            block={
                k: cfg.get(k, runtime_settings.DEFAULTS[k]) for k in runtime_settings.BLOCK_KEYS
            },
        )

    def block_setting(self, overrides: dict[str, Any] | None, key: str) -> Any:
        """A block_* setting, a domain's own override first."""
        o = overrides or {}
        return o[key] if key in o else self.block.get(key, runtime_settings.DEFAULTS[key])

    def block_policy(self, overrides: dict[str, Any] | None) -> ratecontrol.BlockPolicy:
        return ratecontrol.BlockPolicy(
            challenge_run=int(self.block_setting(overrides, "block_challenge_run")),
            suspect_pause_s=float(self.block_setting(overrides, "block_suspect_pause_s")),
            forbidden_run=int(self.block_setting(overrides, "block_forbidden_run")),
        )


@dataclass
class Handout:
    job: Job
    delay_s: float
    extra: dict[str, Any]


def egress_group(node: Node, cfg: CrawlConfig) -> str:
    if cfg.rate_limit_scope == "global":
        return "global"
    return node.egress_group or f"node:{node.id}"


# --- enqueueing --------------------------------------------------------------------


@dataclass
class EnqueueReport:
    queued: int = 0
    duplicates: int = 0
    invalid: list[str] | None = None


async def enqueue_urls(
    session: AsyncSession,
    raw_urls: list[str],
    source: str | None,
    priority: int = 0,
    job_type: str = "fetch_http",
    extra_payload: dict[str, Any] | None = None,
    skip_done_within: timedelta | None = None,
) -> EnqueueReport:
    """Queue fetch jobs, once per normalised URL. `skip_done_within` also skips URLs
    fetched successfully that recently (sitemap cycles, repeated discovery)."""
    report = EnqueueReport(invalid=[])
    rows, domains, seen = [], {}, set()
    for raw in raw_urls:
        try:
            url = urls.normalize(raw)
        except urls.BadUrl:
            report.invalid.append(raw)
            continue
        key = urls.dedup_key(url)
        if job_type != "fetch_http":  # e.g. a sitemap and a page at the same URL
            key = f"{job_type}:{key}"
        if key in seen:
            report.duplicates += 1
            continue
        seen.add(key)
        domain = urls.domain_of(url)
        domains.setdefault(domain, url)
        rows.append(
            {
                "id": uuid7(),
                "type": job_type,
                "payload": {**(extra_payload or {}), "url": url, "source": source},
                "priority": priority,
                "domain": domain,
                "dedup_key": key,
            }
        )
    if rows and skip_done_within is not None:
        recent: set[str] = set()
        for part in _chunks(rows, INSERT_CHUNK):
            recent |= set(
                (
                    await session.execute(
                        select(Job.dedup_key).where(
                            Job.dedup_key.in_([r["dedup_key"] for r in part]),
                            Job.status == "done",
                            Job.finished_at > func.now() - skip_done_within,
                        )
                    )
                ).scalars()
            )
        report.duplicates += sum(1 for r in rows if r["dedup_key"] in recent)
        rows = [r for r in rows if r["dedup_key"] not in recent]
    if not rows:
        return report
    await ensure_domains(session, domains)
    inserted = 0
    # PostgreSQL takes at most 65,535 bind parameters per statement (6 per row here):
    # a 50,000-URL sitemap is inserted in parts.
    for part in _chunks(rows, INSERT_CHUNK):
        stmt = (
            pg_insert(Job)
            .values(part)
            .on_conflict_do_nothing(
                index_elements=["dedup_key"],
                index_where=text("status IN ('queued', 'leased') AND dedup_key IS NOT NULL"),
            )
            .returning(Job.id)
        )
        inserted += len((await session.execute(stmt)).scalars().all())
    report.queued = inserted
    report.duplicates += len(rows) - inserted
    return report


async def ensure_domains(session: AsyncSession, domains: dict[str, str]) -> None:
    """Create domain rows (and their robots.txt jobs) for new domains."""
    if not domains:
        return
    names = sorted(domains)
    unknown: set[str] = set()
    for part in _chunks(names, INSERT_CHUNK):
        await session.execute(
            pg_insert(DomainState)
            .values([{"domain": d} for d in part])
            .on_conflict_do_nothing(index_elements=["domain"])
        )
        # Only domains whose rules are unknown need robots.txt now; known ones are
        # refreshed by the scheduler when robots_next_check_at comes due.
        unknown |= set(
            (
                await session.execute(
                    select(DomainState.domain).where(
                        DomainState.domain.in_(part), DomainState.robots_status.is_(None)
                    )
                )
            ).scalars()
        )
    for domain, sample_url in domains.items():
        if domain in unknown:
            await ensure_robots_job(session, domain, urls.robots_url(sample_url))


async def ensure_robots_job(session: AsyncSession, domain: str, robots_url: str) -> None:
    await session.execute(
        pg_insert(Job)
        .values(
            id=uuid7(),
            type="fetch_robots",
            payload={"url": robots_url, "source": "robots"},
            priority=ROBOTS_PRIORITY,
            domain=domain,
            dedup_key=f"robots:{domain}",
            max_attempts=10,
        )
        .on_conflict_do_nothing(
            index_elements=["dedup_key"],
            index_where=text("status IN ('queued', 'leased') AND dedup_key IS NOT NULL"),
        )
    )


# --- leasing -----------------------------------------------------------------------


async def lease_fetch(
    session: AsyncSession, node: Node, job_types: list[str], max_jobs: int, cfg: CrawlConfig
) -> list[Handout]:
    types = [t for t in job_types if t in FETCH_TYPES]
    if not types or max_jobs <= 0:
        return []
    group = egress_group(node, cfg)
    now: datetime = (await session.execute(select(func.now()))).scalar_one()
    horizon = now + timedelta(seconds=LOOKAHEAD_S)

    # Domains that cannot be served right now (robots.txt pending, first fetch in
    # flight) are pushed back a little in _lease_domain, so they never hide ready ones;
    # and more candidates than needed are looked at.
    candidates = (
        (
            await session.execute(
                text(
                    """
                SELECT d.domain
                FROM domain_state d
                LEFT JOIN domain_egress_state e
                       ON e.domain = d.domain AND e.egress_group = :g
                WHERE d.block_status <> 'blocked'
                  AND (e.cooldown_until IS NULL OR e.cooldown_until <= :now)
                  AND (e.next_slot_at IS NULL OR e.next_slot_at <= :horizon)
                  AND EXISTS (
                      SELECT 1 FROM job j
                      WHERE j.domain = d.domain AND j.status = 'queued'
                        AND j.type = ANY(:types) AND j.not_before <= :now)
                  -- servable now: usable robots.txt rules, or a robots.txt fetch to hand
                  -- out (or to create, when due); domains waiting for one are skipped
                  AND (
                      d.robots_status IN ('rules', 'none') OR NOT :respect_robots
                      OR EXISTS (
                          SELECT 1 FROM job r
                          WHERE r.domain = d.domain AND r.type = 'fetch_robots'
                            AND r.status = 'queued' AND r.not_before <= :now
                            AND 'fetch_robots' = ANY(:types))
                      OR (
                          (d.robots_next_check_at IS NULL OR d.robots_next_check_at <= :now)
                          AND NOT EXISTS (
                              SELECT 1 FROM job r
                              WHERE r.domain = d.domain AND r.type = 'fetch_robots'
                                AND r.status IN ('queued', 'leased'))))
                  -- slow start: a new domain with its first fetch in flight is not ready
                  AND NOT (
                      d.pages_fetched < :slow_start
                      AND EXISTS (
                          SELECT 1 FROM job f
                          WHERE f.domain = d.domain AND f.status = 'leased'
                            AND f.type = ANY(:fetch_types)))
                ORDER BY e.next_slot_at NULLS FIRST, d.domain
                LIMIT :n
                """
                ),
                {
                    "g": group,
                    "now": now,
                    "horizon": horizon,
                    "types": types,
                    "n": min(max_jobs * 2 + 8, MAX_DOMAINS_PER_LEASE),
                    "slow_start": SLOW_START_PAGES,
                    "respect_robots": cfg.respect_robots_txt,
                    "fetch_types": list(FETCH_TYPES),
                },
            )
        )
        .scalars()
        .all()
    )

    handouts: list[Handout] = []
    for domain in candidates:
        if len(handouts) >= max_jobs:
            break
        handouts += await _lease_domain(
            session, node, domain, group, types, max_jobs - len(handouts), now, horizon, cfg
        )
    return handouts


async def _lock_group_state(
    session: AsyncSession, domain: str, group: str, cfg: CrawlConfig
) -> DomainEgressState | None:
    await session.execute(
        pg_insert(DomainEgressState)
        .values(domain=domain, egress_group=group, rate_rps=cfg.base_rate_rps)
        .on_conflict_do_nothing(index_elements=["domain", "egress_group"])
    )
    return (
        await session.execute(
            select(DomainEgressState)
            .where(DomainEgressState.domain == domain, DomainEgressState.egress_group == group)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _lease_domain(
    session: AsyncSession,
    node: Node,
    domain: str,
    group: str,
    types: list[str],
    room: int,
    now: datetime,
    horizon: datetime,
    cfg: CrawlConfig,
) -> list[Handout]:
    # Lock order, everywhere: domain_state, then domain_egress_state (see feedback.apply).
    # The domain row is taken with SKIP LOCKED: if a result or another lease is busy with
    # this domain right now, it is simply served in the next round.
    ds = (
        await session.execute(
            select(DomainState)
            .where(DomainState.domain == domain)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if ds is None:
        log.debug("%s: domain row busy", domain)
        return []
    eg = await _lock_group_state(session, domain, group, cfg)
    if eg is None:  # cannot happen while we hold the domain row; be safe
        log.debug("%s: group state locked by another lease", domain)
        return []
    if eg.cooldown_until is not None and eg.cooldown_until > now:
        log.debug("%s: cooling down until %s", domain, eg.cooldown_until)
        return []
    if ds.block_status == "blocked":
        log.debug("%s: blocked", domain)
        return []

    # Pages only with usable rules: "unreachable" (5xx/network) means disallow-all
    # until robots.txt can be fetched again (RFC 9309), so pages wait for it.
    # Without robots.txt enforcement (an operator's decision) pages need no rules; robots
    # .txt is still fetched and stored.
    rules_usable = ds.robots_status in ("rules", "none") or not cfg.respect_robots_txt
    # Refetched when due: after 24 h, or an hour after it was unreachable (not on
    # every lease: a failing robots.txt must not be hammered).
    due = ds.robots_next_check_at is None or ds.robots_next_check_at <= now
    if due:
        sample = (
            await session.execute(
                select(Job.payload["url"].astext)
                .where(Job.domain == domain, Job.type.in_(PAGE_TYPES), Job.status == "queued")
                .limit(1)
            )
        ).scalar_one_or_none()
        if sample:  # idempotent: at most one active robots job per domain
            await ensure_robots_job(session, domain, urls.robots_url(sample))
    allowed_types = types if rules_usable else [t for t in types if t == "fetch_robots"]
    has_allowed = (
        await session.execute(
            select(Job.id)
            .where(
                Job.domain == domain,
                Job.status == "queued",
                Job.type.in_(allowed_types),
                Job.not_before <= now,
            )
            .limit(1)
        )
    ).first()
    if not allowed_types or not has_allowed:
        log.debug("%s: waiting for robots.txt (%s)", domain, ds.robots_status)
        _push_back(eg, now)
        return []

    override = (ds.overrides or {}).get("rate_rps")
    # robots.txt rules and delays for the identity we actually send (`user_agent`)
    crawl_delay = robots.crawl_delay(ds.robots_status, ds.robots_txt, cfg.user_agent)
    top = ratecontrol.ceiling(cfg.max_rate_rps, crawl_delay, override)
    rate = ratecontrol.clamp(eg.rate_rps, top)  # the floor never beats crawl-delay
    interval = 1.0 / rate
    # The domain as a whole (all egress groups): robots.txt's crawl-delay and an
    # operator's override are the site's wishes, not per-IP budgets; the optional
    # global cap too. One schedule across groups enforces them.
    cap = cfg.max_total_rate_per_domain
    domain_top = ratecontrol.ceiling(cap or float("inf"), crawl_delay, override)
    global_gap = 1.0 / domain_top if domain_top != float("inf") else 0.0
    t_group = max(now, eg.next_slot_at or now)
    t_global = max(now, ds.next_global_slot_at or now) if global_gap else now

    slots = int((horizon - max(t_group, t_global)).total_seconds() / interval) + 1
    want = max(0, min(room, slots, MAX_BATCH_PER_DOMAIN))
    if ds.pages_fetched < SLOW_START_PAGES:
        in_flight = (
            await session.execute(
                select(func.count()).where(
                    Job.domain == domain, Job.status == "leased", Job.type.in_(FETCH_TYPES)
                )
            )
        ).scalar_one()
        want = 0 if in_flight else min(want, 1)
    if want == 0:
        log.debug("%s: no slot (slow start in flight or schedule full)", domain)
        _push_back(eg, now)
        return []
    jobs = (
        (
            await session.execute(
                select(Job)
                .where(
                    Job.domain == domain,
                    Job.status == "queued",
                    Job.type.in_(allowed_types),
                    Job.not_before <= now,
                )
                .order_by(Job.priority.desc(), Job.id)
                # look a little further: robots-disallowed URLs must not use up the batch
                .limit(want + ROBOTS_SKIP_LOOKAHEAD)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )

    out: list[Handout] = []
    for job in jobs:
        if len(out) >= want:
            break
        url = job.payload.get("url", "")
        if (
            job.type in PAGE_TYPES
            and cfg.respect_robots_txt
            and not robots.allowed(ds.robots_status, ds.robots_txt, url, cfg.user_agent)
        ):
            job.status = "failed"
            job.last_error = "robots_disallowed"
            job.finished_at = func.now()
            job.updated_at = func.now()
            continue
        slot = max(t_group, t_global)
        if slot > horizon:
            break
        delay = (slot - now).total_seconds()
        t_group = slot + timedelta(seconds=interval * random.uniform(1 - JITTER, 1 + JITTER))
        if global_gap:
            t_global = slot + timedelta(seconds=global_gap)
        job.status = "leased"
        job.node_id = node.id
        job.attempts = job.attempts + 1
        job.lease_expires_at = now + timedelta(seconds=cfg.lease_s + delay)
        job.updated_at = func.now()
        extra: dict[str, Any] = {
            "delay_s": round(delay, 3),
            # the worker keeps its own requests to the domain at least this far apart
            "interval_s": round(max(interval * (1 - JITTER), global_gap), 3),
            "user_agent": cfg.user_agent,
            # how long the worker holds back its other jobs for the domain after a
            # challenge page: the coordinator's own first pause (block_suspect_pause_s)
            "refusal_pause_s": cfg.block_policy(ds.overrides).suspect_pause_s,
        }
        if ds.unblocked_at is not None:
            # a refusal pause the worker took before this unblock no longer holds
            extra["unblocked_at"] = ds.unblocked_at.timestamp()
        if job.type == "fetch_http":
            extra["keep_html"] = _keep_html(ds, cfg)
        out.append(Handout(job=job, delay_s=delay, extra=extra))

    eg.next_slot_at = t_group
    eg.updated_at = func.now()
    if global_gap:
        ds.next_global_slot_at = t_global
    await session.flush()
    return out


PUSH_BACK_S = 3.0  # enough to move behind ready domains; a first fetch takes seconds


def _push_back(eg: DomainEgressState, now: datetime) -> None:
    """A domain that cannot be served now moves back in the candidate order (its slot
    never advances otherwise, and it would come first in every lease)."""
    later = now + timedelta(seconds=PUSH_BACK_S)
    if eg.next_slot_at is None or eg.next_slot_at < later:
        eg.next_slot_at = later
        eg.updated_at = func.now()


def _keep_html(ds: DomainState, cfg: CrawlConfig) -> bool:
    """HTML retention: the first pages of every domain and a random sample always;
    everything else while the archive is below its quota."""
    if ds.pages_fetched < cfg.keep_html_first_pages:
        return True
    if random.random() < cfg.keep_html_sample:
        return True
    return cfg.html_quota_ok
