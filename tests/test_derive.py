"""The extract stage: raw evidence -> products, offers, measurements; rebuild drill."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import zstandard
from sqlalchemy import func, select

from pos_coordinator.derive import fx, stage
from pos_coordinator.models import DeriveCursor, Measurement, Offer, Product, RawFetch
from pos_shared.extract.model import MeasurementCandidate
from pos_shared.ids import uuid7

NOW = timedelta(0)  # no settle window in tests


def test_gtin_check_digit():
    assert stage.gtin_valid("4006381333931")
    assert not stage.gtin_valid("4006381333932")
    assert stage.gtin_valid("96385074")  # GTIN-8
    assert not stage.gtin_valid("12345")


def test_density_catches_unit_mixups():
    ok = MeasurementCandidate("product", "labelled", 1.2, 0.8, 0.45, mass_kg=40)
    assert stage.measurement_flags(ok) == []
    mm_as_m = MeasurementCandidate("product", "labelled", 1200, 800, 450, mass_kg=40)
    assert set(stage.measurement_flags(mm_as_m)) == {"implausible_size", "implausible_density"}
    lead_feather = MeasurementCandidate("product", "labelled", 0.1, 0.1, 0.1, mass_kg=40)
    assert stage.measurement_flags(lead_feather) == ["implausible_density"]  # 40,000 kg/m³


def test_gross_not_below_net():
    product = MeasurementCandidate("product", "labelled", mass_kg=50, net_or_gross="net")
    package = MeasurementCandidate("package", "labelled", mass_kg=20)
    assert stage.cross_checks([product, package]) == {1: ["gross_lt_net"]}
    assert (
        stage.cross_checks([product, MeasurementCandidate("package", "labelled", mass_kg=55)]) == {}
    )


def test_ecb_csv():
    rows = fx.parse_ecb_csv("Date,USD,JPY,CYP,\n2026-11-20,1.0812,163.9,N/A,\n")
    assert {(r["currency"], r["rate_per_eur"]) for r in rows} == {
        ("USD", Decimal("1.0812")),
        ("JPY", Decimal("163.9")),
    }


def _evidence(
    gtin,
    price,
    currency,
    name="Regal",
    dims="80 x 40 x 180 cm",
    mass="23 kg",
    availability="InStock",
):
    ld = {
        "@type": "Product",
        "name": name,
        "gtin13": gtin,
        "offers": {
            "@type": "Offer",
            "price": price,
            "priceCurrency": currency,
            "availability": availability,
        },
    }
    return {
        "status": 200,
        "verdict": {"kind": "ok"},
        "snippets": {
            "lang": "de",
            "title": name,
            "json_ld": [json.dumps(ld)],
            "tables": [
                f"<table><tr><th>Maße</th><td>{dims}</td></tr>"
                f"<tr><th>Gewicht</th><td>{mass}</td></tr></table>"
            ],
            "definition_lists": [],
            "spec_blocks": [],
            "price_blocks": [],
        },
    }


async def _fetch(sm, store, evidence, url, source, fetched_at=None):
    blob = zstandard.ZstdCompressor().compress(json.dumps(evidence).encode())
    sha = hashlib.sha256(blob).hexdigest()
    store.put("snippets", sha, blob)
    async with sm() as s:
        f = RawFetch(
            id=uuid7(),
            fetched_at=fetched_at or datetime.now(UTC),
            url=url,
            final_url=url,
            status=200,
            mode="http",
            blob_sha256=sha,
            source=source,
        )
        s.add(f)
        await s.commit()
        return f.id


@pytest.fixture
async def world(app):
    sm, store = app.state.sessionmaker, app.state.rawstore
    async with sm() as s:
        await fx.store(
            s, [{"day": date(2026, 11, 20), "currency": "USD", "rate_per_eur": Decimal("1.0800")}]
        )
        await s.commit()
    return sm, store


async def test_offers_from_two_shops_become_one_product(world):
    sm, store = world
    gtin = "4006381333931"
    sunday = datetime(2026, 11, 22, 12, tzinfo=UTC)  # ECB publishes nothing: Friday's rate
    await _fetch(sm, store, _evidence(gtin, "100.00", "EUR"), "https://a.example/p/1", "a")
    await _fetch(sm, store, _evidence(gtin, "129.60", "USD"), "https://b.example/x", "b", sunday)
    await _fetch(sm, store, _evidence(gtin, "0.01", "EUR"), "https://c.example/y", "c")
    await _fetch(
        sm,
        store,
        {"url": "https://a.example/robots.txt", "body": "..."},
        "https://a.example/robots.txt",
        "robots",
    )
    async with sm() as s:  # a fetch whose evidence went missing from the NAS
        s.add(
            RawFetch(
                id=uuid7(),
                fetched_at=datetime.now(UTC),
                url="https://d.example/",
                mode="http",
                blob_sha256="0" * 64,
                source="d",
            )
        )
        await s.commit()

    report = await stage.run_until_caught_up(sm, store, settle=NOW)
    assert (report.fetches, report.pages, report.products, report.missing_blobs) == (5, 3, 1, 1)
    async with sm() as s:
        product = (await s.execute(select(Product))).scalar_one()
        offers = (await s.execute(select(Offer).order_by(Offer.source))).scalars().all()
        ms = (await s.execute(select(Measurement))).scalars().all()
    assert product.match_key == f"gtin:0{gtin}" and product.dimensionality == "3d_object"
    a, b, c = offers
    assert b.price_eur == Decimal("120.0000") and b.fx_rate == Decimal("1.0800")
    assert "placeholder_price" in c.flags
    assert product.canonical_price_eur == Decimal("110.0000")  # median of 100 and 120
    assert all(m.a_m == pytest.approx(1.8) for m in ms if m.method == "spec_table")
    assert all(o.parser_version and o.validator_version for o in offers)

    # nothing new: a second run is a no-op (no duplicates)
    assert (await stage.run_until_caught_up(sm, store, settle=NOW)).fetches == 0
    async with sm() as s:
        assert (await s.execute(select(func.count()).select_from(Offer))).scalar_one() == 3


async def test_settle_window_protects_the_cursor(world):
    sm, store = world
    await _fetch(sm, store, _evidence("4006381333931", "10", "EUR"), "https://a.example/1", "a")
    assert (await stage.run_until_caught_up(sm, store)).fetches == 0  # too fresh
    assert (await stage.run_until_caught_up(sm, store, settle=NOW)).fetches == 1


async def test_missing_fx_rate_is_flagged_not_guessed(world):
    sm, store = world
    await _fetch(sm, store, _evidence("96385074", "5000", "JPY"), "https://jp.example/1", "jp")
    await stage.run_until_caught_up(sm, store, settle=NOW)
    async with sm() as s:
        offer = (await s.execute(select(Offer))).scalar_one()
        product = (await s.execute(select(Product))).scalar_one()
    assert offer.price_eur is None and "no_fx_rate" in offer.flags
    assert product.canonical_price_eur is None


async def test_rebuild_drill_reproduces_the_derived_layer(world):
    sm, store = world
    for i in range(12):
        await _fetch(
            sm,
            store,
            _evidence("", f"{10 + i}.00", "EUR", name=f"Item {i}"),
            f"https://a.example/p/{i}",
            "a",
        )
    await stage.run_until_caught_up(sm, store, limit=5, settle=NOW)
    result = await stage.rebuild(sm, store, settle=NOW)
    assert result["identical"], result
    assert result["after"]["product"] == 12 and result["fetches"] == 12
    async with sm() as s:
        cur = (await s.execute(select(DeriveCursor))).scalar_one()
    assert cur.version == stage.VERSION and cur.processed == 12


async def test_scatter_for_the_log_log_plot(world, app):
    import httpx

    from pos_coordinator.app import create_app  # noqa: F401
    from tests.conftest import ADMIN_TOKEN

    sm, store = world
    things = [
        ("10", "1 kg", "10 x 10 x 10 cm"),
        ("100", "10 kg", "20 x 20 x 20 cm"),
        ("1000", "100 kg", "50 x 50 x 40 cm"),
    ]  # all with plausible densities
    for i, (price, mass, dims) in enumerate(things):
        await _fetch(
            sm,
            store,
            _evidence("", price, "EUR", name=f"Thing {i}", mass=mass, dims=dims),
            f"https://s.example/{i}",
            "s",
        )
    await _fetch(
        sm,
        store,
        _evidence("", "50", "EUR", name="Lead brick", mass="900 kg", dims="10 x 10 x 10 cm"),
        "https://s.example/lead",
        "s",
    )
    await stage.run_until_caught_up(sm, store, settle=NOW)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://c",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    ) as c:
        r = (await c.get("/api/panel/scatter", params={"x": "mass"})).json()
        assert (await c.get("/api/panel/scatter", params={"x": "bogus"})).status_code == 422
    # the implausible lead brick (900,000 kg/m³) is not plotted
    assert r["n"] == 3 and {p["t"] for p in r["points"]} == {"Thing 0", "Thing 1", "Thing 2"}
    assert r["fit"]["slope"] == pytest.approx(1.0)  # price ∝ mass in this toy world
