"""Wikidata as an API source: query, raw response, records, derived products.

The SPARQL rows here are synthetic, in the shape the adapter's query returns."""

import asyncio
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from pos_coordinator.crawl import apisources
from pos_coordinator.crawl.discovery import SourceConfigError
from pos_coordinator.derive import fx, stage
from pos_coordinator.models import Job, Measurement, Offer, Product, RawFetch
from pos_shared.extract import wikidata
from tests.fake_shop import FakeShop
from tests.helpers import make_agent

# --- the query --------------------------------------------------------------------


def test_queries_are_built_for_one_class_and_page():
    q = wikidata.build_query("Q11446", True, 500, 1000)
    assert "wdt:P31/wdt:P279* wd:Q11446" in q and "LIMIT 500 OFFSET 1000" in q
    assert "wdt:P31 wd:Q41176 ." in wikidata.build_query("Q41176", False, 10, 0)
    for bad in ("11446", "Q0", "Q1 } . ?x ?y ?z", "wd:Q1"):
        with pytest.raises(ValueError):
            wikidata.build_query(bad, True, 10, 0)


# --- records from a response ------------------------------------------------------


def _lit(v):
    return {"type": "literal", "value": v}


def _row(qid, costs, **values):
    row = {"item": {"type": "uri", "value": f"http://www.wikidata.org/entity/{qid}"}}
    row["costs"] = _lit("|".join(costs))
    row.update({k: _lit(v) for k, v in values.items()})
    return row


def _records(*rows, label="ship"):
    return wikidata.records({"body": {"results": {"bindings": list(rows)}}}, label)


def test_costs_carry_currency_date_and_the_newest_counts():
    (r,) = _records(
        _row(
            "Q1",
            [
                "P2130~61000000~Q4917~USD~2017-01-01T00:00:00Z",
                "P2130~58000000~Q4917~usd~2015-06-01T00:00:00Z",
                "P2284~1~Q4917~USD~",  # undated: oldest, never the current one
            ],
            label="Fighter",
            P571="2010-00-00T00:00:00Z",
        )
    )
    e = r.extraction
    assert (r.qid, r.url, e.title, e.sku, e.category_path) == (
        "Q1",
        "https://www.wikidata.org/wiki/Q1",
        "Fighter",
        "Q1",
        ["ship"],
    )
    current, older, undated = e.offers
    assert (current.price, current.currency, current.as_of, current.price_kind) == (
        61_000_000,
        "USD",
        "2017-01-01",
        "item",
    )
    assert older.currency == "USD" and older.price_kind == "older_cost"
    # an undated statement falls back to the item's inception (month 00 -> 01)
    assert (undated.as_of, undated.as_of_method, undated.method) == (
        "2010-01-01",
        "item_P571",
        "wikidata:P2284",
    )


def test_old_currencies_and_undated_costs_stay_flagged():
    gold_mark, undated = _records(
        _row("Q2", ["P2130~2140000~Q560355~~1912-01-01T00:00:00Z"]),
        _row("Q3", ["P2130~420000000~Q4917~USD~"]),
    )
    o = gold_mark.extraction.offers[0]
    assert o.currency is None and o.price_raw == "2.14e+06 Q560355"  # no ISO code
    assert undated.extraction.offers[0].as_of is None
    assert undated.extraction.flags == ["undated_cost"]


def test_sizes_masses_and_cylinders():
    sub, rocket, ball, bad = _records(
        _row("Q4", ["P2130~1~Q4917~USD~"], masss=_join(493000, 611000), lengths="57.68"),
        _row("Q5", ["P2130~1~Q4917~USD~"], lengths="70", diameters="3.7"),
        _row("Q6", ["P2130~1~Q4917~USD~"], diameters="2"),
        _row("Q7", ["P2130~1~Q4917~USD~"], masss="-5|nan|x", lengths="0"),
    )
    m = sub.extraction.measurements[0]
    assert (m.a_m, m.b_m, m.mass_kg) == (57.68, None, 493000)  # the lighter variant
    assert m.raw["mass"] == "493000|611000" and m.method == "wikidata"
    m = rocket.extraction.measurements[0]
    assert (m.a_m, m.b_m, m.c_m) == (70, 3.7, 3.7)
    assert ball.extraction.measurements[0].a_m == 2
    assert bad.extraction.measurements == []


def _join(*v):
    return "|".join(str(x) for x in v)


def test_hostile_responses_give_no_records():
    assert wikidata.records({"body": None}) == []
    assert wikidata.records({"body": {"results": {"bindings": "x"}}}) == []
    junk = [
        "nope",
        {"item": {"value": "http://example.org/Q1"}},  # not an item id
        _row("Q8", ["garbage", "P2130~abc~Q4917~USD~", "P2130~1~Q4917~USD~-0500-01-01T00:00:00Z"]),
    ]
    (r,) = wikidata.records({"body": {"results": {"bindings": junk}}})
    (o,) = r.extraction.offers
    assert o.as_of is None  # a BCE date is no cost date


# --- source config ----------------------------------------------------------------


def test_api_source_config_is_validated():
    ok = {"adapter": "wikidata", "classes": [{"id": "Q11446", "label": "ship"}]}
    assert apisources.validate_config(ok) is ok
    for bad in (
        {"adapter": "other", "classes": [{"id": "Q1"}]},
        {"adapter": "wikidata", "classes": []},
        {"adapter": "wikidata", "classes": [{"id": "ship"}]},
        {"adapter": "wikidata", "classes": [{"id": "Q1", "subclasses": "yes"}]},
        {"adapter": "wikidata", "classes": [{"id": "Q1", "query": "DROP"}]},
        {**ok, "page_size": 0},
        {**ok, "page_size": 50_000},
        {**ok, "endpoint": "ftp://x/sparql"},
        {**ok, "endpoint": "https://x/sparql?query=1"},
        {**ok, "sitemaps": []},
    ):
        with pytest.raises(SourceConfigError):
            apisources.validate_config(bad)


# --- end to end -------------------------------------------------------------------


@pytest.fixture
def shop(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("POS_WORKER_ALLOW_PRIVATE_TARGETS", "1")
    with FakeShop() as s:
        yield s


async def test_wikidata_source_end_to_end(app, admin, sessionmaker, shop, tmp_path):
    """Pages of a class are queried until one comes back short (robots.txt, which
    disallows the endpoint for crawlers, does not apply to the API); the raw responses
    become one product per item, costs converted at the date they refer to."""
    for k, v in (("base_rate_rps", 10), ("max_rate_rps", 10)):
        assert (await admin.put(f"/api/admin/settings/{k}", json={"value": v})).status_code == 200
    config = {
        "adapter": "wikidata",
        "classes": [{"id": "Q11446", "label": "ship"}],
        "page_size": 2,
        "endpoint": shop.url("127.0.0.5", "/sparql"),
    }
    r = await admin.put("/api/admin/sources/wd", json={"type": "api", "config": config})
    assert r.status_code == 200, r.text
    bad = {**config, "classes": [{"id": "x"}]}
    assert (
        await admin.put("/api/admin/sources/wd2", json={"type": "api", "config": bad})
    ).status_code == 422
    assert (await admin.post("/api/admin/sources/wd/discover")).json() == {"queries": 1}

    async def pages_done():
        async with sessionmaker() as s:
            n = (
                await s.execute(
                    select(func.count()).where(Job.type == "fetch_api", Job.status == "done")
                )
            ).scalar_one()
            return n == 2

    worker = await make_agent(app, admin, tmp_path / "w", "w")
    task = asyncio.create_task(worker.run(drain_timeout_s=5))
    try:
        for _ in range(300):
            if await pages_done():
                break
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError("API pages not fetched")
        await asyncio.sleep(0.5)  # a third page would have been queued by now
    finally:
        worker.stop()
        await task
    queries = [p for p in shop.paths("127.0.0.5") if p.startswith("/sparql?")]
    assert len(queries) == 2  # offset 0 (full), offset 2 (short): no third page
    async with sessionmaker() as s:
        assert (
            await s.execute(select(func.count()).where(Job.type == "fetch_api"))
        ).scalar_one() == 2
        modes = set(
            (await s.execute(select(RawFetch.mode).where(RawFetch.url.like("%sparql%")))).scalars()
        )
        assert modes == {"api"}
        await fx.store(
            s, [{"day": date(2019, 12, 31), "currency": "USD", "rate_per_eur": Decimal("1.1234")}]
        )
        await s.commit()

    sm, store = app.state.sessionmaker, app.state.rawstore
    report = await stage.run_until_caught_up(sm, store, settle=stage.timedelta(0))
    assert report.errors == 0
    async with sessionmaker() as s:
        products = {
            p.url: p
            for p in (await s.execute(select(Product).where(Product.source == "wd"))).scalars()
        }
        assert set(products) == {f"https://www.wikidata.org/wiki/Q10{i}" for i in (1, 2, 3)}
        tanker = products["https://www.wikidata.org/wiki/Q101"]
        assert tanker.title == "Tanker A" and tanker.attributes["category_path"] == ["ship"]
        offers = {o.product_id: o for o in (await s.execute(select(Offer))).scalars().all()}
        o = offers[tanker.id]
        assert o.currency == "USD" and o.price_eur == Decimal("222538721.7376")  # at 2019-12-31
        assert o.fields["price"]["as_of"] == "2020-01-01"
        assert o.flags == []
        liner = offers[products["https://www.wikidata.org/wiki/Q102"].id]
        assert liner.price_eur == Decimal("900000000.0000")
        assert liner.fields["price"]["as_of_method"] == "item_P729"
        barque = offers[products["https://www.wikidata.org/wiki/Q103"].id]
        assert "no_fx_rate" in barque.flags and barque.price_eur is None  # 1869
        m = (
            await s.execute(select(Measurement).where(Measurement.product_id == tanker.id))
        ).scalar_one()
        assert (m.a_m, m.mass_kg, m.method) == (330, 60_000_000, "wikidata")


def test_an_item_without_a_usable_cost_still_has_its_row():
    """The query keeps one row per selected item (pagination counts them): a cost
    statement with "unknown value" comes back as an empty cost list."""
    (r,) = _records(_row("Q9", [""], lengths="12"))
    assert r.extraction.offers == [] and r.extraction.measurements[0].a_m == 12
    q = wikidata.build_query("Q11446", True, 10, 0)
    costs_block = q[q.index("AS %items") : q.index("AS %costs")]
    assert "OPTIONAL {\n      VALUES" in costs_block


async def test_fx_backfill_uses_the_date_a_cost_refers_to(app, sessionmaker):
    """A cost from 1869 must never get today's exchange rate in a later backfill."""
    from datetime import UTC, datetime

    from pos_coordinator.models import Product

    async with sessionmaker() as s:
        p = Product(match_key="url:https://www.wikidata.org/wiki/Q1", source="wd")
        s.add(p)
        await s.flush()
        now = datetime.now(UTC)
        for as_of in ("1869-01-01", "2020-01-01"):
            s.add(
                Offer(
                    product_id=p.id,
                    source="wd",
                    url=p.match_key[4:],
                    observed_on=now,
                    price=Decimal("1000"),
                    currency="GBP",
                    flags=["no_fx_rate"],
                    fields={"price": {"as_of": as_of, "kind": "item"}},
                    parser_version="4",
                    validator_version="2",
                )
            )
        await fx.store(
            s,
            [
                {"day": now.date(), "currency": "GBP", "rate_per_eur": Decimal("0.85")},
                {"day": date(2019, 12, 31), "currency": "GBP", "rate_per_eur": Decimal("0.8508")},
            ],
        )
        await s.commit()
    assert await stage.backfill_fx(app.state.sessionmaker) == 1
    async with sessionmaker() as s:
        offers = {o.fields["price"]["as_of"]: o for o in (await s.execute(select(Offer))).scalars()}
    assert offers["1869-01-01"].price_eur is None and "no_fx_rate" in offers["1869-01-01"].flags
    assert offers["2020-01-01"].fx_rate == Decimal("0.8508")
