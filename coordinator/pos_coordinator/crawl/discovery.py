"""Discovery: turn sitemaps into page fetches, per source.

A source's config (table `source`, type `scraper`) steers discovery:

    {
      "sitemaps": ["https://shop.example/sitemap.xml"],   # explicit entry points
      "domains": ["shop.example"],                        # + sitemaps from robots.txt
      "seeds": ["https://shop.example/c/shelves"],         # pages fetched directly
      "product_url_pattern": "/p/[^/]+-\\d+/?$",           # which sitemap URLs to fetch
      "sitemap_url_pattern": "prod-de-AT_\\d+\\.xml$",       # which child sitemaps to follow
      "allow_domains": ["cdn.shop.example"],               # beyond the sitemap's own host
      "priority": 0
    }

Sitemap indexes enqueue their child sitemaps (only those matching
`sitemap_url_pattern`, if set: a shop's index may list every country and catalogue,
hundreds of files of up to 50 MB each); urlsets enqueue the URLs that match the
product pattern (all URLs if there is none). Per the sitemaps protocol, URLs
on other hosts than the sitemap's are ignored unless allowed explicitly; so are
`Sitemap:` lines in a robots.txt that point to another host.

Sitemaps are untrusted input: an index may list itself or form cycles, so
nesting is capped and sitemaps (and pages) fetched recently are not queued again.
One broken sitemap result never blocks the rest of a result batch.
"""

import json
import logging
import re
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import zstandard
from sqlalchemy import exc as sa_exc
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator.crawl import scheduler, urls
from pos_coordinator.models import DomainState, Job, Source
from pos_coordinator.rawstore import RawStore, StoreUnavailable
from pos_shared.api import JobOutcome, JobResult

log = logging.getLogger(__name__)
SITEMAP_PRIORITY = 50  # after robots.txt, before pages
MAX_SITEMAP_DEPTH = 4  # index -> index -> ... -> urlset
SITEMAP_REFETCH = timedelta(hours=12)  # cycles and repeated indexes
PAGE_REFETCH = timedelta(hours=20)  # one discovery round re-queues a page once a day


class SourceConfigError(ValueError):
    pass


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    for key in ("product_url_pattern", "sitemap_url_pattern"):
        pattern = config.get(key)
        if pattern is None:
            continue
        if not isinstance(pattern, str):
            raise SourceConfigError(f"{key} must be a string")
        try:
            re.compile(pattern)
        except re.error as e:
            raise SourceConfigError(f"{key}: {e}") from e
    for key in ("sitemaps", "domains", "seeds", "allow_domains"):
        value = config.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SourceConfigError(f"{key} must be a list of strings")
    if not isinstance(config.get("priority", 0), int):
        raise SourceConfigError("priority must be an integer")
    return config


def _base_url(cfg: dict[str, Any], domain: str) -> str:
    """Scheme and port for a bare domain: from the source's own URLs on that host if it
    has any (a seed or a sitemap), else https."""
    for u in [*cfg.get("seeds", []), *cfg.get("sitemaps", [])]:
        if isinstance(u, str) and urls.host_of(u) == domain:
            try:
                parts = urlsplit(urls.normalize(u))
            except urls.BadUrl:
                continue
            return f"{parts.scheme}://{parts.netloc}/"
    return f"https://{domain}/"


def _domain_sitemaps(
    ds: DomainState, domain: str, allowed: set[str], cfg: dict[str, Any]
) -> list[str]:
    """Sitemaps of a domain from its robots.txt (only on the shop's own or allowed
    hosts: robots.txt may name any host), or the conventional /sitemap.xml."""
    named = [u for u in (ds.sitemaps or []) if urls.host_of(u) in {domain, *allowed}]
    return named or [_base_url(cfg, domain) + "sitemap.xml"]


async def discover(session: AsyncSession, source: Source) -> dict[str, int]:
    """Queue the entry points of a source: its sitemaps (explicit and from robots.txt)
    and its seed pages. Domains whose robots.txt is not known yet get it fetched first;
    their sitemaps are queued when it arrives (`resume`)."""
    cfg = source.config or {}
    allowed = {d.lower() for d in cfg.get("allow_domains", [])}
    sitemap_urls = list(cfg.get("sitemaps", []))
    waiting = 0
    for raw in cfg.get("domains", []):
        domain = raw.strip().lower().rstrip(".")
        if not domain:
            continue
        await scheduler.ensure_domains(session, {domain: _base_url(cfg, domain)})
        ds = await session.get(DomainState, domain, with_for_update=True, populate_existing=True)
        if ds is None:
            continue
        if ds.robots_status in (None, "unreachable"):
            ds.pending_discovery = sorted({*(ds.pending_discovery or []), source.name})
            # also after an earlier robots job ran out of attempts (dedup: at most one)
            await scheduler.ensure_robots_job(
                session, domain, _base_url(cfg, domain) + "robots.txt"
            )
            waiting += 1
            continue
        sitemap_urls += _domain_sitemaps(ds, domain, allowed, cfg)
    s = await scheduler.enqueue_urls(
        session,
        sitemap_urls,
        source.name,
        SITEMAP_PRIORITY,
        job_type="fetch_sitemap",
        skip_done_within=SITEMAP_REFETCH,
    )
    p = await scheduler.enqueue_urls(
        session, list(cfg.get("seeds", [])), source.name, int(cfg.get("priority", 0))
    )
    return {"sitemaps": s.queued, "seeds": p.queued, "waiting_for_robots": waiting}


async def resume(session: AsyncSession, domain: str, source_names: list[str]) -> None:
    """robots.txt of `domain` arrived: queue its sitemaps for the sources waiting on it."""
    ds = await session.get(DomainState, domain)
    if ds is None:
        return
    for name in source_names:
        source = (
            await session.execute(select(Source).where(Source.name == name))
        ).scalar_one_or_none()
        if source is None or source.status != "active":
            continue
        cfg = source.config or {}
        allowed = {d.lower() for d in cfg.get("allow_domains", [])}
        await scheduler.enqueue_urls(
            session,
            _domain_sitemaps(ds, domain, allowed, cfg),
            name,
            SITEMAP_PRIORITY,
            job_type="fetch_sitemap",
            skip_done_within=SITEMAP_REFETCH,
        )


async def process_results(
    session: AsyncSession,
    applied: dict[Any, Job],
    results: list[JobResult],
    store: RawStore,
) -> None:
    """Follow up on stored fetch_sitemap results."""
    sources: dict[str, Source | None] = {}
    for r in results:
        job = applied.get(r.job_id)
        if job is None or job.type != "fetch_sitemap" or r.outcome != JobOutcome.done:
            continue
        sha = (r.output or {}).get("evidence_sha256")
        if not sha or not store.exists("snippets", sha):
            continue
        evidence = json.loads(zstandard.ZstdDecompressor().decompress(store.get("snippets", sha)))
        name = job.payload.get("source")
        if name not in sources:
            sources[name] = (
                (
                    await session.execute(select(Source).where(Source.name == name))
                ).scalar_one_or_none()
                if name
                else None
            )
        try:
            async with session.begin_nested():
                await _follow(session, job, evidence, sources[name])
        except (sa_exc.DBAPIError, StoreUnavailable):
            raise  # deadlocks are retried by the caller; the store comes back
        except Exception:  # noqa: BLE001 - a malformed sitemap must not block the batch
            log.exception("sitemap %s: following its URLs failed", job.payload.get("url"))


async def _follow(session: AsyncSession, job: Job, evidence: dict, source: Source | None) -> None:
    cfg = (source.config if source else None) or {}
    name = source.name if source else job.payload.get("source")
    host = urls.host_of(job.payload["url"]) or ""
    allowed_hosts = {host, *(d.lower() for d in cfg.get("allow_domains", []))}

    def same_site(u: Any) -> bool:
        return isinstance(u, str) and urls.host_of(u) in allowed_hosts

    depth = int(job.payload.get("depth") or 0) + 1
    me = _normalized(job.payload["url"])
    wanted = re.compile(cfg["sitemap_url_pattern"]) if cfg.get("sitemap_url_pattern") else None
    children = [
        u
        for u in evidence.get("sitemaps", [])
        if same_site(u) and _normalized(u) != me and (wanted is None or wanted.search(u))
    ]
    if children and depth <= MAX_SITEMAP_DEPTH:
        await scheduler.enqueue_urls(
            session,
            children,
            name,
            SITEMAP_PRIORITY,
            job_type="fetch_sitemap",
            extra_payload={"depth": depth},
            skip_done_within=SITEMAP_REFETCH,
        )
    elif children:
        log.warning("sitemap %s: nested too deep, children ignored", job.payload["url"])
    pattern = re.compile(cfg["product_url_pattern"]) if cfg.get("product_url_pattern") else None
    pages = [
        e["loc"]
        for e in evidence.get("urls", [])
        if isinstance(e, dict)
        and same_site(e.get("loc"))
        and (pattern is None or pattern.search(e["loc"]))
    ]
    if pages:
        report = await scheduler.enqueue_urls(
            session, pages, name, int(cfg.get("priority", 0)), skip_done_within=PAGE_REFETCH
        )
        log.info(
            "sitemap %s: %d pages queued (%d known)",
            job.payload["url"],
            report.queued,
            report.duplicates,
        )


def _normalized(url: Any) -> str | None:
    try:
        return urls.normalize(url) if isinstance(url, str) else None
    except urls.BadUrl:
        return None


async def retry_pending_robots(session: AsyncSession) -> int:
    """Domains whose discovery waits for robots.txt, and whose robots.txt job is gone
    (ran out of attempts during an outage): queue another when it is due. Without this
    such a domain, with no other jobs queued, would never be leased again."""
    due = (
        (
            await session.execute(
                select(DomainState).where(
                    DomainState.pending_discovery.is_not(None),
                    or_(
                        DomainState.robots_next_check_at.is_(None),
                        DomainState.robots_next_check_at <= func.now(),
                    ),
                    ~exists().where(
                        Job.domain == DomainState.domain,
                        Job.type == "fetch_robots",
                        Job.status.in_(("queued", "leased")),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for ds in due:
        source = (
            await session.execute(
                select(Source).where(Source.name.in_(ds.pending_discovery or [])).limit(1)
            )
        ).scalar_one_or_none()
        base = _base_url((source.config or {}) if source else {}, ds.domain)
        await scheduler.ensure_robots_job(session, ds.domain, base + "robots.txt")
    return len(due)
