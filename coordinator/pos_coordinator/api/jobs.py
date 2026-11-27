"""Job leasing and result submission for nodes."""

import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from pos_coordinator import partitions, queue, runtime_settings
from pos_coordinator.api.deps import CurrentNode, Session, check_api_version
from pos_coordinator.crawl import feedback, scheduler
from pos_coordinator.models import Job, RawObject
from pos_shared import api

router = APIRouter(prefix="/api/v1/jobs", dependencies=[Depends(check_api_version)])
HTML_SIZE_CACHE_S = 300


@router.post("/lease", response_model=api.LeaseResponse)
async def lease(body: api.LeaseRequest, request: Request, node: CurrentNode, session: Session):
    if node.desired_run_state != "running":
        return api.LeaseResponse(jobs=[])
    types = body.job_types
    if node.desired_job_types is not None:
        types = [t for t in types if t in node.desired_job_types]
    if not types:
        return api.LeaseResponse(jobs=[])
    cfg = await crawl_config(request, session)
    out: list[api.LeasedJob] = []

    # Fetch jobs only ever go through the crawl scheduler (rate slots, robots.txt).
    fetch_types = [t for t in types if t in scheduler.FETCH_TYPES]
    if fetch_types:
        for h in await scheduler.lease_fetch(session, node, fetch_types, body.max_jobs, cfg):
            out.append(
                api.LeasedJob(
                    id=h.job.id,
                    type=h.job.type,
                    payload={**h.job.payload, **h.extra},
                    attempt=h.job.attempts,
                    lease_expires_at=h.job.lease_expires_at,
                    lease_seconds=cfg.lease_s,
                )
            )
    other = [t for t in types if t not in scheduler.FETCH_TYPES]
    room = body.max_jobs - len(out)
    if other and room > 0:
        for j in await queue.lease(session, node.id, other, room, cfg.lease_s):
            out.append(
                api.LeasedJob(
                    id=j.id,
                    type=j.type,
                    payload=j.payload,
                    attempt=j.attempts,
                    lease_expires_at=j.lease_expires_at,
                    lease_seconds=cfg.lease_s,
                )
            )
    await session.commit()
    return api.LeaseResponse(jobs=out)


async def crawl_config(request: Request, session) -> scheduler.CrawlConfig:
    cfg = await runtime_settings.get_all(session)
    return scheduler.CrawlConfig.from_settings(
        cfg, html_quota_ok=await html_quota_ok(request, session, cfg["html_archive_quota_gb"])
    )


async def html_quota_ok(request: Request, session, quota_gb: float) -> bool:
    """Is the HTML archive below its quota? Cached for a few minutes per process."""
    cache = request.app.state.__dict__.setdefault("html_archive", {"at": 0.0, "bytes": 0})
    if time.monotonic() - cache["at"] > HTML_SIZE_CACHE_S:
        total = (
            await session.execute(
                select(func.coalesce(func.sum(RawObject.size_bytes), 0)).where(
                    RawObject.kind == "html"
                )
            )
        ).scalar_one()
        cache.update(at=time.monotonic(), bytes=int(total))
    return cache["bytes"] < quota_gb * 1024**3


@router.post("/extend", response_model=api.ExtendResponse)
async def extend(body: api.ExtendRequest, node: CurrentNode, session: Session):
    lease_s = await runtime_settings.get(session, "job_lease_s")
    leases = [(ref.job_id, ref.attempt) for ref in body.leases]
    extended = await queue.extend(session, node.id, leases, lease_s)
    await session.commit()
    kept = set(extended)
    return api.ExtendResponse(
        extended=extended,
        lost=[ref.job_id for ref in body.leases if ref.job_id not in kept],
        lease_seconds=lease_s,
    )


@router.post("/results", response_model=api.ResultAck)
async def results(body: api.ResultBatch, request: Request, node: CurrentNode, session: Session):
    months = {partitions.month_of(f.fetched_at) for r in body.results for f in r.fetches}
    if months:
        await request.app.state.partitions.ensure_months(session, months)
    types = dict(
        (
            await session.execute(
                select(Job.id, Job.type).where(Job.id.in_([r.job_id for r in body.results]))
            )
        ).all()
    )
    results = [feedback.reinterpret(types.get(r.job_id, ""), r) for r in body.results]
    applied: dict = {}
    accepted, rejected = await queue.complete(session, node.id, results, applied)
    if any(j.type in scheduler.FETCH_TYPES for j in applied.values()):
        cfg = await crawl_config(request, session)
        stored = [r for r in results if r.job_id in applied]
        await feedback.apply(session, node, applied, stored, cfg)
    await session.commit()
    return api.ResultAck(accepted=accepted, rejected=rejected)
