"""API sources (table `source`, type `api`): queries instead of pages.

An API source is fetched by workers like any page (`fetch_api` jobs: rate slots per
domain, adaptive back-off, blocks), but it is not crawling: an API's own usage
policy governs it, not robots.txt, so its jobs neither wait for robots.txt nor are
checked against it (query.wikidata.org disallows /sparql for crawlers that would
follow query links; the query service itself is meant for programs). The crawler's
`user_agent` should then name a contact, as the Wikimedia User-Agent policy asks.

Config of the Wikidata adapter:

    {
      "adapter": "wikidata",
      "classes": [
        {"id": "Q11446", "label": "ship"},                       # with subclasses
        {"id": "Q41176", "label": "building", "subclasses": false}
      ],
      "page_size": 1000,      # items per query (at most 5000)
      "max_pages": 50,        # per class
      "endpoint": "https://query.wikidata.org/sparql",   # default; or a mirror
      "priority": 0
    }

Each class is queried page by page: a page that comes back full queues the next.
"""

import logging
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import exc as sa_exc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator.crawl import scheduler
from pos_coordinator.crawl.discovery import SourceConfigError
from pos_coordinator.models import Job, Source
from pos_shared.api import JobOutcome, JobResult
from pos_shared.extract import wikidata

log = logging.getLogger(__name__)
API_PRIORITY = 40
API_REFETCH = timedelta(days=1)  # a repeated discovery does not query the same page again
ADAPTERS = (wikidata.ADAPTER,)


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("adapter") not in ADAPTERS:
        raise SourceConfigError(f"adapter must be one of {', '.join(ADAPTERS)}")
    classes = config.get("classes")
    if not isinstance(classes, list) or not classes:
        raise SourceConfigError("classes must be a non-empty list")
    for c in classes:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str):
            raise SourceConfigError('each class needs an "id" (a Wikidata item, e.g. "Q11446")')
        if not wikidata.QID.match(c["id"]):
            raise SourceConfigError(f"not a Wikidata item id: {c['id']!r}")
        if not isinstance(c.get("label", ""), str) or len(c.get("label", "")) > 100:
            raise SourceConfigError("a class label must be a short string")
        if not isinstance(c.get("subclasses", True), bool):
            raise SourceConfigError("subclasses must be true or false")
        if set(c) - {"id", "label", "subclasses"}:
            raise SourceConfigError(
                f"unknown class keys: {sorted(set(c) - {'id', 'label', 'subclasses'})}"
            )
    for key, lo, hi in (("page_size", 1, wikidata.MAX_PAGE_SIZE), ("max_pages", 1, 1000)):
        v = config.get(key, 1)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise SourceConfigError(f"{key} must be an integer from {lo} to {hi}")
    if not isinstance(config.get("priority", 0), int):
        raise SourceConfigError("priority must be an integer")
    endpoint = config.get("endpoint", wikidata.ENDPOINT)
    if not isinstance(endpoint, str) or not re.match(r"^https?://[^/?#\s]+/[^?#\s]*$", endpoint):
        raise SourceConfigError("endpoint must be an http(s) URL without a query")
    unknown = set(config) - {"adapter", "classes", "page_size", "max_pages", "priority", "endpoint"}
    if unknown:
        raise SourceConfigError(f"unknown keys: {sorted(unknown)}")
    return config


def _page(cls: dict[str, Any], offset: int, page_size: int, endpoint: str) -> tuple[str, dict]:
    subclasses = bool(cls.get("subclasses", True))
    url = wikidata.query_url(cls["id"], subclasses, page_size, offset, endpoint)
    payload = {
        "accept": wikidata.ACCEPT,
        "adapter": wikidata.ADAPTER,
        "params": {
            "class": cls["id"],
            "class_label": cls.get("label") or cls["id"],
            "subclasses": subclasses,
            "offset": offset,
            "page_size": page_size,
        },
    }
    return url, payload


async def _enqueue(
    session: AsyncSession, source: Source, cls: dict, offset: int, page_size: int | None = None
) -> int:
    """`page_size`: the one the earlier pages used (a changed config applies to the
    next discovery, it must not shift the offsets of a query in progress)."""
    cfg = source.config or {}
    page_size = page_size or int(cfg.get("page_size", 1000))
    url, payload = _page(cls, offset, page_size, cfg.get("endpoint", wikidata.ENDPOINT))
    report = await scheduler.enqueue_urls(
        session,
        [url],
        source.name,
        API_PRIORITY + int(cfg.get("priority", 0)),
        job_type="fetch_api",
        extra_payload=payload,
        skip_done_within=API_REFETCH,
    )
    return report.queued


async def discover(session: AsyncSession, source: Source) -> dict[str, int]:
    """Queue the first page of every class."""
    validate_config(source.config or {})
    queued = 0
    for cls in source.config["classes"]:
        queued += await _enqueue(session, source, cls, 0)
    return {"queries": queued}


async def process_results(
    session: AsyncSession, applied: dict[Any, Job], results: list[JobResult]
) -> None:
    """A full page queues the next one (up to `max_pages`). The row count comes from
    the worker (untrusted): it only decides whether to ask for one more page."""
    sources: dict[str, Source | None] = {}
    for r in results:
        job = applied.get(r.job_id)
        if job is None or job.type != "fetch_api" or r.outcome != JobOutcome.done:
            continue
        out = r.output or {}
        rows = out.get("rows")
        params = job.payload.get("params") or {}
        name = job.payload.get("source")
        if not isinstance(rows, int) or isinstance(rows, bool) or out.get("verdict") != "ok":
            continue
        if name not in sources:
            sources[name] = (
                await session.execute(select(Source).where(Source.name == name))
            ).scalar_one_or_none()
        source = sources[name]
        if source is None or source.status != "active" or source.type != "api":
            continue
        cfg = source.config or {}
        page_size = int(params.get("page_size") or 0)
        offset = int(params.get("offset") or 0)
        cls = next((c for c in cfg.get("classes", []) if c.get("id") == params.get("class")), None)
        if cls is None or page_size <= 0 or rows < page_size:
            continue  # the last page, or the class was removed from the source
        if offset // page_size + 1 >= int(cfg.get("max_pages", 50)):
            log.warning("%s: %s has more than max_pages pages", name, cls["id"])
            continue
        try:
            async with session.begin_nested():
                await _enqueue(session, source, cls, offset + page_size, page_size)
        except sa_exc.DBAPIError:
            raise  # deadlocks are retried by the caller
        except Exception:  # noqa: BLE001 - one bad page must not block the batch
            log.exception("%s: queueing the next page of %s failed", name, cls["id"])
