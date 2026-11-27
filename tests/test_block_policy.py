"""The block policy is configurable: globally (runtime settings) and per domain."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from pos_coordinator import runtime_settings
from pos_coordinator.crawl import feedback, ratecontrol, scheduler
from pos_coordinator.models import DomainEgressState, DomainEvent, DomainState, Job


def _config(**settings) -> scheduler.CrawlConfig:
    return scheduler.CrawlConfig.from_settings(
        {**runtime_settings.DEFAULTS, **settings}, html_quota_ok=True
    )


def test_defaults_keep_the_previous_behaviour():
    p = _config().block_policy(None)
    assert (p.challenge_run, p.suspect_pause_s, p.forbidden_run) == (2, 3600.0, 5)


def test_a_policy_changes_when_refusals_become_a_block():
    strict = ratecontrol.BlockPolicy(challenge_run=1, forbidden_run=2)
    s = ratecontrol.RateState(rate_rps=1.0)
    d = ratecontrol.observe(s, "blocked", "captcha:x", 100, None, 2.0, strict)
    assert d.event == "blocked"
    s = ratecontrol.RateState(rate_rps=1.0)
    assert ratecontrol.observe(s, "error", "http_403", 100, None, 2.0, strict).event is None
    d = ratecontrol.observe(s, "error", "http_403", 100, None, 2.0, strict)
    assert d.event == "blocked" and d.reason == "repeated_http_403"

    patient = ratecontrol.BlockPolicy(challenge_run=4, suspect_pause_s=60)
    s = ratecontrol.RateState(rate_rps=1.0)
    events = [ratecontrol.observe(s, "blocked", "x", 100, None, 2.0, patient) for _ in range(4)]
    assert [e.event for e in events] == ["block_suspected"] * 3 + ["blocked"]
    assert events[0].cooldown_s == 60


def test_a_domain_override_beats_the_setting():
    cfg = _config(block_challenge_run=3, block_suspect_pause_s=600)
    p = cfg.block_policy({"block_challenge_run": 1, "rate_rps": 0.5})
    assert p.challenge_run == 1 and p.suspect_pause_s == 600


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("block_challenge_run", 0),
        ("block_challenge_run", True),
        ("block_suspect_pause_s", -1),
        ("block_forbidden_run", 2.5),
        ("block_retry_after_h", 0),
        ("block_retry_after_h", "soon"),
    ],
)
async def test_invalid_block_settings_are_refused(sessionmaker, key, value):
    async with sessionmaker() as s:
        with pytest.raises(runtime_settings.InvalidSetting):
            await runtime_settings.put(s, key, value, "test")


async def _blocked(s, domain: str, hours_ago: float, overrides=None) -> None:
    s.add(
        DomainState(
            domain=domain,
            block_status="blocked",
            block_reason="captcha:x",
            overrides=overrides or {},
        )
    )
    await s.flush()
    s.add(
        DomainEgressState(
            domain=domain,
            egress_group="1.1.1.1",
            rate_rps=0.2,
            consecutive_blocks=2,
            cooldown_until=func.now() + timedelta(hours=5),
        )
    )
    await s.flush()
    await s.execute(
        update(DomainState)
        .where(DomainState.domain == domain)
        .values(blocked_at=func.now() - timedelta(hours=hours_ago))
    )


async def test_blocked_domains_are_retried_after_the_configured_time(sessionmaker):
    async with sessionmaker() as s:
        await _blocked(s, "old.example", hours_ago=30)
        await _blocked(s, "fresh.example", hours_ago=1)
        await _blocked(s, "never.example", hours_ago=30, overrides={"block_retry_after_h": None})
        await _blocked(s, "quick.example", hours_ago=2, overrides={"block_retry_after_h": 1})

        # default: no automatic retry at all
        assert await feedback.retry_blocked(s, runtime_settings.DEFAULTS) == 1  # quick only
        settings = {**runtime_settings.DEFAULTS, "block_retry_after_h": 24}
        assert await feedback.retry_blocked(s, settings) == 1  # old
        status = dict((await s.execute(select(DomainState.domain, DomainState.block_status))).all())
        assert status == {
            "old.example": "ok",
            "fresh.example": "blocked",
            "never.example": "blocked",
            "quick.example": "ok",
        }
        eg = await s.get(DomainEgressState, ("old.example", "1.1.1.1"))
        assert eg.cooldown_until is None
        # the refusal count stays: the next challenge page blocks again at once
        assert eg.consecutive_blocks == 2
        events = (
            (await s.execute(select(DomainEvent).where(DomainEvent.kind == "unblocked")))
            .scalars()
            .all()
        )
        assert {e.reason for e in events} == {"auto_retry"}
        assert {e.details["block_reason"] for e in events} == {"captcha:x"}


async def test_block_overrides_through_the_admin_api(admin, sessionmaker):
    async with sessionmaker() as s:
        s.add(DomainState(domain="shop.example"))
        await s.commit()
    url = "/api/admin/domains/shop.example"
    r = await admin.patch(
        url, json={"block": {"block_challenge_run": 4, "block_retry_after_h": 12}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["overrides"] == {"block_challenge_run": 4, "block_retry_after_h": 12}
    r = await admin.patch(url, json={"block": {"block_retry_after_h": 0}})  # never, here
    assert r.json()["overrides"]["block_retry_after_h"] is None
    r = await admin.patch(url, json={"block": {"block_challenge_run": None}})  # clear
    assert "block_challenge_run" not in r.json()["overrides"]
    assert (await admin.patch(url, json={"block": {"nonsense": 1}})).status_code == 422
    assert (await admin.patch(url, json={"block": {"block_forbidden_run": 0}})).status_code == 422
    r = await admin.put("/api/admin/settings/block_retry_after_h", json={"value": 48})
    assert r.status_code == 200, r.text


def test_the_worker_pauses_as_long_as_the_coordinator_says():
    from pos_worker.fetch import breaker

    assert breaker.block_pause({"refusal_pause_s": 120}) == 120
    assert breaker.block_pause({}) == breaker.BLOCK_PAUSE_S
    assert breaker.block_pause({"refusal_pause_s": "x"}) == breaker.BLOCK_PAUSE_S
    assert breaker.block_pause({"refusal_pause_s": -5}) == breaker.BLOCK_PAUSE_S
    assert breaker.block_pause({"refusal_pause_s": 10**12}) == breaker.MAX_BLOCK_PAUSE_S
    breaker.reset()
    breaker.trip("zero.example", "blocked", None, 0)
    assert not breaker.paused("zero.example")  # no pause configured: none here either
    breaker.trip("short.example", "blocked", None, 120)
    assert breaker.paused("short.example")
    breaker.reset()


def test_handouts_carry_the_domains_pause():
    cfg = _config(block_suspect_pause_s=900)
    assert cfg.block_policy({"block_suspect_pause_s": 30}).suspect_pause_s == 30
    assert cfg.block_policy(None).suspect_pause_s == 900


async def test_a_retried_domain_gets_a_probe_when_its_jobs_are_used_up(sessionmaker):
    async with sessionmaker() as s:
        await _blocked(s, "spent.example", hours_ago=30)
        await _blocked(s, "busy.example", hours_ago=30)
        await scheduler.enqueue_urls(
            s, ["https://spent.example/p/1"], "shop", 3, extra_payload={"depth": 1}
        )
        await scheduler.enqueue_urls(s, ["https://busy.example/p/1"], "shop")
        await s.execute(
            update(Job)
            .where(Job.domain == "spent.example", Job.type == "fetch_http")
            .values(status="failed", attempts=5, last_error="throttled:http_403")
        )
        settings = {**runtime_settings.DEFAULTS, "block_retry_after_h": 24}
        assert await feedback.retry_blocked(s, settings) == 2
        jobs = (
            await s.execute(
                select(Job.domain, Job.status, Job.priority, Job.payload)
                .where(Job.type == "fetch_http")
                .order_by(Job.domain, Job.created_at)
            )
        ).all()
        spent = [j for j in jobs if j.domain == "spent.example"]
        assert [j.status for j in spent] == ["failed", "queued"]  # the old one stays
        assert spent[1].priority == 3 and spent[1].payload["depth"] == 1
        assert [j.status for j in jobs if j.domain == "busy.example"] == ["queued"]  # no extra
        details = dict((await s.execute(select(DomainEvent.domain, DomainEvent.details))).all())
        assert details["spent.example"]["probe_url"] == "https://spent.example/p/1"
        assert details["busy.example"]["probe_url"] is None
        ds = await s.get(DomainState, "spent.example")
        assert ds.unblocked_at is not None


def test_an_unblock_releases_the_workers_pause():
    from pos_worker.fetch import breaker

    breaker.reset()
    breaker.trip("shop.example", "blocked", None, 7 * 86400)
    before = breaker._tripped_at["shop.example"] - 10
    breaker.release_if_unblocked("shop.example", {"unblocked_at": before})
    assert breaker.paused("shop.example")  # unblocked before the pause: it holds
    breaker.release_if_unblocked("shop.example", {"unblocked_at": "soon"})
    assert breaker.paused("shop.example")
    breaker.release_if_unblocked("shop.example", {"unblocked_at": before + 20})
    assert not breaker.paused("shop.example")
    breaker.reset()


async def test_the_probe_outlives_pruned_jobs(sessionmaker):
    """A block records the job to retry with; months later, when the job rows are
    pruned, the automatic retry still has something to fetch."""
    from pos_shared.api import JobOutcome, JobResult
    from tests.test_crawl_review import _cfg, _node, _ready

    async with sessionmaker() as s:
        n = await _node(s)
        cfg = await _cfg(s)
        urls = ["https://gone.example/p/1", "https://gone.example/p/2"]
        await scheduler.enqueue_urls(s, urls, "shop", 2, extra_payload={"depth": 3})
        await _ready(s, "gone.example")
        jobs = (
            (await s.execute(select(Job).where(Job.type == "fetch_http").order_by(Job.id)))
            .scalars()
            .all()
        )
        for j in jobs:
            j.status, j.node_id, j.attempts = "leased", n.id, 1
        await s.flush()
        for j in jobs:  # two challenge pages in a row: blocked
            r = JobResult(
                job_id=j.id,
                attempt=1,
                outcome=JobOutcome.done,
                output={"verdict": "blocked", "reason": "captcha:x", "final_url": j.payload["url"]},
            )
            await feedback.apply(s, n, {j.id: j}, [r], cfg)
        ds = await s.get(DomainState, "gone.example")
        assert ds.block_status == "blocked"
        event = (
            await s.execute(select(DomainEvent).where(DomainEvent.kind == "blocked"))
        ).scalar_one()
        probe = event.details["probe"]
        assert probe["type"] == "fetch_http" and probe["priority"] == 2
        assert probe["payload"]["depth"] == 3 and probe["payload"]["url"] in urls
        # months later: the jobs were pruned
        await s.execute(Job.__table__.delete().where(Job.domain == "gone.example"))
        await s.execute(
            update(DomainState)
            .where(DomainState.domain == "gone.example")
            .values(blocked_at=func.now() - timedelta(days=90))
        )
        settings = {**runtime_settings.DEFAULTS, "block_retry_after_h": 24 * 80}
        assert await feedback.retry_blocked(s, settings) == 1
        (job,) = (
            (
                await s.execute(
                    select(Job).where(Job.domain == "gone.example", Job.type == "fetch_http")
                )
            )
            .scalars()
            .all()
        )
        assert job.status == "queued" and job.payload["url"] == probe["payload"]["url"]
        assert job.payload["source"] == "shop" and job.priority == 2
