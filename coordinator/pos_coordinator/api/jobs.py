"""Job leasing and result submission for nodes."""

from fastapi import APIRouter, Depends, Request

from pos_coordinator import partitions, queue, runtime_settings
from pos_coordinator.api.deps import CurrentNode, Session, check_api_version
from pos_shared import api

router = APIRouter(prefix="/api/v1/jobs", dependencies=[Depends(check_api_version)])


@router.post("/lease", response_model=api.LeaseResponse)
async def lease(body: api.LeaseRequest, node: CurrentNode, session: Session):
    if node.desired_run_state != "running":
        return api.LeaseResponse(jobs=[])
    types = body.job_types
    if node.desired_job_types is not None:
        types = [t for t in types if t in node.desired_job_types]
    if not types:
        return api.LeaseResponse(jobs=[])
    lease_s = await runtime_settings.get(session, "job_lease_s")
    jobs = await queue.lease(session, node.id, types, body.max_jobs, lease_s)
    await session.commit()
    return api.LeaseResponse(
        jobs=[
            api.LeasedJob(
                id=j.id,
                type=j.type,
                payload=j.payload,
                attempt=j.attempts,
                lease_expires_at=j.lease_expires_at,
                lease_seconds=lease_s,
            )
            for j in jobs
        ]
    )


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
        await request.app.state.partitions.ensure_months(request.app.state.sessionmaker, months)
    accepted, rejected = await queue.complete(session, node.id, body.results)
    await session.commit()
    return api.ResultAck(accepted=accepted, rejected=rejected)
