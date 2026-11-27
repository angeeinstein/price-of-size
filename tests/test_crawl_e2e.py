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
    # the fake shop lives on 127.0.0.x, which real workers refuse to fetch
    monkeypatch.setenv("POS_WORKER_ALLOW_PRIVATE_TARGETS", "1")


@pytest.fixture
def shop():
    with FakeShop() as s:
        yield s


async def _fast_rates(admin):
    for k, v in (("base_rate_rps", 10), ("max_rate_rps", 10)):
        assert (await admin.put(f"/api/admin/settings/{k}", json={"value": v})).status_code == 200


_SM = {}


@pytest.fixture(autouse=True)
def _remember_sessionmaker(sessionmaker):
    _SM["sm"] = sessionmaker


async def _run_until(worker, predicate, timeout=30.0):
    task = asyncio.create_task(worker.run(drain_timeout_s=5))
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if await predicate():
                return
            if task.done():
                task.result()  # surface a crashed worker instead of timing out
            await asyncio.sleep(0.1)
        raise TimeoutError(f"condition not reached; jobs: {await _job_table(worker)}")
    finally:
        worker.stop()
        await task


async def _job_table(worker):
    rows = await worker.client._http.get("/api/v1/version")  # coordinator still answering?
    out = (
        f"coordinator={rows.status_code} active={len(worker.active)} "
        f"spooled={worker.spool.result_count()}"
    )
    import io

    for t in asyncio.all_tasks():
        if t.get_name() == "flush":
            buf = io.StringIO()
            t.print_stack(file=buf)
            out += f"\nFLUSH TASK done={t.done()} stack:\n{buf.getvalue()}"
    spooled = worker.spool.peek_results(10)
    try:
        ack = await worker.client.push_results(spooled) if spooled else None
        out += f" spool={[(r['job_id'], r['attempt'], r['outcome']) for r in spooled]} ack={ack}"
    except Exception as e:  # noqa: BLE001
        out += f" flush raised {e!r}"
    async with _SM["sm"]() as s:
        jobs = (
            await s.execute(
                select(
                    Job.type,
                    Job.status,
                    Job.domain,
                    Job.attempts,
                    Job.last_error,
                    Job.not_before,
                    Job.payload["url"].astext,
                )
            )
        ).all()
        groups = (
            await s.execute(
                select(
                    DomainEgressState.domain,
                    DomainEgressState.rate_rps,
                    DomainEgressState.next_slot_at,
                    DomainEgressState.cooldown_until,
                )
            )
        ).all()
        doms = (
            await s.execute(
                select(
                    DomainState.domain,
                    DomainState.robots_status,
                    DomainState.pages_fetched,
                    DomainState.block_status,
                )
            )
        ).all()
    return f"{out}\njobs={jobs}\ngroups={groups}\ndomains={doms}"


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

    async def suspected():
        async with sessionmaker() as s:
            kinds = (
                await s.execute(select(DomainEvent.kind).where(DomainEvent.domain == "127.0.0.3"))
            ).scalars()
            return "block_suspected" in set(kinds)

    async def blocked():
        async with sessionmaker() as s:
            return (await s.get(DomainState, "127.0.0.3")).block_status == "blocked"

    # A first challenge page pauses the domain; only a second one blocks it.
    await _run_until(worker, suspected)
    async with sessionmaker() as s:
        d = await s.get(DomainState, "127.0.0.3")
        g = (await s.execute(select(DomainEgressState))).scalars().all()
        assert d.block_status != "blocked" and all(
            x.cooldown_until for x in g if x.domain == d.domain
        )
        for x in g:  # an hour later
            x.cooldown_until = None
        for j in (await s.execute(select(Job).where(Job.domain == "127.0.0.3"))).scalars():
            j.not_before = datetime.now(UTC)
        await s.commit()
    from pos_worker.fetch import breaker

    breaker.reset()  # the worker's own pause (its process would have waited too)
    await _run_until(await make_agent(app, admin, tmp_path / "w2", "w2"), blocked)
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
    assert "block_suspected" in [e["kind"] for e in events]

    r = await admin.patch("/api/admin/domains/127.0.0.3", json={"unblock": True})
    assert r.json()["block_status"] == "ok"


async def test_discovery_through_sitemaps(app, admin, sessionmaker, shop, tmp_path):
    """robots.txt advertises a sitemap index -> gzipped product sitemap -> pages that
    match the source's pattern are fetched; other hosts and other pages are not."""
    await _fast_rates(admin)
    r = await admin.put(
        "/api/admin/sources/fake-shop",
        json={
            "config": {
                "domains": ["127.0.0.4"],
                "product_url_pattern": r"/p/\d+$",
                "seeds": [shop.url("127.0.0.4", "/")],
            }
        },
    )
    assert r.status_code == 200, r.text
    # robots.txt is not known yet: discovery fetches it first (and the seed), then queues
    # the sitemap it advertises by itself, without a second click.
    assert (await admin.post("/api/admin/sources/fake-shop/discover")).json() == {
        "sitemaps": 0,
        "seeds": 1,
        "waiting_for_robots": 1,
    }
    worker = await make_agent(app, admin, tmp_path / "w", "w")

    async def products_fetched():
        async with sessionmaker() as s:
            n = (
                await s.execute(
                    select(func.count()).where(
                        Job.type == "fetch_http",
                        Job.status == "done",
                        Job.payload["url"].astext.like("%/p/%"),
                    )
                )
            ).scalar_one()
            return n == 3

    worker = await make_agent(app, admin, tmp_path / "w2", "w2")
    await _run_until(worker, products_fetched)
    paths = shop.paths("127.0.0.4")
    assert "/sitemap_index.xml" in paths and "/sitemap-products.xml.gz" in paths
    assert {"/p/1", "/p/2", "/p/3"} <= set(paths)
    assert "/about" not in paths  # in a sitemap, but not matching the product pattern
    assert shop.paths("127.0.0.9") == []  # other hosts are never followed
    async with sessionmaker() as s:
        sources = set(
            (
                await s.execute(
                    select(Job.payload["source"].astext).where(Job.type == "fetch_http")
                )
            ).scalars()
        )
    assert sources == {"fake-shop"}
