"""Fetch job handlers: `fetch_http` (a page) and `fetch_robots` (robots.txt).

Payload of fetch_http (set by the coordinator's crawl scheduler):
    url, source, delay_s (wait before fetching: the domain's rate slot),
    keep_html (archive the full page), user_agent
Output metrics drive the coordinator's per-domain rate control.
"""

import asyncio
import contextlib
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import zstandard

from pos_shared import api
from pos_shared.ids import uuid7
from pos_worker.fetch import blocks, breaker, http, sitemaps, snippets
from pos_worker.handlers import JobContext, done, handler

DEFAULT_UA = "PriceOfSizeBot/0.1"
MAX_DELAY_S = 600
_zstd = zstandard.ZstdCompressor(level=12)


_CLIENTS: dict[str, Any] = {}


class _Limiter:
    """Concurrent requests of this worker, resizable when its slots change."""

    def __init__(self, limit: int):
        self.limit = limit
        self.active = 0
        self._cond: asyncio.Condition | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _condition(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        if self._cond is None or self._loop is not loop:  # conditions are loop-bound
            self._cond, self._loop, self.active = asyncio.Condition(), loop, 0
        return self._cond

    async def __aenter__(self):
        cond = self._condition()
        async with cond:
            await cond.wait_for(lambda: self.active < self.limit)
            self.active += 1

    async def __aexit__(self, *exc):
        cond = self._condition()
        async with cond:
            self.active -= 1
            cond.notify_all()

    def resize(self, limit: int) -> None:
        self.limit = max(1, limit)
        cond = self._cond
        if cond is None:
            return

        async def wake():  # waiters re-check against the new limit
            async with cond:
                cond.notify_all()

        with contextlib.suppress(RuntimeError):  # no running loop: nobody is waiting
            self._wake = asyncio.get_running_loop().create_task(wake())


_LIMIT = _Limiter(8)


def set_concurrency(http_slots: int) -> None:
    _LIMIT.resize(http_slots)


def client_for(user_agent: str):
    """One pooled HTTP client per user agent and event loop (clients are loop-bound)."""
    key = (id(asyncio.get_running_loop()), user_agent)
    c = _CLIENTS.get(key)
    if c is None or c.is_closed:
        c = _CLIENTS[key] = http.make_client(user_agent)
    return c


async def _store(ctx: JobContext, kind: str, data: bytes) -> str:
    blob = _zstd.compress(data)
    sha = hashlib.sha256(blob).hexdigest()
    await ctx.upload_raw(kind, sha, blob)
    return sha


def _decode(body: bytes, content_type: str | None) -> str:
    charset = None
    if content_type and "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=")[-1].split(";")[0].strip()
    for enc in (charset, "utf-8", "cp1252"):
        if not enc:
            continue
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


async def _wait_for_slot(job: api.LeasedJob) -> None:
    delay = float(job.payload.get("delay_s", 0) or 0)
    if delay > 0:
        await asyncio.sleep(min(delay, MAX_DELAY_S))


def _skipped(job: api.LeasedJob, domain: str) -> api.JobResult:
    """Not fetched: the domain refused us moments ago (see breaker)."""
    return api.JobResult(
        job_id=job.id,
        attempt=job.attempt,
        outcome=api.JobOutcome.failed,
        error=f"skipped:domain_paused:{domain}",
        retryable=True,
    )


@handler("fetch_http")
async def fetch_http(job: api.LeasedJob, ctx: JobContext) -> api.JobResult:
    url = str(job.payload["url"])
    domain = urlsplit(url).hostname or ""
    await _wait_for_slot(job)
    if breaker.paused(domain):
        return _skipped(job, domain)
    client = client_for(str(job.payload.get("user_agent") or DEFAULT_UA))
    async with _LIMIT:
        fetched_at = datetime.now(UTC)
        res = await http.fetch(client, url)

    text = (
        _decode(res.body, res.content_type)
        if res.body and http.is_textual(res.content_type)
        else ""
    )
    snip = snippets.capture(text, url) if text else {"snippets_version": snippets.SNIPPETS_VERSION}
    verdict = blocks.classify(res.status, text[:50_000], res.error)
    if verdict.kind == "blocked" and res.status == 200 and snip.get("has_structured_data"):
        verdict = blocks.Verdict("ok")  # a product page that merely mentions robots
    breaker.trip(domain, verdict.kind, res.retry_after_s)

    evidence = {
        "url": url,
        "final_url": res.final_url,
        "status": res.status,
        "headers": res.headers,
        "fetched_at": fetched_at.isoformat(),
        "elapsed_ms": res.elapsed_ms,
        "error": res.error,
        "truncated": res.truncated,
        "verdict": {"kind": verdict.kind, "reason": verdict.reason},
        "snippets": snip,
    }
    snippets_sha = await _store(ctx, "snippets", json.dumps(evidence, sort_keys=True).encode())
    # The full page: when asked (retention policy), when the snippets found nothing
    # to work with, and for refusals (evidence for the block log).
    keep = (
        bool(job.payload.get("keep_html"))
        or not snip.get("has_structured_data")
        or verdict.kind == "blocked"
    )
    html_sha = await _store(ctx, "html", res.body) if keep and res.body else None

    fetch = api.FetchRecord(
        id=uuid7(),
        url=url,
        final_url=res.final_url,
        status=res.status,
        fetched_at=fetched_at,
        mode="http",
        content_type=(res.content_type or "")[:200] or None,
        response_bytes=len(res.body),
        blob_sha256=snippets_sha,
        html_sha256=html_sha,
        content_sha256=hashlib.sha256(res.body).hexdigest() if res.body else None,
        source=job.payload.get("source"),
        error=res.error,
    )
    output = {
        "status": res.status,
        "elapsed_ms": res.elapsed_ms,
        "bytes": len(res.body),
        "error": res.error,
        "retry_after_s": res.retry_after_s,
        "verdict": verdict.kind,
        "reason": verdict.reason,
        "has_structured_data": bool(snip.get("has_structured_data")),
        "final_url": res.final_url,
    }
    # Every outcome is a result, never a retry from the worker's side: whether and when
    # to try again is the coordinator's decision (rate control, blocks, robots).
    return done(job, output, fetches=[fetch])


@handler("fetch_robots")
async def fetch_robots(job: api.LeasedJob, ctx: JobContext) -> api.JobResult:
    url = str(job.payload["url"])  # https://host/robots.txt
    domain = urlsplit(url).hostname or ""
    await _wait_for_slot(job)
    if breaker.paused(domain):
        return _skipped(job, domain)
    client = client_for(str(job.payload.get("user_agent") or DEFAULT_UA))
    async with _LIMIT:
        fetched_at = datetime.now(UTC)
        res = await http.fetch(client, url, max_bytes=512 * 1024)
    body = res.body.decode("utf-8", errors="replace") if res.body else ""
    sha = (
        await _store(
            ctx,
            "snippets",
            json.dumps(
                {
                    "url": url,
                    "status": res.status,
                    "headers": res.headers,
                    "body": body,
                    "fetched_at": fetched_at.isoformat(),
                    "error": res.error,
                },
                sort_keys=True,
            ).encode(),
        )
        if (res.body or res.status)
        else None
    )
    fetch = api.FetchRecord(
        id=uuid7(),
        url=url,
        final_url=res.final_url,
        status=res.status,
        fetched_at=fetched_at,
        mode="http",
        content_type=(res.content_type or "")[:200] or None,
        response_bytes=len(res.body),
        blob_sha256=sha,
        source="robots",
        error=res.error,
    )
    verdict = blocks.classify(res.status, body[:20_000], res.error)
    breaker.trip(domain, verdict.kind, res.retry_after_s)
    return done(
        job,
        {
            "status": res.status,
            "body": body if res.status == 200 else None,
            "error": res.error,
            "elapsed_ms": res.elapsed_ms,
            "retry_after_s": res.retry_after_s,
            "verdict": verdict.kind,
            "reason": verdict.reason,
        },
        fetches=[fetch],
    )


@handler("fetch_sitemap")
async def fetch_sitemap(job: api.LeasedJob, ctx: JobContext) -> api.JobResult:
    """Fetch and parse a sitemap. The discovered URLs are evidence too: they go to the
    raw store, and only counts travel in the result."""
    url = str(job.payload["url"])
    domain = urlsplit(url).hostname or ""
    await _wait_for_slot(job)
    if breaker.paused(domain):
        return _skipped(job, domain)
    client = client_for(str(job.payload.get("user_agent") or DEFAULT_UA))
    async with _LIMIT:
        fetched_at = datetime.now(UTC)
        res = await http.fetch(client, url, max_bytes=sitemaps.MAX_BYTES)
    verdict = blocks.classify(res.status, res.body[:20_000].decode("utf-8", "replace"), res.error)
    breaker.trip(domain, verdict.kind, res.retry_after_s)
    parsed, parse_error = None, None
    if res.status == 200 and res.body and verdict.kind == "ok":
        try:
            parsed = sitemaps.parse(res.body)
        except sitemaps.SitemapError as e:
            parse_error = str(e)[:500]
    evidence = {
        "url": url,
        "final_url": res.final_url,
        "status": res.status,
        "headers": res.headers,
        "fetched_at": fetched_at.isoformat(),
        "error": res.error,
        "parse_error": parse_error,
        "kind": parsed.kind if parsed else None,
        "urls": parsed.urls if parsed else [],
        "sitemaps": parsed.sitemaps if parsed else [],
        "truncated": parsed.truncated if parsed else False,
    }
    sha = await _store(ctx, "snippets", json.dumps(evidence, sort_keys=True).encode())
    fetch = api.FetchRecord(
        id=uuid7(),
        url=url,
        final_url=res.final_url,
        status=res.status,
        fetched_at=fetched_at,
        mode="http",
        content_type=(res.content_type or "")[:200] or None,
        response_bytes=len(res.body),
        blob_sha256=sha,
        source=job.payload.get("source"),
        error=res.error or parse_error,
    )
    return done(
        job,
        {
            "status": res.status,
            "elapsed_ms": res.elapsed_ms,
            "error": res.error,
            "retry_after_s": res.retry_after_s,
            "verdict": verdict.kind,
            "reason": verdict.reason,
            "sitemap_kind": parsed.kind if parsed else None,
            "url_count": len(parsed.urls) if parsed else 0,
            "sitemap_count": len(parsed.sitemaps) if parsed else 0,
            "parse_error": parse_error,
            "evidence_sha256": sha,
        },
        fetches=[fetch],
    )
