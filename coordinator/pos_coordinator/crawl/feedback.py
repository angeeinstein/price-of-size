"""Apply fetch outcomes to the crawl state.

Workers report every fetch as a result (a 429 is evidence too). Here the
coordinator decides what it means:

* the job: ok/gone pages are done; throttled, failed and blocked fetches
  become retryable failures, so the page is fetched again later;
* the (domain, egress group) rate: AIMD via `ratecontrol`, with cooldowns;
* the domain: page counter, blocks (CAPTCHA, bot wall, repeated 403);
* robots.txt: rules, crawl-delay and when to check again.

Every change of rate, cooldown, block or robots rules is logged as a
`domain_event`.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exc as sa_exc
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator.crawl import ratecontrol, robots, scheduler, urls
from pos_coordinator.crawl.scheduler import FETCH_TYPES, CrawlConfig, egress_group
from pos_coordinator.models import DomainEgressState, DomainEvent, DomainState, Job, Node
from pos_shared.api import JobOutcome, JobResult

log = logging.getLogger(__name__)
RETRY_VERDICTS = ("throttled", "error", "blocked")
MAX_REDIRECT_HOPS = 5  # pages and sitemaps; the worker follows none itself


def reinterpret(job_type: str, result: JobResult) -> JobResult:
    """A fetch the worker completed may still be a failure for the crawl."""
    if job_type not in FETCH_TYPES or result.outcome != JobOutcome.done or not result.output:
        return result
    verdict = result.output.get("verdict")
    if verdict in RETRY_VERDICTS:
        return result.model_copy(
            update={
                "outcome": JobOutcome.failed,
                "retryable": True,
                "error": f"{verdict}:{result.output.get('reason')}",
            }
        )
    return result


async def apply(
    session: AsyncSession,
    node: Node,
    jobs: dict[Any, Job],
    results: list[JobResult],
    cfg: CrawlConfig,
) -> None:
    """Update crawl state for accepted fetch results (call after queue.complete)."""
    now: datetime = (await session.execute(select(func.now()))).scalar_one()
    group = egress_group(node, cfg)
    # Lock order: domain_state, then domain_egress_state (as in the scheduler), and
    # domains in sorted order, so concurrent batches and leases cannot deadlock.
    ordered = sorted(
        (r for r in results if (j := jobs.get(r.job_id)) is not None and j.domain),
        key=lambda r: jobs[r.job_id].domain,
    )
    redirects: list[tuple[Job, str]] = []
    resumed: list[tuple[str, list[str]]] = []
    for r in ordered:
        job = jobs.get(r.job_id)
        if job is None or job.type not in FETCH_TYPES or not job.domain or not r.output:
            continue
        ds = await session.get(DomainState, job.domain, with_for_update=True)
        if ds is None:
            continue
        eg = await session.get(DomainEgressState, (job.domain, group), with_for_update=True)
        if eg is None:
            # The node's public IP changed since the lease: its answer (a 429, a block
            # page, robots.txt) still counts, for the group it now belongs to.
            await session.execute(
                pg_insert(DomainEgressState)
                .values(domain=job.domain, egress_group=group, rate_rps=cfg.base_rate_rps)
                .on_conflict_do_nothing(index_elements=["domain", "egress_group"])
            )
            eg = await session.get(
                DomainEgressState, (job.domain, group), with_for_update=True, populate_existing=True
            )
            if eg is None:
                continue
        try:
            # Worker output is untrusted: a malformed value is logged and skipped, it
            # must not fail the whole batch (the node would resend it forever).
            async with session.begin_nested():
                _apply_one(session, job, ds, eg, r.output, now, cfg)
        except sa_exc.DBAPIError:
            raise  # deadlocks: the caller retries the transaction
        except Exception:  # noqa: BLE001
            log.exception("feedback for job %s failed", job.id)
            continue
        if (
            job.type == "fetch_robots"
            and ds.pending_discovery
            and ds.robots_status
            in (
                "rules",
                "none",
            )
        ):
            resumed.append((ds.domain, list(ds.pending_discovery)))
            ds.pending_discovery = None
        target = r.output.get("redirect_to")
        if job.type in ("fetch_http", "fetch_sitemap") and isinstance(target, str):
            redirects.append((job, target))
    for job, target in redirects:
        await _follow_redirect(session, job, target)
    for domain, sources in resumed:
        from pos_coordinator.crawl import discovery  # discovery imports this module's peers

        await discovery.resume(session, domain, sources)


def _apply_one(session, job: Job, ds: DomainState, eg: DomainEgressState, out, now, cfg) -> None:
    if job.type == "fetch_robots":
        _apply_robots(session, ds, out, now, cfg.user_agent)
        if ds.robots_crawl_delay_s:  # the first page waits for the delay too
            first = now + timedelta(seconds=ds.robots_crawl_delay_s)
            eg.next_slot_at = max(eg.next_slot_at or first, first)
    else:
        ds.pages_fetched = (ds.pages_fetched or 0) + 1
        ds.last_fetch_at = now
    _apply_rate(session, ds, eg, out, now, cfg)
    ds.updated_at = func.now()
    eg.updated_at = func.now()


def same_site(a: str, b: str) -> bool:
    """www.shop.example and shop.example (and shop.example/at) are one site; another
    registrable host (a tracker, a login service, a marketplace) is not."""
    ha, hb = urls.host_of(a), urls.host_of(b)
    if not ha or not hb:
        return False
    return ha.removeprefix("www.") == hb.removeprefix("www.")


async def _follow_redirect(session: AsyncSession, job: Job, target: str) -> None:
    """A page (or sitemap) redirected: fetch the target as its own job, through its
    host's robots.txt and rate schedule, if it is the same site and not a loop."""
    hops = int(job.payload.get("redirect_hops") or 0) + 1
    if hops > MAX_REDIRECT_HOPS or not same_site(job.payload.get("url", ""), target):
        return
    extra = {"redirect_hops": hops}
    if job.payload.get("depth") is not None:
        extra["depth"] = job.payload["depth"]  # a sitemap keeps its nesting depth
    await scheduler.enqueue_urls(
        session,
        [target],
        job.payload.get("source"),
        job.priority,
        job_type=job.type,
        extra_payload=extra,
        skip_done_within=timedelta(days=1),
    )


def _apply_robots(
    session: AsyncSession, ds: DomainState, out: dict, now: datetime, agent: str
) -> None:
    verdict = out.get("verdict")
    if verdict in ("throttled", "blocked"):
        return  # not an answer about robots.txt; retried through the job
    kind = robots.status_kind(out.get("status") if not out.get("error") else None)
    body = out.get("body") if kind == "rules" else None
    old = (ds.robots_status, ds.robots_txt)
    ds.robots_status = kind
    ds.robots_txt = body
    ds.robots_http_status = out.get("status")
    ds.robots_fetched_at = now
    ds.robots_crawl_delay_s = robots.crawl_delay(kind, body, agent)
    ds.sitemaps = robots.sitemaps(body) or None
    wait = robots.ROBOTS_MAX_AGE_S if kind != "unreachable" else robots.ROBOTS_RETRY_S
    ds.robots_next_check_at = now + timedelta(seconds=wait)
    if old != (kind, body):
        session.add(
            DomainEvent(
                domain=ds.domain,
                kind="robots",
                reason=kind,
                details={
                    "http_status": out.get("status"),
                    "crawl_delay_s": ds.robots_crawl_delay_s,
                },
            )
        )


def _apply_rate(
    session: AsyncSession,
    ds: DomainState,
    eg: DomainEgressState,
    out: dict,
    now: datetime,
    cfg: CrawlConfig,
) -> None:
    state = ratecontrol.RateState(
        rate_rps=eg.rate_rps,
        clean_streak=eg.clean_streak,
        throttle_count=eg.throttle_count,
        latency_ewma_ms=eg.latency_ewma_ms,
        latency_base_ms=eg.latency_base_ms,
        consecutive_errors=eg.consecutive_errors,
        consecutive_403=eg.consecutive_403,
        consecutive_blocks=eg.consecutive_blocks,
    )
    old_rate = eg.rate_rps
    top = ratecontrol.ceiling(
        cfg.max_rate_rps,
        robots.crawl_delay(ds.robots_status, ds.robots_txt, cfg.user_agent),
        (ds.overrides or {}).get("rate_rps"),
    )
    d = ratecontrol.observe(
        state,
        out.get("verdict") or "ok",
        out.get("reason"),
        out.get("elapsed_ms"),
        out.get("retry_after_s"),
        top,
        cfg.block_policy(ds.overrides),
    )
    eg.rate_rps = state.rate_rps
    eg.clean_streak = state.clean_streak
    eg.throttle_count = state.throttle_count
    eg.latency_ewma_ms = state.latency_ewma_ms
    eg.latency_base_ms = state.latency_base_ms
    eg.consecutive_errors = state.consecutive_errors
    eg.consecutive_403 = state.consecutive_403
    eg.consecutive_blocks = state.consecutive_blocks
    if d.cooldown_s:
        until = now + timedelta(seconds=d.cooldown_s)
        eg.cooldown_until = max(eg.cooldown_until or until, until)
        eg.next_slot_at = None  # restart the schedule after the pause
    if d.event == "blocked":
        if ds.block_status != "blocked":
            ds.block_status = "blocked"
            ds.block_reason = d.reason
            ds.blocked_at = now
            session.add(
                DomainEvent(
                    domain=ds.domain,
                    egress_group=eg.egress_group,
                    kind="blocked",
                    reason=d.reason,
                    details={"http_status": out.get("status"), "url": out.get("final_url")},
                )
            )
    elif d.event:
        session.add(
            DomainEvent(
                domain=ds.domain,
                egress_group=eg.egress_group,
                kind=d.event,
                reason=d.reason,
                old_rate_rps=old_rate,
                new_rate_rps=eg.rate_rps,
                details={"cooldown_s": d.cooldown_s, "http_status": out.get("status")},
            )
        )


async def retry_blocked(session: AsyncSession, settings: dict[str, Any]) -> int:
    """Blocked domains whose `block_retry_after_h` (setting or domain override) has
    passed are tried again. Their refusal counters stay: if the next answer is a
    refusal again, the domain is blocked again at once; a clean answer resets them."""
    cfg_default = settings.get("block_retry_after_h")
    now = (await session.execute(select(func.now()))).scalar_one()
    retried = 0
    rows = (
        await session.execute(
            select(DomainState)
            .where(DomainState.block_status == "blocked", DomainState.blocked_at.is_not(None))
            .with_for_update(skip_locked=True)
        )
    ).scalars()
    for ds in rows:
        hours = (ds.overrides or {}).get("block_retry_after_h", cfg_default)
        if hours is None or ds.blocked_at > now - timedelta(hours=float(hours)):
            continue
        reason = ds.block_reason
        ds.block_status, ds.block_reason, ds.blocked_at = "ok", None, None
        ds.unblocked_at = now
        for g in (
            await session.execute(
                select(DomainEgressState).where(DomainEgressState.domain == ds.domain)
            )
        ).scalars():
            g.cooldown_until = None
        probe = await _ensure_probe(session, ds.domain)
        session.add(
            DomainEvent(
                domain=ds.domain,
                kind="unblocked",
                reason="auto_retry",
                details={"after_h": hours, "block_reason": reason, "probe_url": probe},
            )
        )
        retried += 1
    return retried


# Keys the scheduler adds to a job's payload at lease time: not part of the job.
_LEASE_KEYS = {
    "delay_s",
    "interval_s",
    "user_agent",
    "refusal_pause_s",
    "keep_html",
    "unblocked_at",
}


async def _ensure_probe(session: AsyncSession, domain: str) -> str | None:
    """A retried domain needs a job to try: its refusals may have used up the attempts
    of every job it had. If none is queued, the latest fetch of the domain is queued
    again (a new job, the old one stays as it ended). Returns the probe's URL."""
    page_types = [t for t in FETCH_TYPES if t != "fetch_robots"]
    waiting = (
        await session.execute(
            select(Job.id)
            .where(
                Job.domain == domain,
                Job.type.in_(page_types),
                Job.status.in_(("queued", "leased")),
            )
            .limit(1)
        )
    ).first()
    if waiting:
        return None
    last = (
        await session.execute(
            select(Job)
            .where(Job.domain == domain, Job.type.in_(page_types))
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    url = (last.payload or {}).get("url") if last is not None else None
    if not isinstance(url, str):
        return None
    extra = {
        k: v for k, v in (last.payload or {}).items() if k not in _LEASE_KEYS | {"url", "source"}
    }
    report = await scheduler.enqueue_urls(
        session,
        [url],
        (last.payload or {}).get("source"),
        last.priority,
        job_type=last.type,
        extra_payload=extra,
    )
    return url if report.queued else None
