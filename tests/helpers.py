"""Shared test helpers."""

from pathlib import Path

import httpx

from pos_worker.agent import Agent
from pos_worker.config import WorkerSettings


async def new_enrollment_code(admin: httpx.AsyncClient, **kw) -> str:
    r = await admin.post("/api/admin/enrollment-codes", json=kw)
    assert r.status_code == 201, r.text
    return r.json()["code"]


async def make_agent(app, admin: httpx.AsyncClient, state_dir: Path, name: str, **kw) -> Agent:
    code = await new_enrollment_code(admin, label=name)
    settings = WorkerSettings(
        coordinator_url="http://coordinator",
        enrollment_code=code,
        name=name,
        state_dir=state_dir,
        flush_interval_s=0.05,
        lease_poll_s=0.05,
        resource_poll_s=3600,
        **kw,
    )
    agent = Agent(settings, transport=httpx.ASGITransport(app=app))
    await agent.ensure_enrolled()
    return agent
