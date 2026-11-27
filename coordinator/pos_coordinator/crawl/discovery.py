"""Discovery: turn sitemaps into page fetches, per source.

A source's config (table `source`, type `scraper`) steers discovery:

    {
      "sitemaps": ["https://shop.example/sitemap.xml"],   # explicit entry points
      "domains": ["shop.example"],                        # + sitemaps from robots.txt
      "seeds": ["https://shop.example/c/shelves"],         # pages fetched directly
      "product_url_pattern": "/p/[^/]+-\\d+/?$",           # which sitemap URLs to fetch
      "allow_domains": ["cdn.shop.example"],               # beyond the sitemap's own host
      "priority": 0
    }

Sitemap indexes enqueue their child sitemaps; urlsets enqueue the URLs that
match the pattern (all URLs if there is none). Per the sitemaps protocol, URLs
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

import zstandard
from sqlalchemy import exc as sa_exc
from sqlalchemy import select
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
    pattern = config.get("product_url_pattern")
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.error as e:
            raise SourceConfigError(f"product_url_pattern: {e}") from e
    for key in ("sitemaps", "domains", "seeds", "allow_domains"):
        value = config.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SourceConfigError(f"{key} must be a list of strings")
    if not isinstance(config.get("priority", 0), int):
        raise SourceConfigError("priority must be an integer")
    return config


async def discover(session: AsyncSession, source: Source) -> dict[str, int]:
    """Queue the entry points of a source: its sitemaps (explicit and from robots.txt)
    and its seed pages."""
    cfg = source.config or {}
    sitemap_urls = list(cfg.get("sitemaps", []))
    allowed = {d.lower() for d in cfg.get("allow_domains", [])}
    for domain in cfg.get("domains", []):
        ds = await session.get(DomainState, domain)
        if ds is not None and ds.sitemaps:
            # robots.txt may name any host; only the shop's own (or allowed) hosts count
            sitemap_urls += [
                u for u in ds.sitemaps if urls.host_of(u) in {domain.lower(), *allowed}
            ]
    s = await scheduler.enqueue_urls(
        session, sitemap_urls, source.name, SITEMAP_PRIORITY, job_type="fetch_sitemap"
    )
    p = await scheduler.enqueue_urls(
        session, list(cfg.get("seeds", [])), source.name, int(cfg.get("priority", 0))
    )
    return {"sitemaps": s.queued, "seeds": p.queued}


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
    children = [u for u in evidence.get("sitemaps", []) if same_site(u) and _normalized(u) != me]
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
