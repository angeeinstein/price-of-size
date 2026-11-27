"""Price validity: category outliers (robust z on price per kg and m³) and the LLM
review of flagged offers; the rebuild drill with both."""

import json
import math
import uuid
from datetime import timedelta

from sqlalchemy import select

from pos_coordinator import runtime_settings
from pos_coordinator.classify import common, taxonomy
from pos_coordinator.derive import stage
from pos_coordinator.llm.service import LlmService
from pos_coordinator.models import LlmBatch, Offer, PriceReview, Product, TaxonomyNode
from pos_coordinator.validity import outliers, review
from tests.test_classification import _answer
from tests.test_derive import _evidence, _fetch
from tests.test_llm import FakeClient
from tests.test_taxonomy import _google, _google_de

NOW = timedelta(0)


def test_robust_z_with_a_parent_for_small_categories():
    root, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    parents = {root: None, a: root, b: root}
    rows = []
    # 30 shelves around 10 EUR/kg (spread by a factor up to 1.3), one at 1000 EUR/kg
    for i in range(30):
        rows.append((uuid.uuid4(), a, 10 * 1.3 ** ((i % 7) / 6 - 0.5) * 20, 20.0, None))
    odd = uuid.uuid4()
    rows.append((odd, a, 1000 * 20, 20.0, None))
    # a small category (5 products) is judged by its parent's distribution
    small = [uuid.uuid4() for _ in range(5)]
    rows += [(p, b, 11 * 20, 20.0, None) for p in small[:4]]
    rows.append((small[4], b, 0.05 * 20, 20.0, 0.1))
    found = outliers.find(rows, parents, z_limit=3.5)
    assert set(found) == {odd, small[4]}
    assert found[odd].group_kg == a and found[odd].z_kg > 3.5
    assert found[small[4]].group_kg == root and found[small[4]].z_kg < -3.5
    assert found[small[4]].z_m3 is None  # nobody else has a volume to compare with
    assert outliers.find(rows, parents, z_limit=100) == {}


async def _set(sm, key, value):
    async with sm() as s:
        await runtime_settings.put(s, key, value, "test")
        await s.commit()


async def _shelves(app, n=25, odd_price="2500.00"):
    """n shelves of 20 kg at about 100 EUR, one at odd_price; all in one category."""
    sm, store = app.state.sessionmaker, app.state.rawstore
    async with sm() as s:
        await taxonomy.import_google(s, {"en": _google(), "de": _google_de()})
        await s.commit()
    for i in range(n):
        price = odd_price if i == 0 else f"{95 + i % 10}.00"
        ev = _evidence(f"400638133{i:04d}", price, "EUR", name=f"Regal {i}", mass="20 kg")
        ev["snippets"]["price_blocks"] = [f"<div class='price'>{price} € <s>UVP</s></div>"]
        await _fetch(sm, store, ev, f"https://shop.example/p/{i}", "shop")
    await stage.run_until_caught_up(sm, store, settle=NOW)
    async with sm() as s:
        shelf = (
            await s.execute(
                select(TaxonomyNode.id).where(TaxonomyNode.path == "Furniture > Shelving")
            )
        ).scalar_one()
        products = (await s.execute(select(Product).order_by(Product.title))).scalars().all()
        for p in products:
            await common.assign(s, p.id, shelf, "manual", 1.0, "test")
        await s.commit()
    odd = next(p for p in products if p.title == "Regal 0")
    return sm, store, odd


async def _offer(sm, product_id):
    async with sm() as s:
        return (await s.execute(select(Offer).where(Offer.product_id == product_id))).scalar_one()


async def test_category_outliers_are_flagged_and_cleared(app):
    sm, _, odd = await _shelves(app)
    async with sm() as s:
        r = await outliers.run(s)
        await s.commit()
    assert r == {"category_outliers": 1, "flagged": 1, "cleared": 0}
    o = await _offer(sm, odd.id)
    assert outliers.FLAG in o.flags
    check = o.fields["category_check"]
    assert check["version"] == outliers.VERSION and check["z_kg"] > 3.5
    assert check["z_m3"] is not None  # the shelves have sizes too
    async with sm() as s:
        assert (await outliers.run(s))["flagged"] == 0  # nothing new
    async with sm() as s:  # the price is corrected: no outlier any more
        (await s.get(Product, odd.id)).canonical_price_eur = 99
        r = await outliers.run(s)
        await s.commit()
    assert r["cleared"] == 1
    o = await _offer(sm, odd.id)
    assert outliers.FLAG not in o.flags and "category_check" not in o.fields


async def test_flagged_offers_are_reviewed(app):
    sm, store, odd = await _shelves(app)
    async with sm() as s:
        await outliers.run(s)
        await s.commit()
    fake = FakeClient()
    llm = LlmService(sm, lambda: fake)
    assert await review.schedule(sm, llm, store) == {"price_reviews_queued": 1}
    (bid,) = fake.batches
    (req,) = fake.batches[bid]["requests"]
    text = req["params"]["messages"][0]["content"]
    assert "Product: Regal 0" in text and "Weight: 20 kg" in text
    assert 'Price: 2500.0000 EUR (as written: "2500.00")' in text
    assert "Price text on the page:\n- 2500.00 € UVP" in text
    assert "category" not in text.split("Price text")[0].lower().replace("shop category", "")
    # while the batch runs nothing is asked twice
    assert await review.schedule(sm, llm, store) == {"price_reviews_queued": 0}
    answer = {"verdict": "wrong_price_type", "confidence": "high", "reason": "UVP, not the price."}
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [_answer("0", json.dumps(answer))]
    assert await llm.poll_batches() == 1
    o = await _offer(sm, odd.id)
    assert review.FLAG in o.flags
    async with sm() as s:
        (r,) = (await s.execute(select(PriceReview))).scalars().all()
        assert r.verdict == "wrong_price_type" and r.confidence == 0.9
        assert r.validator_version == "price-review-1/claude-haiku-5-5"
        # the rejected offer no longer makes the product's price
        assert (await s.get(Product, odd.id)).canonical_price_eur is None
    assert await review.schedule(sm, llm, store) == {"price_reviews_queued": 0}  # answered


async def test_a_valid_verdict_or_a_low_confidence_rejection_changes_nothing(app):
    assert not review.rejects("valid", "high")
    assert not review.rejects("placeholder", "low")
    assert review.rejects("pack_mismatch", "medium")
    sm, store, odd = await _shelves(app)
    async with sm() as s:
        await outliers.run(s)
        await s.commit()
    fake = FakeClient()
    llm = LlmService(sm, lambda: fake)
    await review.schedule(sm, llm, store)
    (bid,) = fake.batches
    answer = {"verdict": "valid", "confidence": "medium", "reason": "Solid oak, priced so."}
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [_answer("0", json.dumps(answer))]
    await llm.poll_batches()
    o = await _offer(sm, odd.id)
    assert review.FLAG not in o.flags
    async with sm() as s:
        assert math.isclose(float((await s.get(Product, odd.id)).canonical_price_eur), 2500)


async def test_the_switch_and_a_missing_raw_store(app):
    sm, store, _ = await _shelves(app)
    async with sm() as s:
        await outliers.run(s)
        await s.commit()
    llm = LlmService(sm, lambda: FakeClient())
    await _set(sm, "price_review", False)
    assert await review.schedule(sm, llm, store) == {"price_reviews_queued": 0}
    await _set(sm, "price_review", True)

    class Gone:
        def available(self):
            return False

    assert await review.schedule(sm, llm, Gone()) == {"price_reviews_queued": 0}
    async with sm() as s:
        assert (await s.execute(select(LlmBatch))).scalars().all() == []


async def test_rebuild_with_reviews_and_outliers(app):
    sm, store, odd = await _shelves(app)
    async with sm() as s:
        await outliers.run(s)
        await s.commit()
    fake = FakeClient()
    llm = LlmService(sm, lambda: fake)
    await review.schedule(sm, llm, store)
    (bid,) = fake.batches
    answer = {"verdict": "placeholder", "confidence": "high", "reason": "Filler."}
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [_answer("0", json.dumps(answer))]
    await llm.poll_batches()
    result = await stage.rebuild(sm, store, settle=NOW)
    # the review and the outlier flag come back later; the rest is the same
    assert result["identical"], result
    assert result["before"]["price_review"] == 1 and result["after"]["price_review"] == 0
    # once the categories are back, the same question is answered from the ledger
    async with sm() as s:
        shelf = (
            await s.execute(
                select(TaxonomyNode.id).where(TaxonomyNode.path == "Furniture > Shelving")
            )
        ).scalar_one()
        for p in (await s.execute(select(Product))).scalars().all():
            await common.assign(s, p.id, shelf, "manual", 1.0, "test")
        await outliers.run(s)
        await s.commit()
    assert await review.schedule(sm, llm, store) == {"price_reviews_queued": 1}
    assert len(fake.batches) == 1  # no new batch: from the ledger
    async with sm() as s:
        odd_now = (await s.execute(select(Product).where(Product.title == "Regal 0"))).scalar_one()
    assert review.FLAG in (await _offer(sm, odd_now.id)).flags


async def test_price_checks_endpoint(app, admin):
    sm, store, odd = await _shelves(app)
    async with sm() as s:
        await outliers.run(s)
        await s.commit()
    fake = FakeClient()
    llm = LlmService(sm, lambda: fake)
    await review.schedule(sm, llm, store)
    (bid,) = fake.batches
    answer = {"verdict": "placeholder", "confidence": "high", "reason": "Filler."}
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [_answer("0", json.dumps(answer))]
    await llm.poll_batches()
    body = (await admin.get("/api/admin/price-checks")).json()
    assert body["flagged"]["category_outlier"] == 1 and body["flagged"]["review_rejected"] == 1
    assert body["reviews"] == [
        {"version": "price-review-1/claude-haiku-5-5", "verdict": "placeholder", "offers": 1}
    ]
    (row,) = body["latest"]
    assert row["title"] == "Regal 0" and row["reason"] == "Filler."
    assert (await admin.get("/api/admin/price-checks", params={"verdict": "valid"})).json()[
        "latest"
    ] == []
    assert (await admin.get("/api/admin/price-checks", params={"verdict": "x"})).status_code == 422


def test_a_group_of_repeated_prices_still_shows_its_outliers():
    root = uuid.uuid4()
    rows = [(uuid.uuid4(), root, 10.0 * 20, 20.0, None) for _ in range(20)]  # all 10 EUR/kg
    odd = uuid.uuid4()
    rows.append((odd, root, 1000.0 * 20, 20.0, None))
    found = outliers.find(rows, {root: None}, z_limit=3.5)
    assert set(found) == {odd} and found[odd].z_kg > 3.5


async def test_mass_and_volume_may_come_from_different_measurements(app):
    from pos_coordinator.models import Measurement

    sm, _, odd = await _shelves(app)
    async with sm() as s:  # weight and size split over two rows, the size row first
        for m in (await s.execute(select(Measurement))).scalars().all():
            s.add(Measurement(product_id=m.product_id, basis="product", basis_method="labelled",
                              mass_kg=m.mass_kg))  # fmt: skip
            m.mass_kg = None
        await s.commit()
        r = await outliers.run(s)
        await s.commit()
    assert r["category_outliers"] == 1
    o = await _offer(sm, odd.id)
    assert o.fields["category_check"]["z_kg"] > 3.5 and o.fields["category_check"]["z_m3"] > 3.5


async def test_a_review_waits_for_the_derive_runner(app):
    import asyncio

    from pos_coordinator.derive.stage import _cursor
    from pos_coordinator.llm.service import Outcome

    sm, _, odd = await _shelves(app)
    o = await _offer(sm, odd.id)
    outcome = Outcome(
        "ok",
        review.Review(verdict="placeholder", confidence="high", reason="Filler."),
        "claude-haiku-5-5",
        review.STAGE,
        meta={"offer_id": str(o.id), "version": "v"},
    )
    async with sm() as runner:  # a derive batch in progress
        await _cursor(runner)

        async def deliver():
            async with sm() as s:
                await review.handle(s, outcome)
                await s.commit()

        task = asyncio.create_task(deliver())
        await asyncio.sleep(0.3)
        assert not task.done()
        await runner.commit()
    await task
    assert review.FLAG in (await _offer(sm, odd.id)).flags


async def test_the_outlier_pass_never_undoes_a_review_meanwhile(app):
    import asyncio

    sm, _, odd = await _shelves(app)
    async with sm() as s:
        await outliers.run(s)
        await s.commit()
    async with sm() as s:  # the price is corrected: the next pass clears the flag
        (await s.get(Product, odd.id)).canonical_price_eur = 99
        await s.commit()
    o = await _offer(sm, odd.id)
    async with sm() as reviewer:  # a review rejects the offer, not committed yet
        locked = await reviewer.get(Offer, o.id, with_for_update=True)
        locked.flags = sorted(set(locked.flags) | {review.FLAG})
        await reviewer.flush()

        async def outlier_pass():
            async with sm() as s:
                r = await outliers.run(s)
                await s.commit()
                return r

        task = asyncio.create_task(outlier_pass())
        await asyncio.sleep(0.3)
        await reviewer.commit()
    assert (await task)["cleared"] == 1
    flags = (await _offer(sm, odd.id)).flags
    assert review.FLAG in flags and outliers.FLAG not in flags


async def test_a_new_review_version_asks_about_rejected_offers_again(app, monkeypatch):
    sm, store, odd = await _shelves(app)
    async with sm() as s:
        await outliers.run(s)
        await s.commit()
    fake = FakeClient()
    llm = LlmService(sm, lambda: fake)
    await review.schedule(sm, llm, store)
    (bid,) = fake.batches
    answer = {"verdict": "placeholder", "confidence": "high", "reason": "Filler."}
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [_answer("0", json.dumps(answer))]
    await llm.poll_batches()
    async with sm() as s:  # the checks drop the rejected offer: only the rejection is left
        await outliers.run(s)
        await s.commit()
    assert (await _offer(sm, odd.id)).flags == [review.FLAG]
    assert await review.schedule(sm, llm, store) == {"price_reviews_queued": 0}
    monkeypatch.setattr(review, "PROMPT", "price-review-2")
    assert await review.schedule(sm, llm, store) == {"price_reviews_queued": 1}
