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


def test_condensed_html_for_the_llm():
    t = site_parser.condensed_html(_html(1))
    assert t.startswith("<body>") and "tracking" not in t and "color:red" not in t
    assert 'class="product-name"' in t and "onclick" not in t
    assert site_parser.condensed_html(_html(1)) == t
    long = _html(1).replace("</table>", "</table>" + "<p class='x'>Text</p>" * 1000)
    assert len(site_parser.condensed_html(long, 5_000)) == 5_000


# --- generation and use ----------------------------------------------------------------


async def _page(sm, store, i, html_text=None, domain="holz.example"):
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
        f = RawFetch(id=uuid7(), fetched_at=datetime.now(UTC), url=url, final_url=url,
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
    assert row["active"] and row["held_out"] == [15, 15]
    runs = (await admin.get(f"/api/admin/parsers/{row['id']}/test")).json()
    assert runs and all(r["passes"] in (True, None) for r in runs)
    assert any(r["found"]["price"] == "1.299,00 €" for r in runs)
    r = await admin.post(f"/api/admin/parsers/{row['id']}/deactivate")
    assert r.status_code == 200 and not r.json()["active"]
    r = await admin.post(f"/api/admin/parsers/{row['id']}/activate")
    assert r.json()["active"]


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


async def test_an_unsafe_answer_is_recorded_not_run(app):
    sm, store = await _world(app)
    bad = {**SPEC, "price": {"css": "span", "attr": None, "regex": "(a+)+", "index": 0}}
    await _generate(sm, store, bad)
    async with sm() as s:
        (p,) = (await s.execute(select(Parser))).scalars().all()
    assert not p.active and "nested repetition" in p.artifact["error"]


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
