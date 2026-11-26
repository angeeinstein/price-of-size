"""Worker agent against the in-process coordinator: control channel and the Phase 1 gate."""

import asyncio

import pytest
from sqlalchemy import func, select, update

from pos_coordinator import maintenance
from pos_coordinator.models import Job, RawFetch, RawObject
from tests.helpers import make_agent


async def test_desired_state_and_commands(app, admin, tmp_path):
    agent = await make_agent(app, admin, tmp_path / "a", "a")
    node_id = str(agent.identity.node_id)

    await agent.heartbeat_once()
    nodes = (await admin.get("/api/admin/nodes")).json()
    assert nodes[0]["online"] and nodes[0]["actual_state"] == "running"
    assert nodes[0]["egress_group"] == "127.0.0.1"

    # pause: the node stops leasing
    await admin.patch(f"/api/admin/nodes/{node_id}", json={"run_state": "paused"})
    await admin.post("/api/admin/jobs", json={"jobs": [{"type": "noop"}]})
    await agent.heartbeat_once()
    assert agent.state.value == "paused"
    assert await agent.lease_once() == 0

    # remote limit caps concurrency
    await admin.patch(
        f"/api/admin/nodes/{node_id}",
        json={"run_state": "running", "limits": {"max_http_slots": 1}},
    )
    await agent.heartbeat_once()
    assert agent.slots.http == 1

    # one-off command: runs once, acknowledged on the next heartbeat
    r = await admin.post(f"/api/admin/nodes/{node_id}/commands", json={"type": "diagnostics"})
    cmd_id = r.json()["id"]
    await agent.heartbeat_once()  # delivers and runs
    await agent.heartbeat_once()  # reports the result
    await agent.heartbeat_once()  # must not run again
    cmds = (await admin.get(f"/api/admin/nodes/{node_id}/commands")).json()
    assert cmds[0]["id"] == cmd_id and cmds[0]["ok"] is True
    assert cmds[0]["result"]["output"]["slots"]["http"] == 1

    r = await admin.post(f"/api/admin/nodes/{node_id}/commands", json={"type": "self_destruct"})
    await agent.heartbeat_once()
    await agent.heartbeat_once()
    cmds = (await admin.get(f"/api/admin/nodes/{node_id}/commands")).json()
    assert cmds[0]["ok"] is False
    await agent.client.aclose()


async def test_results_survive_coordinator_outage(app, admin, tmp_path, monkeypatch):
    agent = await make_agent(app, admin, tmp_path / "a", "a")
    await admin.post("/api/admin/jobs", json={"jobs": [{"type": "echo", "payload": {"i": 1}}]})
    assert await agent.lease_once() == 1
    await asyncio.gather(*agent.active.values())
    assert agent.spool.result_count() == 1

    async def down(*a, **kw):
        raise ConnectionError("coordinator unreachable")

    monkeypatch.setattr(agent.client, "push_results", down)
    with pytest.raises(ConnectionError):
        await agent.flush_once()
    assert agent.spool.result_count() == 1  # still spooled
    monkeypatch.undo()
    assert await agent.flush_once() == 1
    assert agent.spool.result_count() == 0
    stats = (await admin.get("/api/admin/jobs/stats")).json()
    assert stats == [{"type": "echo", "status": "done", "count": 1}]
    await agent.client.aclose()


async def _wait_until(predicate, timeout=30.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_gate_second_worker_joins_and_leaves_without_data_loss(
    app, admin, sessionmaker, tmp_path
):
    """Phase 1 gate: jobs run end to end on dummy data; a second worker joins
    and leaves (abruptly, without draining) and no job is lost."""
    n_jobs = 300
    jobs = [
        {"type": "dummy_fetch", "payload": {"url": f"https://shop.invalid/p/{i}"}}
        if i % 3 == 0
        else {"type": "sleep", "payload": {"seconds": 0.01}}
        for i in range(n_jobs)
    ]
    await admin.post("/api/admin/jobs", json={"jobs": jobs})

    a = await make_agent(app, admin, tmp_path / "a", "a", max_http_slots=4)
    b = await make_agent(app, admin, tmp_path / "b", "b", max_http_slots=8)
    task_a = asyncio.create_task(a.run(drain_timeout_s=5))

    async def done_count():
        async with sessionmaker() as s:
            return (await s.execute(select(func.count()).where(Job.status == "done"))).scalar_one()

    assert await _wait_until(lambda: _ge(done_count, 30))
    task_b = asyncio.create_task(b.run(drain_timeout_s=5))  # B joins mid-run
    assert await _wait_until(lambda: _b_has_work(b))

    # B dies abruptly: no drain, no final flush; its leased jobs and spooled
    # results are simply gone from the coordinator's point of view.
    task_b.cancel()
    await asyncio.gather(task_b, return_exceptions=True)

    # Its leases expire; the reaper puts the jobs back for A.
    async with sessionmaker() as s:
        await s.execute(
            update(Job)
            .where(Job.status == "leased", Job.node_id == b.identity.node_id)
            .values(lease_expires_at=Job.created_at)
        )
        await s.commit()
    await maintenance.run_once(sessionmaker)

    assert await _wait_until(lambda: _ge(done_count, n_jobs), timeout=60)
    a.stop()
    assert await task_a == 0

    async with sessionmaker() as s:
        by_status = dict(
            (await s.execute(select(Job.status, func.count()).group_by(Job.status))).all()
        )
        fetch_jobs = (
            await s.execute(
                select(func.count()).where(Job.type == "dummy_fetch", Job.status == "done")
            )
        ).scalar_one()
        fetch_rows = (
            await s.execute(
                select(func.count(func.distinct(RawFetch.url))).where(
                    RawFetch.job_id.in_(select(Job.id).where(Job.type == "dummy_fetch"))
                )
            )
        ).scalar_one()
        raw_objects = (await s.execute(select(func.count()).select_from(RawObject))).scalar_one()
    assert by_status == {"done": n_jobs}
    assert fetch_jobs == fetch_rows == n_jobs // 3
    assert raw_objects == n_jobs // 3
    store = app.state.rawstore
    async with sessionmaker() as s:
        sha = (await s.execute(select(RawFetch.blob_sha256).limit(1))).scalar_one()
    assert store.exists("snippets", sha)


async def _ge(counter, n):
    return await counter() >= n


async def _b_has_work(b):
    return bool(b.active) or b.spool.result_count() > 0
