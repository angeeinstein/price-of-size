"""Per-site parsers: the declarative engine and its safety checks, generation from
pages the LLM tier read, held-out validation, activation, and use in the derive
stage."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
import zstandard
from sqlalchemy import select, update

from pos_coordinator.derive import site_parsers, stage
from pos_coordinator.llm.service import LlmService
from pos_coordinator.models import LlmBatch, LlmExtraction, Offer, Parser, RawFetch
from pos_shared.extract import site_parser
from pos_shared.extract.model import Extraction
from pos_shared.ids import uuid7
from tests.test_classification import _answer
from tests.test_llm import FakeClient

NOW = timedelta(0)


def _html(i: int, price: str = "1.299,00 €", weight: str = "35 kg") -> str:
    return f"""<html lang="de"><head><title>Regal {i} | Holzwerk</title>
    <script>var tracking = "{i}";</script><style>.x{{color:red}}</style></head><body>
    <nav class="crumbs"><a>Start</a><a>Möbel</a><a>Regale</a></nav>
    <h1 class="product-name" onclick="x()">Eichenregal Nr. {i}</h1>
    <div class="buy"><span class="old">1.499,00 €</span>
      <span class="price-now">{price}</span></div>
    <table class="facts">
      <tr><th>Maße (B x H x T)</th><td>80 x 180 x {30 + i} cm</td></tr>
      <tr><th>Gewicht</th><td>{weight}</td></tr>
    </table></body></html>"""


SPEC = {
    "title": {"css": "h1.product-name", "attr": None, "regex": None, "index": 0},
    "brand": None,
    "gtin": None,
    "price": {"css": ".buy .price-now", "attr": None, "regex": None, "index": 0},
    "currency": None,
    "currency_default": "EUR",
    "availability": None,
    "condition": None,
    "category": {"css": "nav.crumbs a", "attr": None, "regex": None, "index": 0},
    "size_rows": [{"css": "table.facts tr", "label": "th", "value": "td"}],
    "size_picks": [],
}


def _answer_for(i: int) -> dict:
    return {
        "is_product_page": True,
        "brand": None,
        "price_text": "1.299,00 €",
        "currency": "EUR",
        "price_kind": "item",
        "availability": "unknown",
        "condition": "unknown",
        "sizes": [
            {"label": "Maße (B x H x T)", "value": f"80 x 180 x {30 + i} cm"},
            {"label": "Gewicht", "value": "35 kg"},
        ],
        "category_path": ["Möbel", "Regale"],
    }


# --- the engine ----------------------------------------------------------------------


def test_the_engine_reads_a_page():
    spec = site_parser.parse(SPEC)
    r = site_parser.run(spec, _html(3))
    assert r.title == "Eichenregal Nr. 3" and r.price_text == "1.299,00 €"
    assert r.category_path == ["Start", "Möbel", "Regale"]
    assert r.pairs == [("Maße (B x H x T)", "80 x 180 x 33 cm"), ("Gewicht", "35 kg")]
    assert site_parser.currency_of(r, spec) == "EUR"
    ex = Extraction(extract_version="t", title="Regal 3")
    assert site_parser.apply(ex, r, spec, "de", "holz.example@1")
    (o,) = ex.offers
    assert (o.price, o.currency, o.method) == (1299.0, "EUR", "site_parser:holz.example@1")
    (m,) = ex.measurements
    assert (m.a_m, m.b_m, m.c_m, m.mass_kg) == (1.8, 0.8, 0.33, 35.0)
    # regex and attribute picks
    spec2 = site_parser.parse(
        {
            **SPEC,
            "price": {"css": "title", "attr": None, "regex": r"Regal (\d+)", "index": 0},
            "title": {"css": "html", "attr": "lang", "regex": None, "index": 0},
        }  # fmt: skip
    )
    r2 = site_parser.run(spec2, _html(7))
    assert r2.price_text == "7" and r2.title == "de"
    # notes in the stored artifact are not part of the spec
    assert site_parser.parse({**SPEC, "_check": {"held_out": [1, 1]}}) == spec


@pytest.mark.parametrize(
    "change",
    [
        {"price": {"css": "a[", "attr": None, "regex": None, "index": 0}},
        {"price": {"css": "x" * 400, "attr": None, "regex": None, "index": 0}},
        {"price": {"css": "span", "attr": None, "regex": "(a+)+$", "index": 0}},
        {"price": {"css": "span", "attr": None, "regex": "((a)+)+$", "index": 0}},
        {"price": {"css": "span", "attr": None, "regex": "(?:(?:a){2,})*$", "index": 0}},
        {"price": {"css": "span", "attr": None, "regex": "(a|aa)+$", "index": 0}},
        {"price": {"css": "span", "attr": None, "regex": "(?=(a+)+)", "index": 0}},
        {"price": {"css": "span", "attr": None, "regex": r"(\w+)\s\1", "index": 0}},
        {"price": {"css": "span", "attr": None, "regex": r"\d*" * 13, "index": 0}},
        {"price": {"css": "span", "attr": None, "regex": "([", "index": 0}},
        {"price": {"css": "span", "attr": "on click", "regex": None, "index": 0}},
        {"price": {"css": "span", "attr": None, "regex": None, "index": 99}},
        {"currency_default": "euro"},
        {"price": None, "size_rows": [], "size_picks": []},
        {"size_rows": [{"css": "tr", "label": "th", "value": "td"}] * 6},
        {"unexpected": 1},
    ],
)
def test_unsafe_or_useless_specs_are_refused(change):
    with pytest.raises(site_parser.SpecError):
        site_parser.parse({**SPEC, **change})


def test_regular_expressions_are_checked_by_structure_and_run_with_a_time_limit():
    import time

    # the usual kind passes the check
    for rx in (r"([\d.,]+)\s*(?:kg|g)\b", r"(\d+)\s*x\s*(\d+)\s*x\s*(\d+)", r"Preis:\s*(.+)"):
        site_parser.parse({**SPEC, "price": {"css": "p", "attr": None, "regex": rx, "index": 0}})
    # one the structure allows but that is slow on a long input finds nothing, quickly
    slow = r"\d*\d*\d*\d*\d*\d*\d*x"
    spec = site_parser.parse({**SPEC, "price": {"css": "p", "attr": None, "regex": slow,
                                                "index": 0}})  # fmt: skip
    page = f"<html><body><p>{'1' * 400}</p></body></html>"
    start = time.monotonic()
    assert site_parser.run(spec, page).price_text is None
    assert time.monotonic() - start < 2


@pytest.mark.parametrize(
    ("price_text", "currency", "default", "expected"),
    [
        ("$1,299.00", None, "AUD", "AUD"),  # the shop's dollar
        ("$1,299.00", None, None, "USD"),
        ("$1,299.00", None, "EUR", "USD"),  # a default that is no dollar
        ("A$ 1,299", None, "USD", "AUD"),  # a prefix names it
        ("CA$1,299", None, None, "CAD"),
        ("1.299,00 €", None, "USD", "EUR"),
        ("1 299 kr", None, "NOK", "NOK"),
        ("1 299 kr", None, None, "SEK"),
        ("1299", "CHF", None, "CHF"),
        ("CHF1299", None, None, "CHF"),
        ("1299", None, "PLN", "PLN"),
        ("1 299 zł", None, None, "PLN"),
    ],
)
def test_the_currency_of_a_price(price_text, currency, default, expected):
    spec = site_parser.parse({**SPEC, "currency_default": default})
    result = site_parser.SiteResult(price_text=price_text, currency=currency)
    assert site_parser.currency_of(result, spec) == expected


def test_a_page_passes_only_in_the_currency_the_llm_read():
    page = "<html><body><h1 class='product-name'>Regal</h1><div class='buy'>" \
        "<span class='price-now'>$1,299.00</span></div></body></html>"  # fmt: skip
    answer = {"price_text": "$1,299.00", "currency": "AUD", "sizes": []}
    usd = site_parser.parse({**SPEC, "currency_default": None, "size_rows": []})
    aud = site_parser.parse({**SPEC, "currency_default": "AUD", "size_rows": []})
    assert not site_parsers.page_passes(usd, page.encode(), answer, "en")
    assert site_parsers.page_passes(aud, page.encode(), answer, "en")


def test_a_page_passes_only_with_the_meaning_the_llm_read():
    spec = site_parser.parse(SPEC)
    page = _html(1).encode()  # "Gewicht: 35 kg" in its table, price 1.299,00 €
    assert site_parsers.page_passes(spec, page, _answer_for(1), "de")
    # the LLM read the weight as the shipping weight (package, gross): the spec's
    # unlabelled weight is not the same measurement
    shipping = _answer_for(1)
    shipping["sizes"] = [{"label": "Versandgewicht", "value": "35 kg"}]
    assert not site_parsers.page_passes(spec, page, shipping, "de")
    # a price the LLM read as a monthly (unit, net, "from") price: a spec reads item
    # prices only
    for kind in ("monthly", "unit_price", "net", "from_price"):
        assert not site_parsers.page_passes(spec, page, {**_answer_for(1), "price_kind": kind},
                                            "de")  # fmt: skip
    assert site_parsers.page_passes(spec, page, {**_answer_for(1), "price_kind": "unknown"}, "de")


def test_a_page_passes_only_with_the_availability_the_llm_read():
    sold_out = _html(1).replace(
        '<div class="buy">', '<div class="buy"><link itemprop="availability" '
        'href="https://schema.org/OutOfStock">',
    )  # fmt: skip
    reads_it = site_parser.parse(
        {
            **SPEC,
            "availability": {
                "css": "[itemprop=availability]",
                "attr": "href",
                "regex": None,
                "index": 0,
            },
        }  # fmt: skip
    )
    blind = site_parser.parse(SPEC)
    gone = {**_answer_for(1), "availability": "out_of_stock"}
    assert site_parsers.page_passes(reads_it, sold_out.encode(), gone, "de")
    # a spec that cannot see it would make an unavailable price count
    assert not site_parsers.page_passes(blind, sold_out.encode(), gone, "de")
    # nor may a spec see it where the LLM did not
    available = {**_answer_for(1), "availability": "in_stock"}
    assert not site_parsers.page_passes(reads_it, sold_out.encode(), available, "de")
    assert site_parsers.page_passes(blind, _html(1).encode(), available, "de")


def test_a_page_passes_only_with_the_condition_the_llm_read():
    used_page = _html(1).replace(
        '<div class="buy">', '<div class="buy"><link itemprop="itemCondition" '
        'href="https://schema.org/UsedCondition">',
    )  # fmt: skip
    reads_it = site_parser.parse(
        {
            **SPEC,
            "condition": {
                "css": "[itemprop=itemCondition]",
                "attr": "href",
                "regex": None,
                "index": 0,
            },
        }  # fmt: skip
    )
    blind = site_parser.parse(SPEC)
    used = {**_answer_for(1), "condition": "used"}
    assert site_parsers.page_passes(reads_it, used_page.encode(), used, "de")
    # a spec that cannot see it would make a used item's price a new one's
    assert not site_parsers.page_passes(blind, used_page.encode(), used, "de")
    new = {**_answer_for(1), "condition": "new"}
    assert not site_parsers.page_passes(reads_it, used_page.encode(), new, "de")
    assert site_parsers.page_passes(blind, _html(1).encode(), new, "de")
    # and the offer it makes carries the condition (flagged downstream)
    ex = Extraction(extract_version="t", title="Regal")
    result = site_parser.run(reads_it, used_page)
    assert site_parser.apply(ex, result, reads_it, "de", "holz.example@1")
    assert ex.offers[0].condition == "used"
    for text, want in (("Generalüberholt", "refurbished"), ("B-Ware", "used"),
                       ("https://schema.org/NewCondition", "new"), ("Neu", "new"),
                       ("https://schema.org/DamagedCondition", "damaged"), ("", None)):  # fmt: skip
        assert site_parser._condition(text) == want, text


async def test_activating_two_versions_at_once_leaves_one_active(app):
    import asyncio

    sm = app.state.sessionmaker
    async with sm() as s:
        ids = []
        for v in (1, 2):
            p = Parser(domain="holz.example", version=v, kind="declarative", artifact=SPEC,
                       validation_score=1.0, active=False)  # fmt: skip
            s.add(p)
            await s.flush()
            ids.append(p.id)
        await s.commit()

    async def activate(pid):
        async with sm() as s:
            p = await s.get(Parser, pid)
            await site_parsers.set_active(s, p, True, "test")
            await asyncio.sleep(0.2)  # the other activation runs meanwhile
            await s.commit()

    await asyncio.gather(activate(ids[0]), activate(ids[1]))
    async with sm() as s:
        active = (await s.execute(select(Parser.version).where(Parser.active))).scalars().all()
    assert len(active) == 1


async def test_activating_a_version_loaded_before_another_was_activated(app):
    sm = app.state.sessionmaker
    async with sm() as s:
        a = Parser(domain="holz.example", version=1, kind="declarative", artifact=SPEC,
                   validation_score=1.0, active=True)  # fmt: skip
        b = Parser(domain="holz.example", version=2, kind="declarative", artifact=SPEC,
                   validation_score=1.0, active=False)  # fmt: skip
        s.add_all([a, b])
        await s.commit()
        ids = (a.id, b.id)
    async with sm() as first:
        stale = await first.get(Parser, ids[0])  # active, as loaded
        await first.commit()  # (expire_on_commit off: it stays as loaded)
        async with sm() as second:  # meanwhile B becomes the active version
            await site_parsers.set_active(second, await second.get(Parser, ids[1]), True, "t")
            await second.commit()
        await site_parsers.set_active(first, stale, True, "test")
        await first.commit()
        assert stale.active
    async with sm() as s:
        active = (await s.execute(select(Parser.version).where(Parser.active))).scalars().all()
    assert active == [1]


def test_condensed_html_for_the_llm():
    t = site_parser.condensed_html(_html(1))
    assert t.startswith("<body>") and "tracking" not in t and "color:red" not in t
    assert 'class="product-name"' in t and "onclick" not in t
    assert site_parser.condensed_html(_html(1)) == t
    long = _html(1).replace("</table>", "</table>" + "<p class='x'>Text</p>" * 1000)
    assert len(site_parser.condensed_html(long, 5_000)) == 5_000


# --- generation and use ----------------------------------------------------------------


async def _page(sm, store, i, html_text=None, domain="holz.example", requested=None):
    """A fetch without structured data: snippets (title only) and the full page."""
    evidence = {
        "status": 200,
        "verdict": {"kind": "ok"},
        "snippets": {
            "lang": "de",
            "title": f"Regal {i}",
            "json_ld": [],
            "tables": [],
            "definition_lists": [],
            "spec_blocks": [],
            "price_blocks": [],
        },  # fmt: skip
    }
    blobs = {}
    for kind, data in (("snippets", json.dumps(evidence).encode()),
                       ("html", (html_text or _html(i)).encode())):  # fmt: skip
        blob = zstandard.ZstdCompressor().compress(data)
        blobs[kind] = hashlib.sha256(blob).hexdigest()
        store.put(kind, blobs[kind], blob)
    async with sm() as s:
        url = f"https://www.{domain}/p/{i}"
        f = RawFetch(id=uuid7(), fetched_at=datetime.now(UTC), url=requested or url, final_url=url,
                     status=200, mode="http", blob_sha256=blobs["snippets"],
                     html_sha256=blobs["html"], source=domain)  # fmt: skip
        s.add(f)
        await s.commit()
        return f.id


async def _read_by_llm(sm):
    """As if the LLM tier had answered every wanted page."""
    async with sm() as s:
        for row in (await s.execute(select(LlmExtraction))).scalars():
            f = (await s.execute(select(RawFetch).where(RawFetch.id == row.fetch_id))).scalar_one()
            i = int(f.url.rsplit("/", 1)[1])
            row.status, row.output, row.model = "ok", _answer_for(i), "claude-haiku-5-5"
        await s.commit()


async def _world(app, n=20):
    sm, store = app.state.sessionmaker, app.state.rawstore
    for i in range(n):
        await _page(sm, store, i)
    await stage.run_until_caught_up(sm, store, settle=NOW)
    await _read_by_llm(sm)
    return sm, store


async def _generate(sm, store, spec):
    fake = FakeClient()
    llm = LlmService(sm, lambda: fake)
    assert await site_parsers.schedule(sm, llm, store) == {"parsers_requested": 1}
    (bid,) = fake.batches
    (req,) = fake.batches[bid]["requests"]
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [_answer("0", json.dumps(spec))]
    assert await llm.poll_batches() == 1
    return req, llm


async def test_a_parser_is_generated_checked_activated_and_used(app, admin):
    sm, store = await _world(app)
    req, llm = await _generate(sm, store, SPEC)
    params = req["params"]
    assert params["model"] == "claude-sonnet-5-5" and params["output_config"]["effort"] == "medium"
    text = params["messages"][0]["content"]
    assert text.startswith("Shop: holz.example\n\n## Page 1: https://www.holz.example/p/")
    assert "Found there:\nprice: 1.299,00 €\nMaße (B x H x T): 80 x 180 x" in text
    assert 'class="price-now"' in text and "tracking" not in text
    async with sm() as s:
        (p,) = (await s.execute(select(Parser))).scalars().all()
        meta = (await s.execute(select(LlmBatch))).scalar_one().requests["0"]["meta"]
    assert len(meta["train"]) == 5 and len(meta["held_out"]) == 15
    assert p.active and p.version == 1 and p.validation_score == 1.0
    assert p.artifact["_check"]["held_out"] == [15, 15]
    assert p.created_by.startswith("llm:claude-haiku-5-5/site-parser-1")  # the fake's model
    # no new request while the domain's parser is fresh
    assert await site_parsers.schedule(sm, llm, store) == {"parsers_requested": 0}
    # a new page of the domain: the parser fills it, the LLM tier is not asked
    site_parsers.forget()
    fid = await _page(sm, store, 42)
    await stage.run_until_caught_up(sm, store, settle=NOW)
    async with sm() as s:
        (o,) = (await s.execute(select(Offer).where(Offer.fetch_id == fid))).scalars().all()
        assert (
            float(o.price) == 1299 and o.fields["price"]["method"] == "site_parser:holz.example@1"
        )
        wanted = (
            await s.execute(select(LlmExtraction).where(LlmExtraction.fetch_id == fid))
        ).scalar_one_or_none()
        assert wanted is None
    # the panel's view and a test run beside the LLM's answers
    (row,) = (await admin.get("/api/admin/parsers")).json()
    assert row["active"] and row["held_out"] == [15, 15] and row["qualified_prices"] == 0
    runs = (await admin.get(f"/api/admin/parsers/{row['id']}/test")).json()
    assert runs and all(r["passes"] in (True, None) for r in runs)
    assert any(r["found"]["price"] == "1.299,00 €" for r in runs)
    r = await admin.post(f"/api/admin/parsers/{row['id']}/deactivate")
    assert r.status_code == 200 and not r.json()["active"]
    r = await admin.post(f"/api/admin/parsers/{row['id']}/activate")
    assert r.json()["active"]


async def test_the_test_run_shows_pages_redirected_to_the_shop(app, admin):
    sm, store = await _world(app)
    await _generate(sm, store, SPEC)
    await _page(sm, store, 77, requested="https://old-shop.example/artikel/77")
    (row,) = (await admin.get("/api/admin/parsers")).json()
    runs = (await admin.get(f"/api/admin/parsers/{row['id']}/test")).json()
    assert "https://www.holz.example/p/77" in {r["url"] for r in runs}


async def test_a_parser_that_fails_its_check_stays_inactive(app):
    sm, store = await _world(app)
    wrong = {**SPEC, "price": {"css": ".buy .old", "attr": None, "regex": None, "index": 0}}
    _, llm = await _generate(sm, store, wrong)
    async with sm() as s:
        (p,) = (await s.execute(select(Parser))).scalars().all()
    assert not p.active and p.validation_score == 0.0
    # not tried again for a while
    assert await site_parsers.schedule(sm, llm, store) == {"parsers_requested": 0}
    async with sm() as s:
        await s.execute(update(Parser).values(created_at=datetime.now(UTC) - timedelta(days=8)))
        await s.commit()
    assert await site_parsers.schedule(sm, llm, store) == {"parsers_requested": 1}


async def test_a_shop_with_qualified_prices_waits_for_a_person(app):
    sm, store = await _world(app)
    async with sm() as s:
        row = (await s.execute(select(LlmExtraction).limit(1))).scalar_one()
        row.output = {**row.output, "price_kind": "monthly"}
        await s.commit()
    await _generate(sm, store, SPEC)
    async with sm() as s:
        (p,) = (await s.execute(select(Parser))).scalars().all()
    # good enough by its score, but the shop shows a monthly price somewhere: a spec
    # would read it as the item's price, so it is not activated on its own
    assert p.validation_score >= 0.9 and not p.active
    assert p.artifact["_check"]["qualified_prices"] == 1


async def test_an_unsafe_answer_is_recorded_not_run(app):
    sm, store = await _world(app)
    bad = {**SPEC, "price": {"css": "span", "attr": None, "regex": "(a+)+", "index": 0}}
    await _generate(sm, store, bad)
    async with sm() as s:
        (p,) = (await s.execute(select(Parser))).scalars().all()
    assert not p.active and "repetition inside repetition" in p.artifact["error"]


async def test_a_new_version_learns_from_the_pages_the_current_one_left(app):
    sm, store = await _world(app)
    _, llm = await _generate(sm, store, SPEC)
    async with sm() as s:
        (old,) = (await s.execute(select(Parser))).scalars().all()
        assert old.active
    # the shop changes its template: the active parser finds no price on new pages,
    # so they go to the LLM tier
    site_parsers.forget()
    changed = {}
    for i in range(100, 114):
        html_text = _html(i).replace('class="price-now"', 'class="preis-neu"')
        changed[await _page(sm, store, i, html_text)] = i
    await stage.run_until_caught_up(sm, store, settle=NOW)
    await _read_by_llm(sm)
    assert await site_parsers.schedule(sm, llm, store) == {"parsers_requested": 1}
    async with sm() as s:
        batch = (
            (await s.execute(select(LlmBatch).where(LlmBatch.stage == site_parsers.STAGE)))
            .scalars()
            .all()[-1]
        )
    meta = batch.requests["0"]["meta"]
    sampled = meta["train"] + meta["held_out"]
    # only the pages of the new template: the old ones would let a version pass
    # that still misses the new price
    assert len(sampled) == 14 and {str(f) for f in changed} == set(sampled)


async def test_too_few_pages_and_the_switch(app):
    sm, store = await _world(app, n=8)  # fewer than parser_min_pages (12)
    llm = LlmService(sm, lambda: FakeClient())
    assert await site_parsers.schedule(sm, llm, store) == {"parsers_requested": 0}


def test_pages_are_decoded_by_their_declared_charset():
    page = '<html><head><meta charset="windows-1252"></head><body><p class="p">1.299 €</p>'
    spec = site_parser.parse({**SPEC, "price": {"css": "p.p", "attr": None, "regex": None,
                                                "index": 0}})  # fmt: skip
    assert site_parser.run(spec, page.encode("cp1252")).price_text == "1.299 €"
    assert site_parser.run(spec, page.replace("windows-1252", "utf-8").encode()).price_text == (
        "1.299 €"
    )
    undeclared = "<html><body><p class='p'>Möbel 5 €</p></body></html>"
    assert site_parser.run(spec, undeclared.encode()).price_text == "Möbel 5 €"
    assert site_parser.run(spec, undeclared.encode("cp1252")).price_text == "Möbel 5 €"
    assert site_parser.run(spec, b"").price_text is None
