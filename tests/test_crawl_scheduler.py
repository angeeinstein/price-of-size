"""Crawl scheduler: rate slots per (domain, egress group), caps, robots gating, blocks."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from pos_coordinator import runtime_settings
from pos_coordinator.crawl import scheduler
from pos_coordinator.models import DomainEgressState, DomainState, Job, Node

DOMAIN = "shop.example"


async def _node(s, group: str | None) -> Node:
    n = Node(name=group or "n", public_key=uuid.uuid4().bytes * 2, egress_group=group)
    s.add(n)
    await s.flush()
    return n


async def _cfg(s, **overrides) -> scheduler.CrawlConfig:
    cfg = await runtime_settings.get_all(s)
    cfg.update({"base_rate_rps": 1.0, **overrides})
    return scheduler.CrawlConfig.from_settings(cfg, html_quota_ok=True)


async def _setup(s, n_urls=10, robots_status="none", pages_fetched=scheduler.SLOW_START_PAGES):
    report = await scheduler.enqueue_urls(
        s, [f"https://{DOMAIN}/p/{i}" for i in range(n_urls)], "test"
    )
    if robots_status:
        await s.execute(
            update(DomainState).values(
                robots_status=robots_status,
                robots_next_check_at=func.now() + timedelta(days=1),
                pages_fetched=pages_fetched,  # past slow start unless a test wants it
            )
        )
        await s.execute(update(Job).where(Job.type == "fetch_robots").values(status="done"))
    return report


def _delays(handouts):
    return [round(h.delay_s, 2) for h in handouts]


async def test_enqueue_normalises_and_dedups(sessionmaker):
    async with sessionmaker() as s:
        r = await scheduler.enqueue_urls(
            s,
            [
                "https://Shop.example/p/1?utm_source=a",
                "https://shop.example/p/1",
                "nonsense",
                "https://shop.example/p/2",
            ],
            "t",
        )
        assert (r.queued, r.duplicates, r.invalid) == (2, 1, ["nonsense"])
        again = await scheduler.enqueue_urls(s, ["https://shop.example/p/2"], "t")
        assert (again.queued, again.duplicates) == (0, 1)  # still queued: not twice
        robots_jobs = (
            await s.execute(select(func.count()).where(Job.type == "fetch_robots"))
        ).scalar_one()
        assert robots_jobs == 1


async def test_robots_first_then_pages(sessionmaker):
    async with sessionmaker() as s:
        n = await _node(s, "1.1.1.1")
        await _setup(s, 3, robots_status=None)
        cfg = await _cfg(s)
        first = await scheduler.lease_fetch(s, n, ["fetch_http", "fetch_robots"], 10, cfg)
        assert [h.job.type for h in first] == ["fetch_robots"]  # pages wait for robots.txt


async def test_slots_are_spaced_by_the_domain_rate(sessionmaker):
    async with sessionmaker() as s:
        n = await _node(s, "1.1.1.1")
        await _setup(s)
        h = await scheduler.lease_fetch(s, n, ["fetch_http"], 4, await _cfg(s))
        d = _delays(h)
        assert d[0] == 0
        gaps = [b - a for a, b in zip(d, d[1:], strict=False)]
        assert all(0.79 <= g <= 1.21 for g in gaps), gaps  # 1/rate with ±20 % jitter
        assert all(x.extra["delay_s"] == round(x.delay_s, 3) for x in h)
        # the lease covers the wait for the slot as well as the fetch itself
        spans = [(x.job.lease_expires_at - h[0].job.lease_expires_at).total_seconds() for x in h]
        assert spans == pytest.approx([x.delay_s for x in h], abs=0.01)


async def test_batches_are_capped_by_count_and_lookahead(sessionmaker):
    async with sessionmaker() as s:
        n = await _node(s, "1.1.1.1")
        await _setup(s, 200)
        h = await scheduler.lease_fetch(s, n, ["fetch_http"], 500, await _cfg(s))
        assert len(h) == scheduler.MAX_BATCH_PER_DOMAIN
        slow = await _cfg(s, base_rate_rps=0.05)  # one request per 20 s
        await s.execute(update(DomainEgressState).values(rate_rps=0.05, next_slot_at=None))
        h = await scheduler.lease_fetch(s, n, ["fetch_http"], 500, slow)
        assert len(h) <= 4  # slots within the 60 s lookahead only
        assert max(x.delay_s for x in h) <= scheduler.LOOKAHEAD_S


async def test_slow_start_one_fetch_in_flight_for_new_domains(sessionmaker):
    async with sessionmaker() as s:
        n = await _node(s, "1.1.1.1")
        await _setup(s, 5, pages_fetched=0)
        cfg = await _cfg(s)
        assert len(await scheduler.lease_fetch(s, n, ["fetch_http"], 10, cfg)) == 1
        assert await scheduler.lease_fetch(s, n, ["fetch_http"], 10, cfg) == []  # still in flight


@pytest.mark.parametrize("scope", ["per_egress_ip", "global"])
async def test_egress_groups_get_their_own_budget_unless_global(sessionmaker, scope):
    async with sessionmaker() as s:
        a, b = await _node(s, "1.1.1.1"), await _node(s, "2.2.2.2")
        await _setup(s)
        cfg = await _cfg(s, rate_limit_scope=scope)
        da = _delays(await scheduler.lease_fetch(s, a, ["fetch_http"], 2, cfg))
        db = _delays(await scheduler.lease_fetch(s, b, ["fetch_http"], 2, cfg))
        if scope == "per_egress_ip":
            assert da[0] == 0 and db[0] == 0
        else:
            assert db[0] >= da[-1] + 0.79  # one shared schedule


async def test_nodes_behind_one_ip_share_the_schedule(sessionmaker):
    async with sessionmaker() as s:
        a, b = await _node(s, "1.1.1.1"), await _node(s, "1.1.1.1")
        await _setup(s)
        cfg = await _cfg(s)
        da = _delays(await scheduler.lease_fetch(s, a, ["fetch_http"], 2, cfg))
        db = _delays(await scheduler.lease_fetch(s, b, ["fetch_http"], 2, cfg))
        assert db[0] >= da[-1] + 0.79


async def test_hard_cap_across_groups(sessionmaker):
    async with sessionmaker() as s:
        a, b = await _node(s, "1.1.1.1"), await _node(s, "2.2.2.2")
        await _setup(s)
        cfg = await _cfg(s, max_total_rate_per_domain=0.5)
        da = _delays(await scheduler.lease_fetch(s, a, ["fetch_http"], 1, cfg))
        db = _delays(await scheduler.lease_fetch(s, b, ["fetch_http"], 1, cfg))
        assert da == [0] and db[0] >= 2.0  # 0.5 requests/s in total


async def test_blocked_and_cooling_domains_are_not_handed_out(sessionmaker):
    async with sessionmaker() as s:
        n = await _node(s, "1.1.1.1")
        await _setup(s)
        cfg = await _cfg(s)
        await s.execute(update(DomainState).values(block_status="blocked"))
        assert await scheduler.lease_fetch(s, n, ["fetch_http"], 5, cfg) == []
        await s.execute(update(DomainState).values(block_status="ok"))
        await scheduler.lease_fetch(s, n, ["fetch_http"], 1, cfg)  # creates the group state
        await s.execute(
            update(DomainEgressState).values(cooldown_until=func.now() + timedelta(minutes=5))
        )
        assert await scheduler.lease_fetch(s, n, ["fetch_http"], 5, cfg) == []


async def test_robots_disallowed_urls_fail_without_a_slot(sessionmaker):
    async with sessionmaker() as s:
        n = await _node(s, "1.1.1.1")
        await scheduler.enqueue_urls(
            s, [f"https://{DOMAIN}/private/1", f"https://{DOMAIN}/p/1"], "t"
        )
        await s.execute(update(Job).where(Job.type == "fetch_robots").values(status="done"))
        await s.execute(
            update(DomainState).values(
                pages_fetched=scheduler.SLOW_START_PAGES,
                robots_status="rules",
                robots_txt="User-agent: *\nDisallow: /private\n",
                robots_next_check_at=func.now() + timedelta(days=1),
            )
        )
        h = await scheduler.lease_fetch(s, n, ["fetch_http"], 5, await _cfg(s))
        assert [x.job.payload["url"] for x in h] == [f"https://{DOMAIN}/p/1"]
        assert h[0].delay_s == 0  # the disallowed URL did not use up the first slot
        failed = (await s.execute(select(Job).where(Job.status == "failed"))).scalar_one()
        assert failed.last_error == "robots_disallowed"

        cfg_off = await _cfg(s, respect_robots_txt=False)
        await scheduler.enqueue_urls(s, [f"https://{DOMAIN}/private/2"], "t")
        h = await scheduler.lease_fetch(s, n, ["fetch_http"], 5, cfg_off)
        assert [x.job.payload["url"] for x in h] == [f"https://{DOMAIN}/private/2"]


async def test_unreachable_robots_holds_pages_back(sessionmaker):
    async with sessionmaker() as s:
        n = await _node(s, "1.1.1.1")
        await _setup(s, 2, robots_status="unreachable")
        h = await scheduler.lease_fetch(s, n, ["fetch_http", "fetch_robots"], 5, await _cfg(s))
        # no pages; a fresh robots.txt job instead (the old one was marked done)
        assert [x.job.type for x in h] == ["fetch_robots"]


async def test_disallowed_urls_do_not_use_up_a_slow_start_batch(sessionmaker):
    async with sessionmaker() as s:
        n = await _node(s, "1.1.1.1")
        urls_ = [f"https://{DOMAIN}/private/{i}" for i in range(5)] + [f"https://{DOMAIN}/p/1"]
        await scheduler.enqueue_urls(s, urls_, "t")
        await s.execute(update(Job).where(Job.type == "fetch_robots").values(status="done"))
        await s.execute(
            update(DomainState).values(
                robots_status="rules",
                robots_txt="User-agent: *\nDisallow: /private\n",
                robots_next_check_at=func.now() + timedelta(days=1),
            )
        )
        h = await scheduler.lease_fetch(s, n, ["fetch_http"], 5, await _cfg(s))
        assert [x.job.payload["url"] for x in h] == [f"https://{DOMAIN}/p/1"]
