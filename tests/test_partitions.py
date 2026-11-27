import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select, text, update

from pos_coordinator import maintenance, partitions, queue
from pos_coordinator.models import Job, Node, RawFetch
from pos_shared.ids import uuid7


async def _partition_of(s, fetch_id) -> str:
    return (
        await s.execute(
            text("SELECT tableoid::regclass::text FROM raw_fetch WHERE id = :i"), {"i": fetch_id}
        )
    ).scalar_one()


async def test_rows_in_default_partition_are_moved_when_their_month_is_created(sessionmaker):
    """Codex finding: a row in raw_fetch_default must not make creating its month fail."""
    ts = datetime(2031, 5, 17, 12, tzinfo=UTC)
    fid = uuid7()
    async with sessionmaker() as s:
        s.add(RawFetch(id=fid, fetched_at=ts, url="https://x.invalid/", mode="http"))
        await s.commit()
        assert await _partition_of(s, fid) == "raw_fetch_default"

        assert await partitions.ensure(s, date(2031, 5, 1), 1) == 1
        await s.commit()
        assert await _partition_of(s, fid) == "raw_fetch_2031_05"
        assert await partitions.ensure(s, date(2031, 5, 1), 1) == 0  # idempotent


async def test_partition_bounds_are_utc_whatever_the_session_time_zone(sessionmaker):
    async with sessionmaker() as s:
        await s.execute(text("SET TIME ZONE 'Pacific/Auckland'"))
        await partitions.ensure(s, date(2032, 1, 1), 1)
        await s.commit()
    async with sessionmaker() as s:
        await s.execute(text("SET TIME ZONE 'UTC'"))
        bound = (
            await s.execute(
                text("SELECT pg_get_expr(relpartbound, oid) FROM pg_class WHERE relname = :n"),
                {"n": "raw_fetch_2032_01"},
            )
        ).scalar_one()
    assert bound == ("FOR VALUES FROM ('2032-01-01 00:00:00+00') TO ('2032-02-01 00:00:00+00')")


async def test_reaping_survives_a_failing_partition_upkeep(sessionmaker, monkeypatch):
    async with sessionmaker() as s:
        n = Node(name="n", public_key=uuid.uuid4().bytes * 2)
        s.add(n)
        await s.flush()
        (jid,) = await queue.enqueue(s, [queue.NewJob("noop", {})])
        await queue.lease(s, n.id, ["noop"], 1, 60)
        past = func.now() - timedelta(seconds=1)
        await s.execute(update(Job).values(lease_expires_at=past))
        await s.commit()

    async def broken(session):
        raise RuntimeError("partition DDL failed")

    monkeypatch.setattr(partitions, "ensure_ahead", broken)
    out = await maintenance.run_once(sessionmaker)
    assert out["requeued"] == 1
    async with sessionmaker() as s:
        assert (await s.get(Job, jid)).status == "queued"


async def test_historic_fetch_months_get_their_partition_on_demand(app, admin, tmp_path):
    from pos_shared.api import FetchRecord, JobOutcome, JobResult
    from tests.helpers import make_agent

    worker = await make_agent(app, admin, tmp_path / "w", "w")
    await admin.post("/api/admin/jobs", json={"jobs": [{"type": "noop"}]})
    (job,) = await worker.client.lease(["noop"], 1)
    fetch = FetchRecord(
        id=uuid7(),
        url="https://dataset.invalid/item",
        fetched_at=datetime(2019, 3, 4, tzinfo=UTC),
        mode="dataset",
    )
    result = JobResult(job_id=job.id, attempt=job.attempt, outcome=JobOutcome.done, fetches=[fetch])
    ack = await worker.client.push_results([result.model_dump(mode="json")])
    assert ack.accepted == [job.id]
    async with app.state.sessionmaker() as s:
        assert await _partition_of(s, fetch.id) == "raw_fetch_2019_03"
        assert (await s.execute(select(func.count()).select_from(RawFetch))).scalar_one() == 1
    await worker.client.aclose()


async def test_result_submission_needs_only_one_pooled_connection(settings, admin, tmp_path):
    """Codex finding: concurrent submissions for new months must not each wait for a
    second connection while holding one (pool exhaustion)."""
    import asyncio

    import httpx

    from pos_coordinator.app import create_app
    from pos_shared.api import FetchRecord, JobOutcome, JobResult
    from tests.helpers import make_agent

    tight = settings.model_copy(
        update={"db_pool_size": 1, "db_max_overflow": 0, "db_pool_timeout_s": 5.0}
    )
    app = create_app(tight, run_maintenance=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://coordinator",
            headers={"Authorization": admin.headers["Authorization"]},
        ) as admin2,
    ):
        workers = [await make_agent(app, admin2, tmp_path / f"w{i}", f"w{i}") for i in range(3)]
        await admin2.post("/api/admin/jobs", json={"jobs": [{"type": "noop"}] * 3})
        batches = []
        for i, w in enumerate(workers):
            (job,) = await w.client.lease(["noop"], 1)
            fetch = FetchRecord(
                id=uuid7(),
                url=f"https://x.invalid/{i}",
                mode="dataset",
                fetched_at=datetime(2015, i + 1, 1, tzinfo=UTC),
            )
            r = JobResult(
                job_id=job.id, attempt=job.attempt, outcome=JobOutcome.done, fetches=[fetch]
            )
            batches.append((w, r))
        acks = await asyncio.wait_for(
            asyncio.gather(
                *(w.client.push_results([r.model_dump(mode="json")]) for w, r in batches)
            ),
            timeout=30,
        )
        assert all(len(a.accepted) == 1 for a in acks)
        for w in workers:
            await w.client.aclose()


async def test_prune_finished_jobs_and_expired_auth_rows(sessionmaker):
    from datetime import timedelta as td

    from pos_coordinator.models import NodeToken

    async with sessionmaker() as s:
        n = Node(name="n", public_key=uuid.uuid4().bytes * 2)
        s.add(n)
        await s.flush()
        old, recent, failed_old, queued = await queue.enqueue(
            s, [queue.NewJob("noop", {}) for _ in range(4)]
        )
        long_ago = func.now() - td(days=30)
        await s.execute(update(Job).where(Job.id.in_([old, recent])).values(status="done"))
        await s.execute(update(Job).where(Job.id == old).values(finished_at=long_ago))
        await s.execute(update(Job).where(Job.id == recent).values(finished_at=func.now()))
        await s.execute(
            update(Job).where(Job.id == failed_old).values(status="failed", finished_at=long_ago)
        )
        s.add(
            NodeToken(
                token_hash=b"x" * 32, node_id=n.id, expires_at=datetime(2020, 1, 1, tzinfo=UTC)
            )
        )
        await s.commit()
        assert await maintenance.prune(s) == 2  # the old done job and the expired token
        await s.commit()
        left = set((await s.execute(select(Job.id))).scalars())
    assert left == {recent, failed_old, queued}  # failed jobs are kept 60 days
