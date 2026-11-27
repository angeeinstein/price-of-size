"""Regressions from the independent review of the crawl code (each was reproduced)."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from pos_coordinator import queue, runtime_settings
from pos_coordinator.crawl import discovery, feedback, ratecontrol, robots, scheduler, urls
from pos_coordinator.models import DomainEgressState, DomainEvent, DomainState, Job, Node
from pos_shared.api import JobOutcome, JobResult
from pos_worker.fetch import blocks
from pos_worker.fetch import handlers as fetch_handlers

# --- pure functions ----------------------------------------------------------------


@pytest.mark.parametrize("bad", ["https://shop:99999/p", "http://[oops/p", "https://" + "a" * 9000])
def test_malformed_urls_raise_only_badurl(bad):
    with pytest.raises(urls.BadUrl):
        urls.normalize(bad)
    assert urls.host_of(bad) is None or isinstance(urls.host_of(bad), str)


def test_floor_never_beats_crawl_delay():
    top = ratecontrol.ceiling(2.0, 300.0, None)  # Crawl-delay: 300
    assert ratecontrol.clamp(0.0001, top) == pytest.approx(1 / 300)
    s = ratecontrol.RateState(rate_rps=1 / 300)
    d = ratecontrol.observe(s, "throttled", "http_429", 100, None, top)
    assert d.rate_rps <= 1 / 300 + 1e-12  # halving clamps at the floor, the ceiling wins


def test_one_challenge_page_is_a_suspicion_two_are_a_block():
    s = ratecontrol.RateState(rate_rps=1.0)
    first = ratecontrol.observe(s, "blocked", "captcha:recaptcha", 100, None, 2.0)
    assert first.event == "block_suspected" and first.cooldown_s == ratecontrol.SUSPECT_PAUSE_S
    ratecontrol.observe(s, "ok", None, 100, None, 2.0)  # a clean answer in between
    assert ratecontrol.observe(s, "blocked", "x", 100, None, 2.0).event == "block_suspected"
    assert ratecontrol.observe(s, "blocked", "x", 100, None, 2.0).event == "blocked"


def test_widgets_on_ordinary_pages_are_not_blocks():
    page = '<html><div class="g-recaptcha">' + "<p>Regal 80 x 40 cm, 49 €</p>" * 2000
    assert blocks.classify(200, page[:50_000]).kind == "ok"  # a newsletter widget
    tag = '<html><script src="https://js.datadome.co/tags.js"></script>' + "x" * 30_000
    assert blocks.classify(200, tag).kind == "ok"
    challenge = '<html><title>Verify</title><div class="g-recaptcha"></div></html>'
    assert blocks.classify(200, challenge).kind == "blocked"
    assert blocks.classify(403, page[:50_000]).kind == "blocked"  # a refusal status
    assert blocks.classify(None, "", "blocked_address").kind == "gone"


def test_robots_product_token_matching_and_request_rate_units():
    own = "User-agent: PriceOfSizeBot/1.0\nDisallow: /\n"
    assert not robots.allowed("rules", own, "https://a.example/p")
    other = "User-agent: bot\nDisallow: /\n\nUser-agent: Size\nDisallow: /\n"
    assert robots.allowed("rules", other, "https://a.example/p")  # not our groups
    rate = "User-agent: *\nRequest-rate: 1/10m\n"
    assert robots.crawl_delay("rules", rate) == 600.0


def test_same_site_redirects():
    assert feedback.same_site("https://shop.example/p", "https://www.shop.example/p")
    assert not feedback.same_site("https://shop.example/p", "https://tracker.example/r")
    assert not feedback.same_site("https://shop.example/p", "http://[oops/")


async def test_worker_spaces_requests_after_the_limiter():
    """Overdue jobs for one domain must not burst out when the limiter frees up."""
    sent = []

    async def fake_fetch(client, url, max_bytes):
        sent.append(asyncio.get_running_loop().time())
        return fetch_handlers.http.Response(url=url, status=200)

    class Job:
        def __init__(self, i):
            self.payload = {"url": f"https://a.example/{i}", "delay_s": 0, "interval_s": 0.3}

    orig = fetch_handlers.http.fetch
    fetch_handlers.http.fetch = lambda c, u, max_bytes=0: fake_fetch(c, u, max_bytes)
    try:
        await asyncio.gather(
            *(fetch_handlers._gated_fetch(Job(i), Job(i).payload["url"], 10) for i in range(3))
        )
    finally:
        fetch_handlers.http.fetch = orig
    gaps = [b - a for a, b in zip(sent, sent[1:], strict=False)]
    assert len(sent) == 3 and all(g >= fetch_handlers.MIN_INTERVAL_S - 0.05 for g in gaps)


# --- with the database -------------------------------------------------------------


async def _node(s, group="1.1.1.1") -> Node:
    n = Node(name=group, public_key=uuid.uuid4().bytes * 2, egress_group=group)
    s.add(n)
    await s.flush()
    return n


async def _cfg(s, **overrides) -> scheduler.CrawlConfig:
    cfg = await runtime_settings.get_all(s)
    cfg.update({"base_rate_rps": 1.0, **overrides})
    return scheduler.CrawlConfig.from_settings(cfg, html_quota_ok=True)


async def _ready(s, domain, robots_txt=None, pages_fetched=scheduler.SLOW_START_PAGES):
    await s.execute(
        update(DomainState)
        .where(DomainState.domain == domain)
        .values(
            robots_status="rules" if robots_txt else "none",
            robots_txt=robots_txt,
            robots_crawl_delay_s=robots.crawl_delay("rules", robots_txt) if robots_txt else None,
            robots_next_check_at=func.now() + timedelta(days=1),
            pages_fetched=pages_fetched,
        )
    )
    await s.execute(
        update(Job).where(Job.type == "fetch_robots", Job.domain == domain).values(status="done")
    )


async def test_crawl_delay_holds_across_egress_groups(sessionmaker):
    async with sessionmaker() as s:
        await scheduler.enqueue_urls(s, [f"https://slow.example/p/{i}" for i in range(6)], "t")
        await _ready(s, "slow.example", "User-agent: *\nCrawl-delay: 30\n")
        cfg = await _cfg(s, max_rate_rps=10, base_rate_rps=10)
        nodes = [await _node(s, f"10.0.0.{i}") for i in range(3)]
        got = []
        for n in nodes:
            got += await scheduler.lease_fetch(s, n, ["fetch_http"], 10, cfg)
        delays = sorted(h.delay_s for h in got)
        assert 2 <= len(got) <= 3  # 60 s of lookahead, slots >= 30 s apart, site-wide
        assert all(b - a >= 29.9 for a, b in zip(delays, delays[1:], strict=False))
        assert all(h.extra["interval_s"] >= 30 for h in got)


async def test_waiting_domains_do_not_starve_ready_ones(sessionmaker):
    async with sessionmaker() as s:
        n = await _node(s)
        cfg = await _cfg(s)
        # many new domains whose robots.txt is in flight (leased): nothing to hand out
        for i in range(60):
            await scheduler.enqueue_urls(s, [f"https://new{i:02d}.example/"], "t")
        await s.execute(update(Job).where(Job.type == "fetch_robots").values(status="leased"))
        await scheduler.enqueue_urls(s, [f"https://zzz.example/p/{i}" for i in range(5)], "t")
        await _ready(s, "zzz.example")
        got = []
        for _ in range(5):  # a few lease rounds, as workers poll
            got += await scheduler.lease_fetch(s, n, ["fetch_http", "fetch_robots"], 1, cfg)
            if got:
                break
        assert got and got[0].job.domain == "zzz.example"


async def test_sitemap_cycles_end(sessionmaker, tmp_path):
    async with sessionmaker() as s:
        index = "https://shop.example/sitemap.xml"
        await scheduler.enqueue_urls(s, [index], "t", job_type="fetch_sitemap")
        job = (await s.execute(select(Job).where(Job.type == "fetch_sitemap"))).scalar_one()
        job.status, job.finished_at = "done", func.now()
        await s.flush()
        evidence = {"sitemaps": [index, "https://shop.example/b.xml", "http://[bad/"], "urls": []}
        await discovery._follow(s, job, evidence, None)
        queued = (
            await s.execute(
                select(Job.payload["url"].astext, Job.payload["depth"].astext).where(
                    Job.type == "fetch_sitemap", Job.status == "queued"
                )
            )
        ).all()
        assert queued == [("https://shop.example/b.xml", "1")]  # not itself, not the bad one
        deep = Job(
            type="fetch_sitemap",
            payload={"url": "https://shop.example/c.xml", "depth": discovery.MAX_SITEMAP_DEPTH},
            domain="shop.example",
        )
        await discovery._follow(s, deep, {"sitemaps": ["https://shop.example/d.xml"]}, None)
        urls_now = set(
            (await s.execute(select(Job.payload["url"].astext).where(Job.status == "queued")))
            .scalars()
            .all()
        )
        assert "https://shop.example/d.xml" not in urls_now


async def test_skipped_jobs_do_not_use_up_attempts(sessionmaker):
    async with sessionmaker() as s:
        n = await _node(s)
        await scheduler.enqueue_urls(s, ["https://a.example/p"], "t")
        await _ready(s, "a.example")
        job = (await s.execute(select(Job).where(Job.type == "fetch_http"))).scalar_one()
        budget = job.max_attempts
        for _ in range(job.max_attempts + 2):
            job.status, job.node_id, job.attempts = "leased", n.id, job.attempts + 1
            await s.flush()
            r = JobResult(
                job_id=job.id,
                attempt=job.attempts,
                outcome=JobOutcome.failed,
                error="skipped:domain_paused:a.example",
            )
            await queue.complete(s, n.id, [r], {})
            await s.flush()
        assert job.status == "queued" and job.max_attempts - job.attempts == budget


async def test_feedback_survives_an_egress_change_and_bad_output(sessionmaker):
    async with sessionmaker() as s:
        n = await _node(s, "2.2.2.2")
        cfg = await _cfg(s)
        await scheduler.enqueue_urls(s, ["https://a.example/p", "https://a.example/q"], "t")
        await _ready(s, "a.example")
        jobs = (await s.execute(select(Job).where(Job.type == "fetch_http"))).scalars().all()
        ok = JobResult(
            job_id=jobs[0].id,
            attempt=1,
            outcome=JobOutcome.done,
            output={"verdict": "throttled", "reason": "http_429", "retry_after_s": 120},
        )
        bad = JobResult(
            job_id=jobs[1].id,
            attempt=1,
            outcome=JobOutcome.done,
            output={"verdict": "ok", "elapsed_ms": "not a number"},
        )
        # no egress row for 2.2.2.2 exists yet (the node's IP changed after the lease)
        await feedback.apply(s, n, {j.id: j for j in jobs}, [bad, ok], cfg)
        eg = await s.get(DomainEgressState, ("a.example", "2.2.2.2"))
        assert eg is not None and eg.cooldown_until is not None  # the 429 counted
        kinds = (await s.execute(select(DomainEvent.kind))).scalars().all()
        assert "rate_down" in kinds


async def test_cross_host_redirect_goes_through_the_scheduler(sessionmaker):
    async with sessionmaker() as s:
        n = await _node(s)
        cfg = await _cfg(s)
        await scheduler.enqueue_urls(s, ["https://shop.example/p"], "t")
        job = (await s.execute(select(Job).where(Job.type == "fetch_http"))).scalar_one()
        out = {"verdict": "ok", "status": 301, "redirect_to": "https://www.shop.example/p"}
        r = JobResult(job_id=job.id, attempt=1, outcome=JobOutcome.done, output=out)
        await feedback.apply(s, n, {job.id: job}, [r], cfg)
        moved = (
            await s.execute(
                select(Job).where(Job.domain == "www.shop.example", Job.type == "fetch_http")
            )
        ).scalar_one()
        assert moved.payload["redirect_hops"] == 1
        # a robots.txt job for the new host comes first, like for any new domain
        assert await s.get(DomainState, "www.shop.example") is not None
        away = {**out, "redirect_to": "https://tracker.example/r"}
        r2 = JobResult(job_id=moved.id, attempt=1, outcome=JobOutcome.done, output=away)
        await feedback.apply(s, n, {moved.id: moved}, [r2], cfg)
        assert await s.get(DomainState, "tracker.example") is None


# --- Codex findings ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("rules", "path", "ok"),
    [
        ("Disallow: /*?secret=", "/p?secret=1", False),
        ("Disallow: /\nAllow: /products/", "/products/123", True),  # longest match wins
        ("Disallow: /\nAllow: /products/", "/about", False),
        ("Disallow: /*.pdf$", "/a/b.pdf", False),
        ("Disallow: /*.pdf$", "/a/b.pdf?x=1", True),  # $ anchors the end
        ("Disallow: /fish\nAllow: /fish/salmon", "/fish/salmon.html", True),
        ("Disallow: /fish\nAllow: /fish/salmon", "/fish.html", False),
        ("Allow: /page\nDisallow: /page", "/page", True),  # tie: the less restrictive
        ("Disallow: /%7Euser", "/~user/x", False),  # percent-encoding normalised
        ("Disallow:", "/anything", True),  # empty disallow allows everything
        ("Disallow: /", "/robots.txt", True),  # always fetchable
        ("Disallow: /\nAllow: /public/", "/public%2Fprivate", False),  # %2F is not "/"
        ("Disallow: /\nAllow: /public/", "/public/x", True),
        ("Disallow: /a%2Ab", "/aXb", True),  # an encoded "*" is no wildcard
        ("Disallow: /a%2Ab", "/a%2ab", False),  # but matches itself, any hex case
    ],
)
def test_robots_rfc9309_matching(rules, path, ok):
    text = f"User-agent: *\n{rules}\n"
    assert robots.allowed("rules", text, f"https://a.example{path}") is ok


async def test_discovery_waits_for_robots_of_new_domains(sessionmaker):
    from pos_coordinator.models import Source

    async with sessionmaker() as s:
        n = await _node(s)
        cfg = await _cfg(s)
        src = Source(name="shop", type="scraper", config={"domains": ["shop.example"]})
        other = Source(name="other", type="scraper", config={"domains": ["plain.example"]})
        s.add_all([src, other])
        await s.flush()
        assert (await discovery.discover(s, src))["waiting_for_robots"] == 1
        assert (await discovery.discover(s, other))["waiting_for_robots"] == 1
        robots_jobs = {
            j.domain: j
            for j in (await s.execute(select(Job).where(Job.type == "fetch_robots"))).scalars()
        }
        assert set(robots_jobs) == {"shop.example", "plain.example"}
        with_sitemap = "User-agent: *\nAllow: /\nSitemap: https://shop.example/sm.xml\n"
        results = [
            JobResult(
                job_id=robots_jobs["shop.example"].id,
                attempt=1,
                outcome=JobOutcome.done,
                output={"verdict": "ok", "status": 200, "body": with_sitemap},
            ),
            JobResult(
                job_id=robots_jobs["plain.example"].id,
                attempt=1,
                outcome=JobOutcome.done,
                output={"verdict": "gone", "status": 404},
            ),
        ]
        await feedback.apply(s, n, {j.id: j for j in robots_jobs.values()}, results, cfg)
        sitemaps = {
            (j.payload["url"], j.payload["source"])
            for j in (await s.execute(select(Job).where(Job.type == "fetch_sitemap"))).scalars()
        }
        assert sitemaps == {
            ("https://shop.example/sm.xml", "shop"),
            ("https://plain.example/sitemap.xml", "other"),  # none named: the convention
        }
        assert (await s.get(DomainState, "shop.example")).pending_discovery is None


def test_an_empty_group_for_us_replaces_the_star_group():
    text = "User-agent: *\nDisallow: /\n\nUser-agent: PriceOfSizeBot\nDisallow:\n"
    assert robots.allowed("rules", text, "https://a.example/p/1")
    only_ua = "User-agent: *\nDisallow: /\n\nUser-agent: PriceOfSizeBot\n"
    assert robots.allowed("rules", only_ua, "https://a.example/p/1")
