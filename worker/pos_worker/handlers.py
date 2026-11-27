"""Job handlers. Each takes a leased job and a context and returns a JobResult.

Phase 1 ships only dummy handlers to prove the pipeline end to end; fetch,
extract and embed handlers plug in here with the same signature.
"""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import zstandard

from pos_shared import api
from pos_shared.ids import uuid7


@dataclass
class JobContext:
    upload_raw: Callable[[str, str, bytes], Awaitable[None]]


Handler = Callable[[api.LeasedJob, JobContext], Awaitable[api.JobResult]]
HANDLERS: dict[str, Handler] = {}


def handler(job_type: str):
    def register(fn: Handler) -> Handler:
        HANDLERS[job_type] = fn
        return fn

    return register


def done(job: api.LeasedJob, output: dict[str, Any] | None = None, **kw) -> api.JobResult:
    return api.JobResult(
        job_id=job.id, attempt=job.attempt, outcome=api.JobOutcome.done, output=output, **kw
    )


@handler("noop")
async def noop(job: api.LeasedJob, ctx: JobContext) -> api.JobResult:
    return done(job, {"ok": True})


@handler("sleep")
async def sleep(job: api.LeasedJob, ctx: JobContext) -> api.JobResult:
    seconds = float(job.payload.get("seconds", 1.0))
    await asyncio.sleep(min(seconds, 3600))
    return done(job, {"slept": seconds})


@handler("echo")
async def echo(job: api.LeasedJob, ctx: JobContext) -> api.JobResult:
    return done(job, {"echo": job.payload})


@handler("fail")
async def fail(job: api.LeasedJob, ctx: JobContext) -> api.JobResult:
    return api.JobResult(
        job_id=job.id,
        attempt=job.attempt,
        outcome=api.JobOutcome.failed,
        error=str(job.payload.get("error", "requested failure")),
        retryable=bool(job.payload.get("retryable", True)),
    )


@handler("dummy_fetch")
async def dummy_fetch(job: api.LeasedJob, ctx: JobContext) -> api.JobResult:
    """Pretend fetch: stores synthetic evidence through the real raw path."""
    url = str(job.payload.get("url", "https://example.invalid/"))
    body = f"<html><title>{url}</title></html>".encode()
    blob = zstandard.ZstdCompressor(level=10).compress(body)
    sha = hashlib.sha256(blob).hexdigest()
    await ctx.upload_raw("snippets", sha, blob)
    fetch = api.FetchRecord(
        id=uuid7(),
        url=url,
        final_url=url,
        status=200,
        fetched_at=datetime.now(UTC),
        mode="http",
        content_type="text/html",
        response_bytes=len(body),
        blob_sha256=sha,
        content_sha256=hashlib.sha256(body).hexdigest(),
        source=job.payload.get("source"),
    )
    return done(job, {"fetch_id": str(fetch.id)}, fetches=[fetch])
