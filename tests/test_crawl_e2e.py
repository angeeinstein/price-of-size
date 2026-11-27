"""End to end: coordinator + real worker + real HTTP against a local fake shop."""

import asyncio
import json
from datetime import UTC, datetime

import pytest
import zstandard
from sqlalchemy import func, select

from pos_coordinator.models import DomainEgressState, DomainEvent, DomainState, Job, RawFetch
from tests.fake_shop import FakeShop
from tests.helpers import make_agent


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")


@pytest.fixture
def shop():
    with FakeShop() as s:
        yield s


async def _fast_rates(admin):
    for k, v in (("base_rate_rps", 10), ("max_rate_rps", 10)):
        assert (await admin.put(f"/api/admin/settings/{k}", json={"value": v})).status_code == 200


async def _run_until(worker, predicate, timeout=30.0):
    task = asyncio.create_task(worker.run(drain_timeout_s=5))
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if await predicate():
                return
            await asyncio.sleep(0.1)
        raise TimeoutError("condition not reached")
    finally:
        worker.stop()
        await task


def _statuses(sm, domain):
    async def get():
        async with sm() as s:
            return dict(
                (
                    await s.execute(
                        select(Job.status, func.count())
                        .where(Job.domain == domain)
                        .group_by(Job.status)
                    )
                ).all()
            )

    return get


async def test_polite_crawl_with_robots_and_evidence(app, admin, sessionmaker, shop, tmp_path):
    await _fast_rates(admin)
    pages = [shop.url("127.0.0.1", f"/p/{i}") for i in range(1, 6)]
    r = await admin.post(
        "/api/admin/crawl/urls",
        json={"urls": [*pages, shop.url("127.0.0.1", "/private/7")], "source": "fake-shop"},
    )
    assert r.json()["queued"] == 6

    worker = await make_agent(app, admin, tmp_path / "w", "w")
    stats = _statuses(sessionmaker, "127.0.0.1")

    async def finished():
        st = await stats()
        return st.get("done", 0) == 6 and st.get("failed", 0) == 1  # 5 pages + robots, 1 private

    await _run_until(worker, finished)

    paths = shop.paths("127.0.0.1")
    assert paths[0] == "/robots.txt"
    assert not any(p.startswith("/private") for p in paths)  # never requested
    times = shop.times("127.0.0.1")
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    # Crawl-delay 0.2 s, not the 10/s setting; even the first page after robots.txt waits
    assert min(gaps) >= 0.15, gaps

    async with sessionmaker() as s:
        d = await s.get(DomainState, "127.0.0.1")
        assert (d.robots_status, d.robots_crawl_delay_s, d.pages_fetched) == ("rules", 0.2, 5)
        private = (await s.execute(select(Job).where(Job.status == "failed"))).scalar_one()
        assert private.last_error == "robots_disallowed"
        fetches = (
            (
                await s.execute(
                    select(RawFetch).where(RawFetch.source == "fake-shop").order_by(RawFetch.url)
                )
            )
            .scalars()
            .all()
        )
    assert len(fetches) == 5 and all(f.status == 200 and f.blob_sha256 for f in fetches)
    assert all(f.html_sha256 for f in fetches)  # first pages of a domain: HTML archived

    store = app.state.rawstore
    evidence = json.loads(
        zstandard.ZstdDecompressor().decompress(store.get("snippets", fetches[0].blob_sha256))
    )
    assert evidence["status"] == 200 and evidence["verdict"]["kind"] == "ok"
    ld = json.loads(evidence["snippets"]["json_ld"][0])
    assert ld["@type"] == "Product" and ld["offers"]["priceCurrency"] == "EUR"
    assert any("Gewicht" in t for t in evidence["snippets"]["tables"])
    html = zstandard.ZstdDecompressor().decompress(store.get("html", fetches[0].html_sha256))
    assert b"application/ld+json" in html


async def test_429_slows_down_and_honours_retry_after(app, admin, sessionmaker, shop, tmp_path):
    await _fast_rates(admin)
    await admin.post("/api/admin/crawl/urls", json={"urls": [shop.url("127.0.0.2", "/limited")]})
    worker = await make_agent(app, admin, tmp_path / "w", "w")

    async def throttled():
        async with sessionmaker() as s:
            return (
                await s.execute(
                    select(func.count())
                    .select_from(DomainEvent)
                    .where(DomainEvent.domain == "127.0.0.2", DomainEvent.kind == "rate_down")
                )
            ).scalar_one()

    await _run_until(worker, throttled)
    async with sessionmaker() as s:
        eg = (
            await s.execute(
                select(DomainEgressState).where(DomainEgressState.domain == "127.0.0.2")
            )
        ).scalar_one()
        job = (await s.execute(select(Job).where(Job.type == "fetch_http"))).scalar_one()
    assert eg.rate_rps == 5.0  # halved
    assert (eg.cooldown_until - datetime.now(UTC)).total_seconds() > 25  # Retry-After: 30
    assert job.status == "queued" and job.last_error == "throttled:http_429"
    assert job.not_before > datetime.now(UTC)  # retried later, not hammered
    assert shop.paths("127.0.0.2").count("/limited") == 1


async def test_captcha_blocks_the_domain(app, admin, sessionmaker, shop, tmp_path):
    await _fast_rates(admin)
    await admin.post(
        "/api/admin/crawl/urls", json={"urls": [shop.url("127.0.0.3", "/captcha")], "priority": 10}
    )
    await admin.post(
        "/api/admin/crawl/urls", json={"urls": [shop.url("127.0.0.3", f"/p/{i}") for i in range(3)]}
    )
    worker = await make_agent(app, admin, tmp_path / "w", "w")

    async def blocked():
        async with sessionmaker() as s:
            return (await s.get(DomainState, "127.0.0.3")).block_status == "blocked"

    await _run_until(worker, blocked)
    await asyncio.sleep(0.5)
    async with sessionmaker() as s:
        d = await s.get(DomainState, "127.0.0.3")
        queued = (
            await s.execute(
                select(func.count()).where(Job.domain == "127.0.0.3", Job.status == "queued")
            )
        ).scalar_one()
    assert d.block_reason == "captcha:recaptcha"
    assert queued >= 3  # the product pages wait; no evasion, no further requests
    assert [p for p in shop.paths("127.0.0.3") if p.startswith("/p/")] == []
    domains = (await admin.get("/api/admin/domains", params={"blocked": True})).json()
    assert [x["domain"] for x in domains] == ["127.0.0.3"]
    events = (await admin.get("/api/admin/domains/127.0.0.3/events")).json()
    assert events[0]["kind"] == "blocked"

    r = await admin.patch("/api/admin/domains/127.0.0.3", json={"unblock": True})
    assert r.json()["block_status"] == "ok"
