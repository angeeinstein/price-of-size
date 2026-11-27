"""Job queue on PostgreSQL using `FOR UPDATE SKIP LOCKED` (no extra broker).

Lifecycle: queued -> leased -> done | failed (terminal) or back to queued.

Every lease starts a new *attempt* (`job.attempts`), and every result and
lease renewal quotes the attempt it belongs to. Rules for a submitted result:

* attempt older than the job's current attempt: superseded, rejected (the job
  was leased again; that attempt's result is either stored or irrelevant).
* current attempt, but a result for it is already stored (`result_attempt`):
  a replay from the node's spool, acknowledged without changes.
* current attempt from its holder, no result stored yet: applied, even if the
  lease meanwhile expired and the reaper requeued or failed the job. A late
  but genuine result is better than redoing the work, and until the job is
  leased again nobody else is working on it.

A lease that expires puts the job back in the queue (keeping the holder in
`node_id`); after `max_attempts` it fails terminally.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, tuple_, update
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
    session: AsyncSession, node_id: uuid.UUID, leases: list[tuple[uuid.UUID, int]], lease_s: int
) -> list[uuid.UUID]:
    """Renew leases given as (job_id, attempt). Returns the ids that were renewed."""
    if not leases:
        return []
    stmt = (
        update(Job)
        .where(
            tuple_(Job.id, Job.attempts).in_(leases),
            Job.node_id == node_id,
            Job.status == "leased",
        )
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
            .order_by(Job.id)  # stable lock order across concurrent batches
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars()
    jobs = {j.id: j for j in rows}

    for r in results:
        job = jobs.get(r.job_id)
        reason = _rejection(job, node_id, r)
        if reason == "replay":
            accepted.append(r.job_id)
            continue
        if reason is not None:
            rejected[str(r.job_id)] = reason
            continue
        assert job is not None
        if r.fetches:
            await _store_fetches(session, job, node_id, r)
        _apply(job, r)
        accepted.append(job.id)
    return accepted, rejected


def _rejection(job: Job | None, node_id: uuid.UUID, r: JobResult) -> str | None:
    """None if the result should be applied, "replay" if already stored, else a reason."""
    if job is None:
        return "unknown job"
    if r.attempt < job.attempts:
        return f"superseded: attempt {r.attempt}, job is at attempt {job.attempts}"
    if r.attempt > job.attempts:
        return f"unknown attempt {r.attempt}"
    if job.node_id != node_id:
        return "lease held by another node"
    if job.result_attempt == r.attempt:
        return "replay"
    if job.status == "cancelled":
        return "job cancelled"
    if job.status == "done":  # done without result_attempt cannot happen; be safe
        return "job already done"
    return None


def _apply(job: Job, r: JobResult) -> None:
    job.result_attempt = r.attempt
    job.updated_at = func.now()
    job.lease_expires_at = None
    if r.outcome == JobOutcome.done:
        job.status = "done"
        job.result = r.output
        job.last_error = None
        job.finished_at = func.now()
    elif r.retryable and job.attempts < job.max_attempts:
        job.status = "queued"
        job.last_error = r.error
        job.not_before = func.now() + _retry_delay(job.attempts)
        job.finished_at = None
    else:
        job.status = "failed"
        job.last_error = r.error
        job.result = r.output
        job.finished_at = func.now()


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
            UPDATE job SET status = 'queued', lease_expires_at = NULL, updated_at = now()
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
