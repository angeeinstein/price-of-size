import asyncio
import uuid

from sqlalchemy import select, update

from pos_coordinator import queue
from pos_coordinator.models import Job, Node
from pos_shared.api import JobOutcome, JobResult


async def _node(session, name="n") -> uuid.UUID:
    n = Node(name=name, public_key=uuid.uuid4().bytes + uuid.uuid4().bytes)
    session.add(n)
    await session.flush()
    return n.id


async def test_lease_respects_priority_and_type(sessionmaker):
    async with sessionmaker() as s:
        nid = await _node(s)
        low = await queue.enqueue(s, [queue.NewJob("noop", {"i": i}) for i in range(3)])
        high = await queue.enqueue(s, [queue.NewJob("noop", {"hi": True}, priority=10)])
        await queue.enqueue(s, [queue.NewJob("other", {})])
        await s.commit()
        jobs = await queue.lease(s, nid, ["noop"], 10, 60)
        await s.commit()
    assert [j.id for j in jobs] == high + low
    assert all(j.status == "leased" and j.attempts == 1 for j in jobs)


async def test_concurrent_leases_never_hand_out_a_job_twice(sessionmaker):
    async with sessionmaker() as s:
        nodes = [await _node(s, f"n{i}") for i in range(8)]
        await queue.enqueue(s, [queue.NewJob("noop", {}) for _ in range(400)])
        await s.commit()

    async def grab(nid):
        got = []
        while True:
            async with sessionmaker() as s:
                jobs = await queue.lease(s, nid, ["noop"], 7, 60)
                await s.commit()
            if not jobs:
                return got
            got += [j.id for j in jobs]

    results = await asyncio.gather(*(grab(n) for n in nodes))
    flat = [j for r in results for j in r]
    assert len(flat) == 400
    assert len(set(flat)) == 400


async def test_complete_retry_and_terminal_failure(sessionmaker):
    async with sessionmaker() as s:
        nid = await _node(s)
        (jid,) = await queue.enqueue(s, [queue.NewJob("noop", {}, max_attempts=2)])
        await s.commit()

        await queue.lease(s, nid, ["noop"], 1, 60)
        res = JobResult(job_id=jid, outcome=JobOutcome.failed, error="boom")
        accepted, rejected = await queue.complete(s, nid, [res])
        await s.commit()
        job = await s.get(Job, jid)
        assert accepted == [jid] and not rejected
        assert job.status == "queued" and job.node_id is None
        # retry is delayed (back-off); make it due now
        await s.execute(update(Job).where(Job.id == jid).values(not_before=Job.created_at))
        await s.commit()

        assert len(await queue.lease(s, nid, ["noop"], 1, 60)) == 1
        await queue.complete(s, nid, [res])
        await s.commit()
        await s.refresh(job)
        assert job.status == "failed" and job.attempts == 2 and job.last_error == "boom"


async def test_results_only_from_lease_holder_and_replay_is_idempotent(sessionmaker):
    async with sessionmaker() as s:
        a, b = await _node(s, "a"), await _node(s, "b")
        (jid,) = await queue.enqueue(s, [queue.NewJob("noop", {})])
        await s.commit()
        await queue.lease(s, a, ["noop"], 1, 60)
        await s.commit()
        ok = JobResult(job_id=jid, outcome=JobOutcome.done, output={"v": 1})

        _, rejected = await queue.complete(s, b, [ok])
        assert str(jid) in rejected
        accepted, _ = await queue.complete(s, a, [ok])
        await s.commit()
        assert accepted == [jid]
        accepted, rejected = await queue.complete(s, a, [ok])  # spool replay
        assert accepted == [jid] and not rejected
        unknown = JobResult(job_id=uuid.uuid4(), outcome=JobOutcome.done)
        _, rejected = await queue.complete(s, a, [unknown])
        assert rejected == {str(unknown.job_id): "unknown job"}
        job = await s.get(Job, jid)
        assert job.status == "done" and job.result == {"v": 1}


async def test_reaper_requeues_expired_leases(sessionmaker):
    async with sessionmaker() as s:
        nid = await _node(s)
        ids = await queue.enqueue(
            s, [queue.NewJob("noop", {}), queue.NewJob("noop", {}, max_attempts=1)]
        )
        await s.commit()
        await queue.lease(s, nid, ["noop"], 10, 60)
        await s.execute(update(Job).where(Job.id.in_(ids)).values(lease_expires_at=Job.created_at))
        requeued, failed = await queue.reap_expired(s)
        await s.commit()
        assert (requeued, failed) == (1, 1)
        statuses = dict((await s.execute(select(Job.id, Job.status))).all())
        assert statuses == {ids[0]: "queued", ids[1]: "failed"}


async def test_extend_only_own_leases(sessionmaker):
    async with sessionmaker() as s:
        a, b = await _node(s, "a"), await _node(s, "b")
        (jid,) = await queue.enqueue(s, [queue.NewJob("noop", {})])
        await queue.lease(s, a, ["noop"], 1, 60)
        assert await queue.extend(s, b, [jid], 600) == []
        assert await queue.extend(s, a, [jid], 600) == [jid]
