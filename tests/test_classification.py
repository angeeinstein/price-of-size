"""Classification tiers: precedence, k-NN, LLM picks from a shortlist, LLM mapping
proposals, and the admin endpoints. Vectors come from a deterministic fake model,
answers from a fake Claude API client."""

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from pos_coordinator import runtime_settings
from pos_coordinator.classify import by_llm, common, embeddings, knn, proposals, shopmap, taxonomy
from pos_coordinator.derive import stage
from pos_coordinator.llm.service import LlmService
from pos_coordinator.models import (
    CategoryMapping,
    Classification,
    Job,
    LlmBatch,
    Product,
    TaxonomyNode,
)
from pos_shared import embedding
from tests.test_embeddings import FakeModel
from tests.test_llm import FakeClient, _msg
from tests.test_taxonomy import _google, _google_de


async def _taxonomy(sm):
    async with sm() as s:
        await taxonomy.import_google(s, {"en": _google(), "de": _google_de()})
        await s.commit()


async def _products(sm, titles, source="shop", path=None, prefix="p"):
    ids = []
    async with sm() as s:
        for i, title in enumerate(titles):
            p = Product(
                match_key=f"url:https://{source}.example/{prefix}{i}",
                source=source,
                title=title,
                attributes={"category_path": path} if path else {},
            )
            s.add(p)
            await s.flush()
            ids.append(p.id)
        await s.commit()
    return ids


async def _embed_all(sm):
    model = FakeModel()
    while True:
        async with sm() as s:
            await embeddings.enqueue(s)
            jobs = (
                (await s.execute(select(Job).where(Job.type == "embed", Job.status == "queued")))
                .scalars()
                .all()
            )
            if not jobs:
                await s.commit()
                return
            for j in jobs:
                items = j.payload["items"]
                vecs = model.embed([it["text"] for it in items])
                out = {
                    "model": j.payload["model"],
                    "ids": [it["id"] for it in items],
                    "vectors": embedding.encode(vecs),
                }
                await embeddings.ingest(s, j, out)
                j.status = "done"
            await s.commit()


async def _node(sm, path):
    async with sm() as s:
        return (
            await s.execute(select(TaxonomyNode.id).where(TaxonomyNode.path == path))
        ).scalar_one()


async def _current(sm, pid):
    async with sm() as s:
        return (
            await s.execute(
                select(Classification).where(
                    Classification.product_id == pid, Classification.is_current
                )
            )
        ).scalar_one_or_none()


async def _set(sm, key, value):
    async with sm() as s:
        await runtime_settings.put(s, key, value, "test")
        await s.commit()


# --- precedence -----------------------------------------------------------------------


async def test_a_tier_never_replaces_a_higher_one(sessionmaker):
    await _taxonomy(sessionmaker)
    shelf = await _node(sessionmaker, "Furniture > Shelving")
    aircraft = await _node(sessionmaker, "Vehicles & Parts > Vehicles > Aircraft")
    a, b = await _products(sessionmaker, ["Regal", "Regal 2"], path=["Möbel", "Regale"])
    async with sessionmaker() as s:
        assert await common.assign(s, a, aircraft, "knn", 0.9, "knn-test")
        assert await common.assign(s, b, aircraft, "llm", 0.9, "llm-test")
        assert not await common.assign(s, b, aircraft, "knn", 0.99, "knn-test")  # attempt only
        await shopmap.set_mapping(s, "shop", "Möbel > Regale", shelf)
        await shopmap.classify(s)
        await s.commit()
    # the shop mapping replaced the k-NN guess, not the LLM's choice
    assert (await _current(sessionmaker, a)).tier == "shop_mapping"
    cur_b = await _current(sessionmaker, b)
    assert cur_b.tier == "llm" and cur_b.taxonomy_node_id == aircraft
    async with sessionmaker() as s:
        tiers = (
            await s.execute(select(Classification.tier).where(Classification.product_id == b))
        ).scalars()
        assert sorted(tiers) == ["knn", "llm"]
        assert (await s.get(Product, a)).category_id == shelf


async def test_a_confirmed_mapping_updates_the_confidence(sessionmaker):
    await _taxonomy(sessionmaker)
    shelf = await _node(sessionmaker, "Furniture > Shelving")
    (a,) = await _products(sessionmaker, ["Regal"], path=["Möbel", "Regale"])
    async with sessionmaker() as s:
        await shopmap.set_mapping(s, "shop", "Möbel > Regale", shelf, "llm", confidence=0.7)
        await shopmap.classify(s)
        await shopmap.set_mapping(s, "shop", "Möbel > Regale", shelf, "manual")
        assert (await shopmap.classify(s))["classified"] == 1
        await s.commit()
    assert (await _current(sessionmaker, a)).confidence == 1.0


# --- k-NN -----------------------------------------------------------------------------


async def _labelled_world(sm, monkeypatch):
    monkeypatch.setattr(knn, "MIN_EXAMPLES", 10)
    await _taxonomy(sm)
    shelf = await _node(sm, "Furniture > Shelving")
    item = await _node(sm, "Office Supplies > Item 5")
    shelves = await _products(sm, ["Regal Eiche"] * 10, prefix="s")
    laptops = await _products(sm, ["Laptop Pro"] * 10, prefix="l")
    await _embed_all(sm)
    async with sm() as s:
        v = await by_llm.version(s, "claude-haiku-5-5")  # as if the LLM had labelled them
        for pid in shelves:
            await common.assign(s, pid, shelf, "llm", 0.9, v)
        for pid in laptops:
            await common.assign(s, pid, item, "llm", 0.9, v)
        await s.commit()
    return shelf, item


async def test_knn_classifies_what_its_neighbours_agree_on(sessionmaker, monkeypatch):
    shelf, _ = await _labelled_world(sessionmaker, monkeypatch)
    sure, unsure = await _products(
        sessionmaker, ["Regal Eiche Buche", "Regal Eiche Laptop"], prefix="q"
    )
    await _embed_all(sessionmaker)
    async with sessionmaker() as s:
        r = await knn.classify(s)
        await s.commit()
    assert r == {"knn_classified": 1, "knn_unsure": 1}
    cur = await _current(sessionmaker, sure)
    assert cur.tier == "knn" and cur.taxonomy_node_id == shelf and cur.confidence >= 0.8
    assert cur.classifier_version.startswith(f"knn-1/k15/{embedding.MODEL_KEY}/g0/e")
    assert await _current(sessionmaker, unsure) is None  # 0.75 < 0.8: kept as an attempt
    async with sessionmaker() as s:
        guess = (
            await s.execute(select(Classification).where(Classification.product_id == unsure))
        ).scalar_one()
        assert not guess.is_current and guess.taxonomy_node_id == shelf
        assert guess.confidence == pytest.approx(0.75, abs=0.01)
        assert await knn.classify(s) == {"knn_classified": 0, "knn_unsure": 0}  # done
    # a new generation runs the classifier again
    await _set(sessionmaker, "classification_generation", 1)
    async with sessionmaker() as s:
        assert sum((await knn.classify(s)).values()) == 2
        await s.commit()
    assert "/g1/" in (await _current(sessionmaker, sure)).classifier_version


async def test_knn_evaluation(sessionmaker, monkeypatch):
    await _labelled_world(sessionmaker, monkeypatch)
    async with sessionmaker() as s:
        r = await knn.evaluate(s)
    assert r["examples"] == 20 and r["evaluated"] == 20
    by_t = {t["threshold"]: t for t in r["thresholds"]}
    # each example: 9 of its own kind (similarity 1) and 6 of the other (1/3)
    assert by_t[0.8]["coverage"] == 1.0 and by_t[0.8]["accuracy"] == 1.0
    assert by_t[0.9]["coverage"] == 0.0


async def test_knn_waits_for_enough_examples(sessionmaker):
    await _taxonomy(sessionmaker)
    await _products(sessionmaker, ["Regal"])
    await _embed_all(sessionmaker)
    async with sessionmaker() as s:
        assert await knn.classify(s) == {"knn_classified": 0, "knn_unsure": 0}


# --- LLM classification -----------------------------------------------------------------


def _answer(custom_id, payload):
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(type="succeeded", message=_msg(payload)),
    )


async def test_llm_picks_from_the_shortlist(sessionmaker):
    await _taxonomy(sessionmaker)
    a, b, c = await _products(
        sessionmaker, ["Regal Eiche", "Mystery Thing", "Laptop Pro"], path=["Möbel", "Regale"]
    )
    await _embed_all(sessionmaker)
    fake = FakeClient()
    llm = LlmService(sessionmaker, lambda: fake)
    assert await by_llm.schedule(sessionmaker, llm) == {"llm_classify_queued": 3}
    (bid,) = fake.batches
    reqs = fake.batches[bid]["requests"]
    assert len(reqs) == 3
    params = reqs[0]["params"]
    assert params["system"][0]["text"] == by_llm.SYSTEM
    assert params["model"] == "claude-haiku-5-5" and params["max_tokens"] == 64
    text = params["messages"][0]["content"]
    assert "Shop category: Möbel > Regale" in text and "Price" not in text
    assert "\nCandidates:\n1. " in text and "\n20. " in text
    async with sessionmaker() as s:
        row = (await s.execute(select(LlmBatch))).scalar_one()
    by_pid = {v["meta"]["product_id"]: k for k, v in row.requests.items()}
    cands = {k: v["meta"]["candidates"] for k, v in row.requests.items()}
    # nothing is asked twice while the batch runs
    assert await by_llm.schedule(sessionmaker, llm) == {"llm_classify_queued": 0}
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [
        _answer(by_pid[str(a)], '{"choice": 2, "confidence": "high"}'),
        _answer(by_pid[str(b)], '{"choice": 0, "confidence": "medium"}'),
        _answer(by_pid[str(c)], "not json"),
    ]
    assert await llm.poll_batches() == 3
    cur = await _current(sessionmaker, a)
    assert cur.tier == "llm" and cur.confidence == 0.9
    assert str(cur.taxonomy_node_id) == cands[by_pid[str(a)]][1]
    assert cur.classifier_version.startswith("llm-classify-1/claude-haiku-5-5/2021-09-21+pos-1.")
    assert await _current(sessionmaker, b) is None and await _current(sessionmaker, c) is None
    # every product was answered under this version: nothing more to ask
    assert await by_llm.schedule(sessionmaker, llm) == {"llm_classify_queued": 0}
    # a new generation asks again: the valid answers come from the ledger at once
    await _set(sessionmaker, "classification_generation", 1)
    assert await by_llm.schedule(sessionmaker, llm) == {"llm_classify_queued": 3}
    assert len(fake.batches) == 2
    (again,) = [k for k in fake.batches if k != bid]
    assert len(fake.batches[again]["requests"]) == 1  # only the invalid one
    assert (await _current(sessionmaker, a)).classifier_version.endswith("/g1")


async def test_llm_classification_respects_settings_and_knn(sessionmaker, monkeypatch):
    await _labelled_world(sessionmaker, monkeypatch)
    (q,) = await _products(sessionmaker, ["Regal Eiche Laptop"], prefix="q")
    await _embed_all(sessionmaker)
    fake = FakeClient()
    llm = LlmService(sessionmaker, lambda: fake)
    # k-NN is active: the product waits for its k-NN attempt
    assert await by_llm.schedule(sessionmaker, llm) == {"llm_classify_queued": 0}
    async with sessionmaker() as s:
        await knn.classify(s)
        await s.commit()
    await _set(sessionmaker, "classify_llm", False)
    assert await by_llm.schedule(sessionmaker, llm) == {"llm_classify_queued": 0}
    await _set(sessionmaker, "classify_llm", True)
    assert await by_llm.schedule(sessionmaker, llm) == {"llm_classify_queued": 1}
    (bid,) = fake.batches
    assert json.dumps(fake.batches[bid]["requests"]).count(str(q)) == 0  # ids stay out
    async with sessionmaker() as s:
        row = (await s.execute(select(LlmBatch))).scalar_one()
    assert [r["meta"]["product_id"] for r in row.requests.values()] == [str(q)]


async def test_an_answer_off_the_list_is_an_attempt(sessionmaker):
    await _taxonomy(sessionmaker)
    shelf = await _node(sessionmaker, "Furniture > Shelving")
    (a,) = await _products(sessionmaker, ["Regal"])
    out = SimpleNamespace(
        status="ok",
        data=by_llm.Pick(choice=2, confidence="low"),
        meta={"product_id": str(a), "candidates": [str(shelf)], "version": "v"},
    )
    async with sessionmaker() as s:
        await by_llm.handle(s, out)
        (row,) = (await s.execute(select(Classification))).scalars().all()
    assert not row.is_current and row.taxonomy_node_id is None and row.confidence is None


async def test_a_rebuilt_product_is_skipped(sessionmaker):
    await _taxonomy(sessionmaker)
    shelf = await _node(sessionmaker, "Furniture > Shelving")
    out = SimpleNamespace(
        status="ok",
        data=by_llm.Pick(choice=1, confidence="high"),
        meta={"product_id": str(uuid.uuid4()), "candidates": [str(shelf)], "version": "v"},
    )
    async with sessionmaker() as s:
        await by_llm.handle(s, out)
        assert (await s.execute(select(Classification))).scalars().all() == []


# --- mapping proposals ------------------------------------------------------------------


async def test_mapping_proposals(sessionmaker, admin):
    await _taxonomy(sessionmaker)
    shelf = await _node(sessionmaker, "Furniture > Shelving")
    regal = await _products(
        sessionmaker, ["Regal Eiche", "Regal Buche", "Wandregal"], path=["Möbel", "Regale"]
    )
    await _products(sessionmaker, ["Sale item"], path=["Sale"], prefix="x")  # too few
    await _embed_all(sessionmaker)
    fake = FakeClient()
    llm = LlmService(sessionmaker, lambda: fake)
    assert await proposals.schedule(sessionmaker, llm, force=True) == {
        "mapping_proposals_queued": 1
    }
    (bid,) = fake.batches
    (req,) = fake.batches[bid]["requests"]
    assert req["params"]["model"] == "claude-sonnet-5-5"
    text = req["params"]["messages"][0]["content"]
    assert text.startswith("Shop: shop\nShop category: Möbel > Regale\n\nExample products:\n- ")
    async with sessionmaker() as s:
        row = (await s.execute(select(LlmBatch))).scalar_one()
    meta = row.requests["0"]["meta"]
    choice = meta["candidates"].index(str(shelf)) + 1
    # products under a path being mapped wait for the mapping
    assert await by_llm.schedule(sessionmaker, llm) == {"llm_classify_queued": 1}  # "Sale"
    assert await proposals.schedule(sessionmaker, llm, force=True) == {
        "mapping_proposals_queued": 0
    }  # in flight
    answer = {"choice": choice, "confidence": "medium", "reason": "Shelves, all of them."}
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [_answer("0", json.dumps(answer))]
    await llm.poll_batches()
    async with sessionmaker() as s:
        await shopmap.classify(s)
        await s.commit()
    cur = await _current(sessionmaker, regal[0])
    assert cur.tier == "shop_mapping" and cur.taxonomy_node_id == shelf and cur.confidence == 0.7
    r = await admin.get("/api/admin/category-paths", params={"method": "llm"})
    (p,) = r.json()
    assert p["path"] == "Möbel > Regale" and p["note"] == "Shelves, all of them."
    assert p["created_by"].startswith("llm-mapping-1/claude-sonnet-5-5/")
    # a person confirms it: from now on it is a manual mapping, never proposed again
    r = await admin.put(
        "/api/admin/category-mappings",
        json={"source": "shop", "path": "Möbel > Regale", "node_id": str(shelf)},
    )
    assert r.status_code == 200
    await _set(sessionmaker, "classification_generation", 5)
    assert await proposals.schedule(sessionmaker, llm, force=True) == {
        "mapping_proposals_queued": 0
    }


async def test_a_failed_proposal_is_recorded_once(sessionmaker):
    await _taxonomy(sessionmaker)
    await _products(sessionmaker, ["Regal A", "Regal B", "Regal C"], path=["Regale"])
    await _embed_all(sessionmaker)
    fake = FakeClient()
    llm = LlmService(sessionmaker, lambda: fake)
    await proposals.schedule(sessionmaker, llm, force=True)
    (bid,) = fake.batches
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [_answer("0", "not json")]
    await llm.poll_batches()
    async with sessionmaker() as s:
        (m,) = (await s.execute(select(CategoryMapping))).scalars().all()
    assert not m.is_current and m.note.startswith("invalid")
    assert await proposals.schedule(sessionmaker, llm, force=True) == {
        "mapping_proposals_queued": 0
    }


# --- panel API and rebuild --------------------------------------------------------------


async def test_classification_endpoints(sessionmaker, admin, monkeypatch):
    await _labelled_world(sessionmaker, monkeypatch)
    r = await admin.get("/api/admin/classification")
    body = r.json()
    assert body["products"] == 20 and body["unclassified"] == 0 and body["embedded"] == 20
    (row,) = body["current"]
    assert row["tier"] == "llm" and row["products"] == 20 and row["at_least_0_8"] == 20
    r = await admin.get("/api/admin/classification/knn-eval")
    assert r.json()["evaluated"] == 20
    r = await admin.post("/api/admin/classification/rerun")
    assert r.json() == {"generation": 1}
    r = await admin.get("/api/admin/category-paths", params={"method": "nonsense"})
    assert r.status_code == 422


async def test_the_rebuild_compares_only_what_it_rebuilds(sessionmaker):
    from sqlalchemy import delete

    await _taxonomy(sessionmaker)
    shelf = await _node(sessionmaker, "Furniture > Shelving")
    aircraft = await _node(sessionmaker, "Vehicles & Parts > Vehicles > Aircraft")
    a, b = await _products(sessionmaker, ["Regal", "Jet"], path=["Möbel", "Regale"])
    async with sessionmaker() as s:
        await shopmap.set_mapping(s, "shop", "Möbel > Regale", shelf)
        await shopmap.classify(s)
        # the LLM's pick (say, from the labelling sample) replaces the mapping's for one
        await common.assign(s, b, aircraft, "llm", 0.9, "v")
        await s.commit()
        before = await stage.snapshot(s)
        # what a rebuild does with the classifications: drop them, map again
        await s.execute(delete(Classification))
        await shopmap.classify(s)
        await s.commit()
        after = await stage.snapshot(s)
    assert before["mapped_products"] == after["mapped_products"] == 2
    assert stage._comparable(before) == stage._comparable(after)
    assert (await _current(sessionmaker, b)).tier == "shop_mapping"  # until the LLM's is back


async def test_one_current_classification_per_product(sessionmaker):
    from sqlalchemy.exc import IntegrityError

    await _taxonomy(sessionmaker)
    shelf = await _node(sessionmaker, "Furniture > Shelving")
    (a,) = await _products(sessionmaker, ["Regal"])
    async with sessionmaker() as s:
        s.add(
            Classification(product_id=a, taxonomy_node_id=shelf, tier="knn", classifier_version="x")
        )
        s.add(
            Classification(product_id=a, taxonomy_node_id=shelf, tier="llm", classifier_version="y")
        )
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_schedulers_run_on_one_coordinator_at_a_time(sessionmaker):
    await _taxonomy(sessionmaker)
    await _products(sessionmaker, ["Regal Eiche"])
    await _embed_all(sessionmaker)
    fake = FakeClient()
    llm = LlmService(sessionmaker, lambda: fake)
    async with common.exclusive(sessionmaker, by_llm.STAGE) as mine:
        assert mine
        assert await by_llm.schedule(sessionmaker, llm) == {"llm_classify_queued": 0}
    assert await by_llm.schedule(sessionmaker, llm) == {"llm_classify_queued": 1}


async def test_the_labelling_sample_checks_the_cheaper_tiers(sessionmaker, admin):
    await _taxonomy(sessionmaker)
    shelf = await _node(sessionmaker, "Furniture > Shelving")
    (a,) = await _products(sessionmaker, ["Regal Eiche"], path=["Möbel", "Regale"])
    await _embed_all(sessionmaker)
    async with sessionmaker() as s:
        await shopmap.set_mapping(s, "shop", "Möbel > Regale", shelf)  # confidence 1.0
        await shopmap.classify(s)
        await s.commit()
    fake = FakeClient()
    llm = LlmService(sessionmaker, lambda: fake)
    await _set(sessionmaker, "classify_llm_sample_fraction", 0)
    assert await by_llm.schedule(sessionmaker, llm) == {"llm_classify_queued": 0}
    await _set(sessionmaker, "classify_llm_sample_fraction", 1)
    assert await by_llm.schedule(sessionmaker, llm) == {"llm_classify_queued": 1}
    (bid,) = fake.batches
    async with sessionmaker() as s:
        row = (await s.execute(select(LlmBatch))).scalar_one()
    choice = row.requests["0"]["meta"]["candidates"].index(str(shelf)) + 1
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [_answer("0", f'{{"choice": {choice}, "confidence": "high"}}')]
    await llm.poll_batches()
    assert (await _current(sessionmaker, a)).tier == "llm"
    body = (await admin.get("/api/admin/classification")).json()
    assert body["agreement"] == [{"tier": "shop_mapping", "compared": 1, "agreed": 1}]


async def test_a_weak_shop_mapping_goes_to_the_llm_while_knn_is_active(sessionmaker, monkeypatch):
    shelf, _ = await _labelled_world(sessionmaker, monkeypatch)
    (a,) = await _products(sessionmaker, ["Wandboard"], path=["Deko"], prefix="w")
    await _embed_all(sessionmaker)
    async with sessionmaker() as s:
        await shopmap.set_mapping(s, "shop", "Deko", shelf, "llm", confidence=0.5)
        await shopmap.classify(s)
        await s.commit()
    await _set(sessionmaker, "classify_llm_sample_fraction", 0)
    fake = FakeClient()
    llm = LlmService(sessionmaker, lambda: fake)
    assert await by_llm.schedule(sessionmaker, llm) == {"llm_classify_queued": 1}
    async with sessionmaker() as s:
        row = (await s.execute(select(LlmBatch))).scalar_one()
    assert row.requests["0"]["meta"]["product_id"] == str(a)


async def test_a_knn_guess_that_turns_unsure_is_withdrawn(sessionmaker, monkeypatch):
    await _labelled_world(sessionmaker, monkeypatch)
    (sure,) = await _products(sessionmaker, ["Regal Eiche Buche"], prefix="q")
    await _embed_all(sessionmaker)
    async with sessionmaker() as s:
        await knn.classify(s)
        await s.commit()
    assert (await _current(sessionmaker, sure)).tier == "knn"
    await _set(sessionmaker, "classify_knn_min_confidence", 0.95)
    await _set(sessionmaker, "classification_generation", 1)
    async with sessionmaker() as s:
        assert (await knn.classify(s))["knn_unsure"] == 1
        await s.commit()
    assert await _current(sessionmaker, sure) is None
    async with sessionmaker() as s:
        assert (await s.get(Product, sure)).category_id is None


async def test_a_proposal_never_overwrites_a_mapping_made_meanwhile(sessionmaker):
    import asyncio

    from pos_coordinator.llm.service import Outcome

    await _taxonomy(sessionmaker)
    shelf = await _node(sessionmaker, "Furniture > Shelving")
    aircraft = await _node(sessionmaker, "Vehicles & Parts > Vehicles > Aircraft")
    outcome = Outcome(
        "ok",
        proposals.Proposal(choice=1, confidence="high", reason="r"),
        "claude-sonnet-5-5",
        proposals.STAGE,
        meta={
            "source": "shop",
            "path": "Möbel > Regale",
            "candidates": [str(aircraft)],
            "version": "v",
        },  # fmt: skip
    )
    async with sessionmaker() as person:  # a person maps the path; not committed yet
        await shopmap.set_mapping(person, "shop", "Möbel > Regale", shelf, "manual")

        async def deliver():
            async with sessionmaker() as s:
                await proposals.handle(s, outcome)
                await s.commit()

        task = asyncio.create_task(deliver())
        await asyncio.sleep(0.3)
        assert not task.done()  # waits for the person's transaction
        await person.commit()
    await task
    async with sessionmaker() as s:
        (m,) = (
            (await s.execute(select(CategoryMapping).where(CategoryMapping.is_current)))
            .scalars()
            .all()
        )
    assert m.method == "manual" and m.taxonomy_node_id == shelf


async def test_a_shop_mapping_change_never_undoes_a_classifier_meanwhile(sessionmaker):
    import asyncio

    await _taxonomy(sessionmaker)
    shelf = await _node(sessionmaker, "Furniture > Shelving")
    aircraft = await _node(sessionmaker, "Vehicles & Parts > Vehicles > Aircraft")
    (a,) = await _products(sessionmaker, ["Regal"], path=["Möbel", "Regale"])
    async with sessionmaker() as s:
        await shopmap.set_mapping(s, "shop", "Möbel > Regale", shelf)
        await shopmap.classify(s)
        await shopmap.set_mapping(s, "shop", "Möbel > Regale", None)  # now unmappable
        await s.commit()
    async with sessionmaker() as llm_side:  # an LLM answer lands, not committed yet
        await common.assign(llm_side, a, aircraft, "llm", 0.9, "v")

        async def mapping_pass():
            async with sessionmaker() as s:
                r = await shopmap.classify(s)
                await s.commit()
                return r

        task = asyncio.create_task(mapping_pass())
        await asyncio.sleep(0.3)
        await llm_side.commit()
    assert (await task)["withdrawn"] == 0
    cur = await _current(sessionmaker, a)
    assert cur.tier == "llm"
    async with sessionmaker() as s:
        assert (await s.get(Product, a)).category_id == aircraft


async def test_llm_work_waits_for_every_category_vector(sessionmaker):
    from sqlalchemy import delete

    from pos_coordinator.models import TaxonomyEmbedding

    await _taxonomy(sessionmaker)
    await _products(sessionmaker, ["Regal Eiche", "Regal Buche", "Wandregal"],
                    path=["Möbel", "Regale"])  # fmt: skip
    await _embed_all(sessionmaker)
    async with sessionmaker() as s:  # one category's vector is still being computed
        node = (await s.execute(select(TaxonomyEmbedding.node_id).limit(1))).scalar_one()
        await s.execute(delete(TaxonomyEmbedding).where(TaxonomyEmbedding.node_id == node))
        await s.commit()
    fake = FakeClient()
    llm = LlmService(sessionmaker, lambda: fake)
    assert await by_llm.schedule(sessionmaker, llm) == {"llm_classify_queued": 0}
    assert await proposals.schedule(sessionmaker, llm, force=True) == {
        "mapping_proposals_queued": 0
    }
    await _embed_all(sessionmaker)
    common._nodes = None
    assert await by_llm.schedule(sessionmaker, llm) == {"llm_classify_queued": 3}


async def test_none_fits_withdraws_an_earlier_llm_label(sessionmaker):
    await _taxonomy(sessionmaker)
    shelf = await _node(sessionmaker, "Furniture > Shelving")
    a, b = await _products(sessionmaker, ["Regal", "Jet"])
    async with sessionmaker() as s:
        await common.assign(s, a, shelf, "llm", 0.9, "old-version")
        await common.assign(s, b, shelf, "manual", 1.0, "person")
        await s.commit()

    def none_fits(pid):
        return SimpleNamespace(
            status="ok",
            data=by_llm.Pick(choice=0, confidence="high"),
            meta={"product_id": str(pid), "candidates": [str(shelf)], "version": "new-version"},
        )

    async with sessionmaker() as s:
        await by_llm.handle(s, none_fits(a))
        await by_llm.handle(s, none_fits(b))
        await s.commit()
        assert (await s.get(Product, a)).category_id is None
    assert await _current(sessionmaker, a) is None
    assert (await _current(sessionmaker, b)).tier == "manual"  # a person's stays


async def test_paths_that_cannot_be_sampled_do_not_block_proposals(sessionmaker, monkeypatch):
    monkeypatch.setattr(proposals, "PER_ROUND", 1)
    await _taxonomy(sessionmaker)
    async with sessionmaker() as s:  # the biggest path: products without a title
        for i in range(10):
            s.add(Product(match_key=f"url:https://shop.example/x{i}", source="shop",
                          attributes={"category_path": ["Sale"]}))  # fmt: skip
        await s.commit()
    await _products(sessionmaker, ["Regal A", "Regal B", "Regal C"], path=["Regale"])
    await _embed_all(sessionmaker)
    fake = FakeClient()
    llm = LlmService(sessionmaker, lambda: fake)
    assert await proposals.schedule(sessionmaker, llm, force=True) == {
        "mapping_proposals_queued": 1
    }
    async with sessionmaker() as s:
        row = (await s.execute(select(LlmBatch))).scalar_one()
    assert row.requests["0"]["meta"]["path"] == "Regale"
