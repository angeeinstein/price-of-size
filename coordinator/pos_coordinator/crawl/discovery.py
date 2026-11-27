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
on other hosts than the sitemap's are ignored unless allowed explicitly.
"""

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import zstandard
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator.crawl import scheduler
from pos_coordinator.models import DomainState, Job, Source
from pos_coordinator.rawstore import RawStore
from pos_shared.api import JobOutcome, JobResult

log = logging.getLogger(__name__)
SITEMAP_PRIORITY = 50  # after robots.txt, before pages


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
    for domain in cfg.get("domains", []):
        ds = await session.get(DomainState, domain)
        if ds is not None and ds.sitemaps:
            sitemap_urls += ds.sitemaps
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
        await _follow(session, job, evidence, sources[name])


async def _follow(session: AsyncSession, job: Job, evidence: dict, source: Source | None) -> None:
    cfg = (source.config if source else None) or {}
    name = source.name if source else job.payload.get("source")
    host = urlsplit(job.payload["url"]).hostname or ""
    allowed_hosts = {host, *cfg.get("allow_domains", [])}

    def same_site(u: str) -> bool:
        return (urlsplit(u).hostname or "") in allowed_hosts

    children = [u for u in evidence.get("sitemaps", []) if same_site(u)]
    if children:
        await scheduler.enqueue_urls(
            session, children, name, SITEMAP_PRIORITY, job_type="fetch_sitemap"
        )
    pattern = re.compile(cfg["product_url_pattern"]) if cfg.get("product_url_pattern") else None
    pages = [
        e["loc"]
        for e in evidence.get("urls", [])
        if e.get("loc") and same_site(e["loc"]) and (pattern is None or pattern.search(e["loc"]))
    ]
    if pages:
        report = await scheduler.enqueue_urls(session, pages, name, int(cfg.get("priority", 0)))
        log.info(
            "sitemap %s: %d pages queued (%d known)",
            job.payload["url"],
            report.queued,
            report.duplicates,
        )
