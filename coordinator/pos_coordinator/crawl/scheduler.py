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

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator.crawl import ratecontrol, robots, urls
from pos_coordinator.models import DomainEgressState, DomainState, Job, Node
from pos_shared.ids import uuid7

FETCH_TYPES = ("fetch_http", "fetch_robots")
LOOKAHEAD_S = 60.0
JITTER = 0.2  # slots are spaced interval * U(1 - JITTER, 1 + JITTER)
ROBOTS_PRIORITY = 100
MAX_DOMAINS_PER_LEASE = 50
# Slow start: until a domain has answered this many pages, at most one fetch for it is
# in flight, so a CAPTCHA or a 429 on first contact stops everything at once.
SLOW_START_PAGES = 10
MAX_BATCH_PER_DOMAIN = 10  # jobs of one domain per lease call, at most
ROBOTS_SKIP_LOOKAHEAD = 20


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
    session: AsyncSession, raw_urls: list[str], source: str | None, priority: int = 0
) -> EnqueueReport:
    report = EnqueueReport(invalid=[])
    rows, domains, seen = [], {}, set()
    for raw in raw_urls:
        try:
            url = urls.normalize(raw)
        except urls.BadUrl:
            report.invalid.append(raw)
            continue
        key = urls.dedup_key(url)
        if key in seen:
            report.duplicates += 1
            continue
        seen.add(key)
        domain = urls.domain_of(url)
        domains.setdefault(domain, url)
        rows.append(
            {
                "id": uuid7(),
                "type": "fetch_http",
                "payload": {"url": url, "source": source},
                "priority": priority,
                "domain": domain,
                "dedup_key": key,
            }
        )
    if not rows:
        return report
    await ensure_domains(session, domains)
    stmt = (
        pg_insert(Job)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=["dedup_key"],
            index_where=text("status IN ('queued', 'leased') AND dedup_key IS NOT NULL"),
        )
        .returning(Job.id)
    )
    inserted = len((await session.execute(stmt)).scalars().all())
    report.queued = inserted
    report.duplicates += len(rows) - inserted
    return report


async def ensure_domains(session: AsyncSession, domains: dict[str, str]) -> None:
    """Create domain rows (and their robots.txt jobs) for new domains."""
    if not domains:
        return
    await session.execute(
        pg_insert(DomainState)
        .values([{"domain": d} for d in domains])
        .on_conflict_do_nothing(index_elements=["domain"])
    )
    for domain, sample_url in domains.items():
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
                ORDER BY e.next_slot_at NULLS FIRST, d.domain
                LIMIT :n
                """
                ),
                {
                    "g": group,
                    "now": now,
                    "horizon": horizon,
                    "types": types,
                    "n": min(max_jobs, MAX_DOMAINS_PER_LEASE),
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
    eg = await _lock_group_state(session, domain, group, cfg)
    if eg is None:  # another node of this group is scheduling the domain right now
        return []
    if eg.cooldown_until is not None and eg.cooldown_until > now:
        return []
    ds = (
        await session.execute(
            select(DomainState)
            .where(DomainState.domain == domain)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if ds.block_status == "blocked":
        return []

    # Pages only with usable rules: "unreachable" (5xx/network) means disallow-all
    # until robots.txt can be fetched again (RFC 9309), so pages wait for it.
    rules_usable = ds.robots_status in ("rules", "none")
    due = ds.robots_next_check_at is None or ds.robots_next_check_at <= now
    if not rules_usable or due:
        sample = (
            await session.execute(
                select(Job.payload["url"].astext)
                .where(Job.domain == domain, Job.type == "fetch_http", Job.status == "queued")
                .limit(1)
            )
        ).scalar_one_or_none()
        if sample:  # idempotent: at most one active robots job per domain
            await ensure_robots_job(session, domain, urls.robots_url(sample))
    allowed_types = types if rules_usable else [t for t in types if t == "fetch_robots"]
    if not allowed_types:
        return []

    override = (ds.overrides or {}).get("rate_rps")
    top = ratecontrol.ceiling(cfg.max_rate_rps, ds.robots_crawl_delay_s, override)
    rate = max(min(eg.rate_rps, top), ratecontrol.FLOOR_RPS)
    interval = 1.0 / rate
    cap = cfg.max_total_rate_per_domain
    t_group = max(now, eg.next_slot_at or now)
    t_global = max(now, ds.next_global_slot_at or now) if cap else now

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
            job.type == "fetch_http"
            and cfg.respect_robots_txt
            and not robots.allowed(ds.robots_status, ds.robots_txt, url)
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
        if cap:
            t_global = slot + timedelta(seconds=1.0 / cap)
        job.status = "leased"
        job.node_id = node.id
        job.attempts = job.attempts + 1
        job.lease_expires_at = now + timedelta(seconds=cfg.lease_s + delay)
        job.updated_at = func.now()
        extra: dict[str, Any] = {"delay_s": round(delay, 3), "user_agent": cfg.user_agent}
        if job.type == "fetch_http":
            extra["keep_html"] = _keep_html(ds, cfg)
        out.append(Handout(job=job, delay_s=delay, extra=extra))

    eg.next_slot_at = t_group
    eg.updated_at = func.now()
    if cap:
        ds.next_global_slot_at = t_global
    await session.flush()
    return out


def _keep_html(ds: DomainState, cfg: CrawlConfig) -> bool:
    """HTML retention: the first pages of every domain and a random sample always;
    everything else while the archive is below its quota."""
    if ds.pages_fetched < cfg.keep_html_first_pages:
        return True
    if random.random() < cfg.keep_html_sample:
        return True
    return cfg.html_quota_ok
