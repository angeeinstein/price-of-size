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
