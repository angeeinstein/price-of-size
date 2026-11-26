"""Job queue on PostgreSQL using `FOR UPDATE SKIP LOCKED` (no extra broker).

Lifecycle: queued -> leased -> done | failed (terminal) or back to queued.
A lease that expires (node vanished) puts the job back in the queue; after
`max_attempts` it fails terminally. Results are accepted only from the node
that holds the lease, and a repeated identical result is acknowledged again,
so a node replaying its offline spool is harmless.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator.models import Job, RawFetch
from pos_shared.api import JobOutcome, JobResult
from pos_shared.ids import uuid7

RETRY_BASE_S = 30
RETRY_MAX_S = 3600


@dataclass
class NewJob:
    type: str
    payload: dict[str, Any]
    priority: int = 0
    max_attempts: int = 5
    domain: str | None = None
    not_before: datetime | None = None


async def enqueue(session: AsyncSession, jobs: list[NewJob]) -> list[uuid.UUID]:
    if not jobs:
        return []
    rows = []
    for j in jobs:
        row = {
            "id": uuid7(),
            "type": j.type,
            "payload": j.payload,
            "priority": j.priority,
            "max_attempts": j.max_attempts,
            "domain": j.domain,
        }
        if j.not_before is not None:
            row["not_before"] = j.not_before
        rows.append(row)
    await session.execute(pg_insert(Job), rows)
    return [r["id"] for r in rows]


async def lease(
    session: AsyncSession, node_id: uuid.UUID, job_types: list[str], max_jobs: int, lease_s: int
) -> list[Job]:
    picked = (
        select(Job.id)
        .where(Job.status == "queued", Job.type.in_(job_types), Job.not_before <= func.now())
        .order_by(Job.priority.desc(), Job.id)
        .limit(max_jobs)
        .with_for_update(skip_locked=True)
        .cte("picked")
    )
    # UPDATE ... FROM picked: the locking subquery is evaluated exactly once.
    stmt = (
        update(Job)
        .where(Job.id == picked.c.id)
        .values(
            status="leased",
            node_id=node_id,
            attempts=Job.attempts + 1,
            lease_expires_at=func.now() + timedelta(seconds=lease_s),
            updated_at=func.now(),
        )
        .returning(Job)
    )
    result = await session.execute(
        stmt, execution_options={"synchronize_session": False, "populate_existing": True}
    )
    return sorted(result.scalars().all(), key=lambda j: (-j.priority, j.id))


async def extend(
    session: AsyncSession, node_id: uuid.UUID, job_ids: list[uuid.UUID], lease_s: int
) -> list[uuid.UUID]:
    if not job_ids:
        return []
    stmt = (
        update(Job)
        .where(Job.id.in_(job_ids), Job.node_id == node_id, Job.status == "leased")
        .values(lease_expires_at=func.now() + timedelta(seconds=lease_s), updated_at=func.now())
        .returning(Job.id)
    )
    return list((await session.execute(stmt)).scalars().all())


def _retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(RETRY_BASE_S * 2 ** max(attempts - 1, 0), RETRY_MAX_S))


async def complete(
    session: AsyncSession, node_id: uuid.UUID, results: list[JobResult]
) -> tuple[list[uuid.UUID], dict[str, str]]:
    """Apply a batch of results. Returns (accepted ids, {job_id: rejection reason})."""
    accepted: list[uuid.UUID] = []
    rejected: dict[str, str] = {}
    ids = [r.job_id for r in results]
    rows = (
        await session.execute(
            select(Job)
            .where(Job.id.in_(ids))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars()
    jobs = {j.id: j for j in rows}

    for r in results:
        job = jobs.get(r.job_id)
        if job is None:
            rejected[str(r.job_id)] = "unknown job"
            continue
        if job.node_id != node_id:
            rejected[str(r.job_id)] = "lease held by another node"
            continue
        if job.status in ("done", "failed", "cancelled"):
            # Replay of a result we already stored (e.g. from the offline spool).
            accepted.append(job.id)
            continue
        if job.status != "leased":
            rejected[str(r.job_id)] = f"job is {job.status}"
            continue

        if r.fetches:
            await _store_fetches(session, job, node_id, r)

        job.updated_at = func.now()
        if r.outcome == JobOutcome.done:
            job.status = "done"
            job.result = r.output
            job.finished_at = func.now()
            job.lease_expires_at = None
        elif r.retryable and job.attempts < job.max_attempts:
            job.status = "queued"
            job.last_error = r.error
            job.node_id = None
            job.lease_expires_at = None
            job.not_before = func.now() + _retry_delay(job.attempts)
        else:
            job.status = "failed"
            job.last_error = r.error
            job.result = r.output
            job.finished_at = func.now()
            job.lease_expires_at = None
        accepted.append(job.id)
    return accepted, rejected


async def _store_fetches(session: AsyncSession, job: Job, node_id: uuid.UUID, r: JobResult):
    rows = [{**f.model_dump(), "job_id": job.id, "node_id": node_id} for f in r.fetches]
    # Fetch ids are generated by the node, so a replayed batch must not duplicate them.
    stmt = (
        pg_insert(RawFetch).values(rows).on_conflict_do_nothing(index_elements=["id", "fetched_at"])
    )
    await session.execute(stmt)


async def reap_expired(session: AsyncSession) -> tuple[int, int]:
    """Requeue jobs whose lease expired; fail those out of attempts. Returns (requeued, failed)."""
    failed = await session.execute(
        text(
            """
            UPDATE job SET status = 'failed', finished_at = now(), updated_at = now(),
                   lease_expires_at = NULL,
                   last_error = coalesce(last_error, '') || ' [lease expired, out of attempts]'
            WHERE status = 'leased' AND lease_expires_at < now() AND attempts >= max_attempts
            """
        )
    )
    requeued = await session.execute(
        text(
            """
            UPDATE job SET status = 'queued', node_id = NULL, lease_expires_at = NULL,
                   updated_at = now()
            WHERE status = 'leased' AND lease_expires_at < now()
            """
        )
    )
    return requeued.rowcount, failed.rowcount


async def release_node(session: AsyncSession, node_id: uuid.UUID) -> int:
    """Return all jobs leased by a node to the queue (node revoked or drained)."""
    res = await session.execute(
        update(Job)
        .where(Job.node_id == node_id, Job.status == "leased")
        .values(status="queued", node_id=None, lease_expires_at=None, updated_at=func.now())
    )
    return res.rowcount


async def queue_stats(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(Job.type, Job.status, func.count()).group_by(Job.type, Job.status)
    )
    return [{"type": t, "status": s, "count": c} for t, s, c in rows.all()]
