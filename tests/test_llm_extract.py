"""Tier-4 extraction: the LLM reads pages the other tiers left without a price or
sizes; its answer fills only those gaps and survives a rebuild without a new call."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from pos_coordinator import runtime_settings
from pos_coordinator.classify import shopmap
from pos_coordinator.derive import llm_tier, stage
from pos_coordinator.llm.service import LlmService
from pos_coordinator.models import (
    Classification,
    LlmExtraction,
    Measurement,
    Offer,
    Product,
    RawFetch,
    TaxonomyNode,
)
from pos_shared.extract import llm_page
from pos_shared.extract.model import Extraction, MeasurementCandidate, OfferCandidate
from tests.test_classification import _answer
from tests.test_derive import _fetch
from tests.test_llm import FakeClient

NOW = timedelta(0)

ANSWER = {
    "is_product_page": True,
    "brand": "Holzwerk",
    "price_text": "1.299,00 €",
    "currency": "EUR",
    "price_kind": "item",
    "availability": "in_stock",
    "condition": "new",
    "sizes": [
        {"label": "Maße (B x H x T)", "value": "80 x 180 x 40 cm"},
        {"label": "Gewicht", "value": "35 kg"},
    ],
    "category_path": ["Möbel", "Regale"],
}


def _page(title="Eichenregal Massiv", price_html="<span class='price'>1.299,00 €</span>"):
    """A page without structured data: the price only in a price block, sizes in a
    sentence the spec-table parser does not read."""
    return {
        "status": 200,
        "verdict": {"kind": "ok"},
        "snippets": {
            "lang": "de",
            "title": title,
            "meta": {"description": ["Massives Regal aus Eiche, geölt."]},
            "json_ld": [],
            "breadcrumbs": ["<nav class='breadcrumb'>Start > Möbel > Regale</nav>"],
            "tables": [],
            "definition_lists": [],
            "spec_blocks": ["<div class='details'>Das Regal misst 80 x 180 x 40 cm.</div>"],
            "price_blocks": [price_html],
        },
    }


def test_page_text_is_condensed_and_stable():
    t = llm_page.page_text(_page())
    assert t == llm_page.page_text(_page())
    assert t.startswith("Title: Eichenregal Massiv\nDescription: Massives Regal aus Eiche")
    assert "Breadcrumbs: Start > Möbel > Regale" in t
    assert "Price texts:\n- 1.299,00 €" in t and "Details:\n- Das Regal misst" in t
    huge = _page()
    huge["snippets"]["tables"] = [
        f"<table><tr><td>{'x' * 1900} {i}</td></tr></table>" for i in range(50)
    ]
    assert len(llm_page.page_text(huge)) <= llm_page.MAX_CHARS


def test_an_answer_fills_only_the_gaps():
    ex = Extraction(extract_version="t", title="Regal")
    assert llm_page.missing(ex) == ["price", "size"]
    assert llm_page.apply(ex, ANSWER, "de")
    (o,) = ex.offers
    assert (o.price, o.currency, o.method, o.price_raw) == (1299.0, "EUR", "llm", "1.299,00 €")
    (m,) = ex.measurements
    assert (m.a_m, m.b_m, m.c_m, m.mass_kg, m.method) == (1.8, 0.8, 0.4, 35.0, "llm")
    assert ex.category_path == ["Möbel", "Regale"] and ex.brand is None
    assert ex.sources == ["llm"]
    # a price found by the other tiers stays the only one
    ex = Extraction(extract_version="t", title="Regal", offers=[OfferCandidate(99.0, "EUR", "99")])
    ex.measurements.append(MeasurementCandidate("product", "labelled", mass_kg=3))
    assert llm_page.missing(ex) == []
    assert not llm_page.apply(ex, ANSWER, "de") and len(ex.offers) == 1
    # not a product page: nothing
    ex = Extraction(extract_version="t", title="Regale")
    assert not llm_page.apply(ex, {**ANSWER, "is_product_page": False}, "de")
    assert ex.offers == [] and ex.measurements == []
    # a monthly rate is flagged like the other tiers flag it
    ex = Extraction(extract_version="t", title="Sofa")
    llm_page.apply(ex, {**ANSWER, "price_text": "29 €", "price_kind": "monthly"}, "de")
    assert "monthly_price" in ex.flags


async def test_a_gap_is_asked_answered_and_kept_through_a_rebuild(app):
    sm, store = app.state.sessionmaker, app.state.rawstore
    fid = await _fetch(sm, store, _page(), "https://holz.example/regal", "holz")
    await stage.run_until_caught_up(sm, store, settle=NOW)
    async with sm() as s:
        (row,) = (await s.execute(select(LlmExtraction))).scalars().all()
        assert row.fetch_id == fid and row.status == "wanted"
        assert row.missing == ["price", "size"]
        assert (await s.execute(select(Offer))).scalars().all() == []
    fake = FakeClient()
    llm = LlmService(sm, lambda: fake)
    assert await llm_tier.schedule(sm, llm, store) == {"llm_extractions_queued": 1}
    (bid,) = fake.batches
    (req,) = fake.batches[bid]["requests"]
    assert req["params"]["messages"][0]["content"] == llm_page.page_text(_page())
    assert req["params"]["system"][0]["text"] == llm_tier.SYSTEM
    assert await llm_tier.schedule(sm, llm, store) == {"llm_extractions_queued": 0}  # in flight
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [_answer("0", json.dumps(ANSWER))]
    assert await llm.poll_batches() == 1
    async with sm() as s:
        row = (await s.execute(select(LlmExtraction))).scalar_one()
        assert row.status == "ok" and row.model == "claude-haiku-5-5"
        (o,) = (await s.execute(select(Offer))).scalars().all()
        assert float(o.price) == 1299 and o.fields["price"]["method"] == "llm"
        (m,) = (await s.execute(select(Measurement))).scalars().all()
        assert m.method == "llm" and m.mass_kg == 35
        p = await s.get(Product, o.product_id)
        assert float(p.canonical_price_eur) == 1299
        assert p.attributes["category_path"] == ["Möbel", "Regale"]
    assert await llm_tier.schedule(sm, llm, store) == {"llm_extractions_queued": 0}
    # a rebuild reads the stored answer: the same offer, no new question
    result = await stage.rebuild(sm, store, settle=NOW)
    assert result["identical"], result
    async with sm() as s:
        (o,) = (await s.execute(select(Offer))).scalars().all()
        assert float(o.price) == 1299
        assert len((await s.execute(select(LlmExtraction))).scalars().all()) == 1
    assert await llm_tier.schedule(sm, llm, store) == {"llm_extractions_queued": 0}
    assert len(fake.batches) == 1


async def test_answers_that_add_nothing(app):
    sm, store = app.state.sessionmaker, app.state.rawstore
    await _fetch(sm, store, _page("Regale"), "https://holz.example/regale", "holz")
    await _fetch(sm, store, _page("Kaputt"), "https://holz.example/x", "holz")
    await stage.run_until_caught_up(sm, store, settle=NOW)
    fake = FakeClient()
    llm = LlmService(sm, lambda: fake)
    assert await llm_tier.schedule(sm, llm, store) == {"llm_extractions_queued": 2}
    (bid,) = fake.batches
    async with sm() as s:
        rows = (await s.execute(select(LlmExtraction).order_by(LlmExtraction.created_at))).scalars()
        ids = [str(r.fetch_id) for r in rows]
    reqs = fake.batches[bid]["requests"]
    order = {json.loads(json.dumps(r))["custom_id"]: r for r in reqs}
    assert len(order) == 2
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [
        _answer("0", json.dumps({**ANSWER, "is_product_page": False})),
        _answer("1", "not json"),
    ]
    await llm.poll_batches()
    async with sm() as s:
        statuses = {
            str(r.fetch_id): r.status for r in (await s.execute(select(LlmExtraction))).scalars()
        }
        assert sorted(statuses.values()) == ["invalid", "ok"] and set(statuses) == set(ids)
        assert (await s.execute(select(Offer))).scalars().all() == []


async def test_the_switch_and_a_fetch_not_derived_yet(app):
    sm, store = app.state.sessionmaker, app.state.rawstore
    await _fetch(sm, store, _page(), "https://holz.example/regal", "holz")
    await stage.run_until_caught_up(sm, store, settle=NOW)
    llm = LlmService(sm, lambda: FakeClient())
    async with sm() as s:
        await runtime_settings.put(s, "llm_extraction", False, "test")
        await s.commit()
    assert await llm_tier.schedule(sm, llm, store) == {"llm_extractions_queued": 0}
    later = await _fetch(sm, store, _page("Neu"), "https://holz.example/neu", "holz")
    async with sm() as s:  # beyond the cursor: derived later, with the answer
        assert not await llm_tier.rederive(s, later)


async def test_a_page_without_evidence_never_holds_up_the_others(app, monkeypatch):
    sm, store = app.state.sessionmaker, app.state.rawstore
    first = await _fetch(sm, store, _page(), "https://holz.example/a", "holz")
    await _fetch(sm, store, _page("Buchenregal"), "https://holz.example/b", "holz")
    await stage.run_until_caught_up(sm, store, settle=NOW)
    async with sm() as s:
        await runtime_settings.put(s, "llm_extraction_per_round", 1, "test")
        sha = (await s.execute(select(RawFetch.blob_sha256).where(RawFetch.id == first))).scalar()
        await s.commit()
    real = store.exists
    monkeypatch.setattr(store, "exists", lambda kind, h: h != sha and real(kind, h))
    fake = FakeClient()
    llm = LlmService(sm, lambda: fake)
    # the oldest page's blob is missing: it steps back, and the next page is asked
    assert await llm_tier.schedule(sm, llm, store) == {"llm_extractions_queued": 0}
    assert await llm_tier.schedule(sm, llm, store) == {"llm_extractions_queued": 1}
    (bid,) = fake.batches
    (req,) = fake.batches[bid]["requests"]
    assert "Buchenregal" in json.dumps(req)
    async with sm() as s:
        row = (
            await s.execute(select(LlmExtraction).where(LlmExtraction.fetch_id == first))
        ).scalar_one()
        assert row.status == "wanted" and row.tried_at is not None


async def _answered(app, monkeypatch, store_up=True):
    sm, store = app.state.sessionmaker, app.state.rawstore
    fid = await _fetch(sm, store, _page(), "https://holz.example/regal", "holz")
    await stage.run_until_caught_up(sm, store, settle=NOW)
    fake = FakeClient()
    llm = LlmService(sm, lambda: fake)
    await llm_tier.schedule(sm, llm, store)
    (bid,) = fake.batches
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [_answer("0", json.dumps(ANSWER))]
    if not store_up:
        monkeypatch.setattr(store, "available", lambda: False)
    await llm.poll_batches()
    return sm, store, fid


async def test_an_answer_waits_for_the_raw_store(app, monkeypatch):
    sm, store, _ = await _answered(app, monkeypatch, store_up=False)
    async with sm() as s:
        row = (await s.execute(select(LlmExtraction))).scalar_one()
        assert row.status == "ok" and row.applied_at is None
        assert (await s.execute(select(Offer))).scalars().all() == []
    assert await llm_tier.apply_pending(sm) == 0  # the store is still away
    monkeypatch.undo()
    assert await llm_tier.apply_pending(sm) == 1
    async with sm() as s:
        (o,) = (await s.execute(select(Offer))).scalars().all()
        assert float(o.price) == 1299
        assert (await s.execute(select(LlmExtraction))).scalar_one().applied_at is not None
    assert await llm_tier.apply_pending(sm) == 0


def _priced_page():
    """A page whose price the structured data gives (an offer before any answer);
    the sizes are left to the LLM."""
    page = _page()
    page["snippets"]["price_blocks"] = []
    offer = {"price": "1299", "priceCurrency": "EUR"}
    page["snippets"]["json_ld"] = [
        json.dumps({"@type": "Product", "name": "Eichenregal", "offers": offer})
    ]
    return page


async def test_a_missing_blob_never_replaces_derived_rows_with_nothing(app, monkeypatch):
    sm, store = app.state.sessionmaker, app.state.rawstore
    await _fetch(sm, store, _priced_page(), "https://holz.example/regal", "holz")
    await stage.run_until_caught_up(sm, store, settle=NOW)
    fake = FakeClient()
    llm = LlmService(sm, lambda: fake)
    assert await llm_tier.schedule(sm, llm, store) == {"llm_extractions_queued": 1}  # sizes
    (bid,) = fake.batches
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [_answer("0", json.dumps(ANSWER))]
    # a partial restore: the store is there, this fetch's blob is not
    monkeypatch.setattr(store, "exists", lambda kind, sha: False)
    assert await llm.poll_batches() == 1
    async with sm() as s:
        (o,) = (await s.execute(select(Offer))).scalars().all()  # still there
        assert float(o.price) == 1299
        assert (await s.execute(select(Measurement))).scalars().all() == []
        row = (await s.execute(select(LlmExtraction))).scalar_one()
        assert row.status == "ok" and row.applied_at is None and row.tried_at
    monkeypatch.undo()
    assert await llm_tier.apply_pending(sm) == 0  # tried again only after a while
    async with sm() as s:
        row = (await s.execute(select(LlmExtraction))).scalar_one()
        row.tried_at -= llm_tier.RETRY_AFTER + timedelta(seconds=1)
        await s.commit()
    assert await llm_tier.apply_pending(sm) == 1
    async with sm() as s:
        (o,) = (await s.execute(select(Offer))).scalars().all()
        assert float(o.price) == 1299
        assert (await s.execute(select(Measurement))).scalars().all()  # from the answer
        assert (await s.execute(select(LlmExtraction))).scalar_one().applied_at is not None


async def test_gold_labels_keep_their_fetch_when_a_page_is_derived_again(app, monkeypatch):
    from pos_coordinator.models import GoldLabel

    sm, store = app.state.sessionmaker, app.state.rawstore
    fid = await _fetch(sm, store, _priced_page(), "https://holz.example/regal", "holz")
    await stage.run_until_caught_up(sm, store, settle=NOW)
    async with sm() as s:
        (o,) = (await s.execute(select(Offer))).scalars().all()
        s.add(GoldLabel(offer_id=o.id, product_id=o.product_id, fetch_id=fid,
                        labels={"price": 1299}, labelled_by="person"))  # fmt: skip
        await s.commit()
    fake = FakeClient()
    llm = LlmService(sm, lambda: fake)
    assert await llm_tier.schedule(sm, llm, store) == {"llm_extractions_queued": 1}  # sizes
    (bid,) = fake.batches
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [_answer("0", json.dumps(ANSWER))]
    assert await llm.poll_batches() == 1
    async with sm() as s:
        (g,) = (await s.execute(select(GoldLabel))).scalars().all()
        assert g.offer_id is None and g.fetch_id == fid
        assert (await s.execute(select(Measurement))).scalars().all()  # sizes added


def test_an_answer_never_changes_a_product_identity():
    ex = Extraction(extract_version="t", title="Regal", mpn="R-80")
    llm_page.apply(ex, ANSWER, "de")
    assert ex.brand is None  # a brand would turn the url: key into a bm: key


def _ld_page(gtin, title, category=None, dims=None):
    product = {"@type": "Product", "name": title, "gtin13": gtin,
               "offers": {"@type": "Offer", "price": "199", "priceCurrency": "EUR"}}  # fmt: skip
    if category:
        product["category"] = category
    tables = [f"<table><tr><th>Maße</th><td>{dims}</td></tr></table>"] if dims else []
    return {
        "status": 200,
        "verdict": {"kind": "ok"},
        "snippets": {
            "lang": "de",
            "title": title,
            "json_ld": [json.dumps(product)],
            "tables": tables,
            "definition_lists": [],
            "spec_blocks": [],
            "price_blocks": [],
        },  # fmt: skip
    }


async def _product(sm):
    async with sm() as s:
        return (await s.execute(select(Product))).scalar_one()


async def test_breadcrumbs_of_another_shop_never_become_the_products(app):
    sm, store = app.state.sessionmaker, app.state.rawstore
    gtin = "4006381333931"
    await _fetch(sm, store, _ld_page(gtin, "Regal", dims="80 x 40 x 180 cm"),
                 "https://a.example/p", "a")  # fmt: skip
    await _fetch(sm, store, _ld_page(gtin, "Regal", "Möbel > Regale", "80 x 40 x 180 cm"),
                 "https://b.example/p", "b")  # fmt: skip
    await stage.run_until_caught_up(sm, store, settle=NOW)
    p = await _product(sm)
    assert p.source == "a" and p.attributes["category_path"] == []


async def test_the_earliest_fetch_with_breadcrumbs_names_the_category(app):
    sm, store = app.state.sessionmaker, app.state.rawstore
    gtin = "4006381333931"
    # the earlier page has no breadcrumbs (and no sizes: the LLM is asked about it)
    first = await _fetch(sm, store, _ld_page(gtin, "Regal"), "https://a.example/p1", "a")
    await _fetch(sm, store, _ld_page(gtin, "Regal", "Möbel > Spätere", "80 x 40 x 180 cm"),
                 "https://a.example/p2", "a")  # fmt: skip
    await stage.run_until_caught_up(sm, store, settle=NOW)
    assert (await _product(sm)).attributes["category_path"] == ["Möbel", "Spätere"]
    # the later path is mapped and classifies the product
    async with sm() as s:
        node = TaxonomyNode(name="Shelves", path="Furniture > Shelves", depth=1)
        s.add(node)
        await s.flush()
        await shopmap.set_mapping(s, "a", "Möbel > Spätere", node.id)
        await s.commit()
    await stage.classify_until_settled(sm)
    assert (await _product(sm)).category_id == node.id
    fake = FakeClient()
    llm = LlmService(sm, lambda: fake)
    assert await llm_tier.schedule(sm, llm, store) == {"llm_extractions_queued": 1}
    (bid,) = fake.batches
    answer = {**ANSWER, "category_path": ["Möbel", "Frühere"]}
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [_answer("0", json.dumps(answer))]
    await llm.poll_batches()
    async with sm() as s:
        assert (await s.execute(select(LlmExtraction.fetch_id))).scalar_one() == first
    # the earlier fetch's breadcrumbs (from the answer) take precedence, as in a rebuild
    assert (await _product(sm)).attributes["category_path"] == ["Möbel", "Frühere"]
    # ... and the earlier path has no mapping: the later one's classification goes
    assert await stage.classify_until_settled(sm) == 1
    product = await _product(sm)
    assert product.category_id is None
    async with sm() as s:
        current = (
            await s.execute(
                select(Classification).where(
                    Classification.product_id == product.id, Classification.is_current
                )
            )
        ).scalar_one_or_none()
        assert current is None
    result = await stage.rebuild(sm, store, settle=NOW)
    assert result["identical"], result
    assert (await _product(sm)).attributes["category_path"] == ["Möbel", "Frühere"]
    assert (await _product(sm)).category_id is None


def _cut_off(custom_id):
    """A batch answer that ran out of tokens while thinking: no text at all."""
    from types import SimpleNamespace

    from tests.test_llm import _msg

    message = _msg("", stop="max_tokens")
    message.content = message.content[:1]  # the thinking block only
    return SimpleNamespace(
        custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=message)
    )


async def test_a_cut_off_answer_is_asked_once_more(app):
    sm, store = app.state.sessionmaker, app.state.rawstore
    await _fetch(sm, store, _page(), "https://holz.example/regal", "holz")
    await stage.run_until_caught_up(sm, store, settle=NOW)
    fake = FakeClient()
    llm = LlmService(sm, lambda: fake)

    async def answer(result) -> LlmExtraction:
        assert await llm_tier.schedule(sm, llm, store) == {"llm_extractions_queued": 1}
        bid = list(fake.batches)[-1]
        fake.batches[bid]["status"] = "ended"
        fake.batches[bid]["results"] = [result]
        assert await llm.poll_batches() == 1
        async with sm() as s:
            return (await s.execute(select(LlmExtraction))).scalar_one()

    row = await answer(_cut_off("0"))
    assert row.status == "invalid"
    assert row.output == {"detail": "no complete answer (stop_reason max_tokens)"}
    # asked once more (an answer cut off under an earlier release's tighter limit)
    row = await answer(_cut_off("0"))
    assert row.status == "invalid" and row.output["requeued"]
    # cut off again: not a third time
    assert await llm_tier.schedule(sm, llm, store) == {"llm_extractions_queued": 0}
    assert len(fake.batches) == 2


async def test_a_requeued_answer_that_works_is_applied(app):
    sm, store = app.state.sessionmaker, app.state.rawstore
    fid = await _fetch(sm, store, _page(), "https://holz.example/regal", "holz")
    await stage.run_until_caught_up(sm, store, settle=NOW)
    async with sm() as s:  # as an earlier release left it
        row = (await s.execute(select(LlmExtraction))).scalar_one()
        row.status, row.output = "invalid", {"detail": "no answer"}
        row.answered_at = row.applied_at = datetime.now(UTC)  # a failure counts as applied
        await s.commit()
    fake = FakeClient()
    llm = LlmService(sm, lambda: fake)
    assert await llm_tier.schedule(sm, llm, store) == {"llm_extractions_queued": 1}
    async with sm() as s:
        row = (await s.execute(select(LlmExtraction))).scalar_one()
        assert row.status == "wanted" and row.applied_at is None and row.answered_at is None
    (bid,) = fake.batches
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [_answer("0", json.dumps(ANSWER))]
    assert await llm.poll_batches() == 1
    async with sm() as s:
        row = (await s.execute(select(LlmExtraction))).scalar_one()
        assert row.fetch_id == fid and row.status == "ok"
        (o,) = (await s.execute(select(Offer))).scalars().all()
        assert float(o.price) == 1299
