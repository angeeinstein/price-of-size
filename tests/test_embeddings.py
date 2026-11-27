"""Embeddings: texts, wire format, worker job, coordinator queueing and storage.

The model itself is replaced by a deterministic fake here (CI does not download
470 MB); test_real_model runs against the pinned files when they are present
(POS_TEST_EMBED_MODEL_DIR)."""

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import select

from pos_coordinator.classify import embeddings, taxonomy
from pos_coordinator.models import Embedding, Job, Product, TaxonomyEmbedding, TaxonomyNode
from pos_shared import api, embedding
from pos_worker import embed as worker_embed
from tests.helpers import make_agent
from tests.test_taxonomy import _google, _google_de


class FakeModel:
    """A vector per text from its words: texts sharing words are close."""

    def embed(self, texts):
        out = np.zeros((len(texts), embedding.DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in t.lower().replace(">", " ").replace("|", " ").split():
                h = int(hashlib.md5(w.encode()).hexdigest(), 16)
                out[i, h % embedding.DIM] += 1.0
        out[:, -1] += 0.01
        return out / np.linalg.norm(out, axis=1, keepdims=True)


# --- shared ---------------------------------------------------------------------------


def test_texts_and_wire_format():
    t = embedding.product_text(
        " VITTSJÖ  Regal ", ["Produkte", "Regale"], (1.75, 1.0, 0.36), 27.56, brand="IKEA"
    )
    assert t == "query: IKEA VITTSJÖ Regal | Produkte > Regale | 175 x 100 x 36 cm | 27.6 kg"
    assert embedding.node_text("Furniture > Shelving", "Möbel > Regale") == (
        "query: Furniture > Shelving | Möbel > Regale"
    )
    assert embedding.node_text("Land", "Land") == "query: Land"
    v = FakeModel().embed(["a b", "c"])
    back = embedding.decode(embedding.encode(v), 2)
    assert len(back) == 2 and abs(float(np.dot(back[0], v[0])) - 1) < 1e-3
    for bad, n in (("!!", 1), (embedding.encode(v), 3), (embedding.encode(v * 3), 2)):
        with pytest.raises(ValueError):
            embedding.decode(bad, n)
    nan = v.copy()
    nan[0, 0] = np.nan
    with pytest.raises(ValueError):
        embedding.decode(embedding.encode(nan), 2)


# --- worker -----------------------------------------------------------------------------


def _job(payload, attempt=1):
    return api.LeasedJob(
        id=uuid.uuid4(),
        type="embed",
        payload=payload,
        attempt=attempt,
        lease_expires_at="2030-01-01T00:00:00Z",
        lease_seconds=300,
    )


async def test_the_worker_job(monkeypatch):
    monkeypatch.setattr(worker_embed, "load", lambda: FakeModel())
    items = [{"id": "a", "text": "query: Regal"}, {"id": "b", "text": "query: Laptop"}]
    r = await worker_embed.embed(_job({"model": embedding.MODEL_KEY, "items": items}), None)
    assert r.outcome == api.JobOutcome.done and r.output["ids"] == ["a", "b"]
    assert len(embedding.decode(r.output["vectors"], 2)) == 2
    r = await worker_embed.embed(_job({"model": "other@1/t9", "items": items}), None)
    assert r.outcome == api.JobOutcome.failed and "model mismatch" in r.error
    with pytest.raises(ValueError):
        await worker_embed.embed(_job({"model": embedding.MODEL_KEY, "items": []}), None)


def test_model_files_are_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(embedding, "FILES", {"onnx/x.bin": hashlib.sha256(b"good").hexdigest()})

    class Resp:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def raise_for_status(self):
            pass

        def iter_bytes(self, n):
            yield self.data

    served = [b"evil"]
    monkeypatch.setattr(worker_embed.httpx, "stream", lambda *a, **k: Resp(served[0]))
    with pytest.raises(RuntimeError, match="checksum"):
        worker_embed.ensure_files(tmp_path)
    assert not list(tmp_path.rglob("x.bin*"))  # nothing half-downloaded stays
    served[0] = b"good"
    root = worker_embed.ensure_files(tmp_path)
    assert (root / "onnx/x.bin").read_bytes() == b"good"
    served[0] = b"evil"
    worker_embed.ensure_files(tmp_path)  # verified file present: no download


@pytest.mark.skipif(
    not os.environ.get("POS_TEST_EMBED_MODEL_DIR"), reason="real model files not present"
)
def test_real_model(tmp_path):
    """The pinned model: German products land nearest their category."""
    src = Path(os.environ["POS_TEST_EMBED_MODEL_DIR"])
    root = tmp_path / f"{embedding.REPO.replace('/', '__')}@{embedding.REVISION}" / "onnx"
    root.mkdir(parents=True)
    for name in ("model.onnx", "tokenizer.json"):
        (root / name).symlink_to(src / name)
    model = worker_embed.Model(worker_embed.ensure_files(tmp_path))
    products = model.embed(
        [
            embedding.product_text("VITTSJÖ Regal schwarzbraun/Glas", ["Möbel", "Regale"]),
            embedding.product_text("MacBook Air 13 Zoll Laptop 8 GB"),
            embedding.product_text("Bürostuhl ergonomisch mit Armlehnen"),
        ]
    )
    nodes = model.embed(
        [
            embedding.node_text("Furniture > Shelving", "Möbel > Regale"),
            embedding.node_text(
                "Electronics > Computers > Laptops", "Elektronik > Computer > Laptops"
            ),
            embedding.node_text(
                "Furniture > Office Furniture > Office Chairs", "Möbel > Büromöbel > Bürostühle"
            ),  # fmt: skip
        ]
    )
    assert products.shape == (3, embedding.DIM)
    assert list((products @ nodes.T).argmax(axis=1)) == [0, 1, 2]


# --- coordinator --------------------------------------------------------------------------


async def _world(sm, products=("VITTSJÖ Regal", "MacBook Laptop")):
    async with sm() as s:
        await taxonomy.import_google(s, {"en": _google(), "de": _google_de()})
        for i, title in enumerate(products):
            s.add(Product(match_key=f"url:https://shop.example/{i}", source="shop", title=title,
                          attributes={"category_path": ["Möbel"]}))  # fmt: skip
        await s.commit()


async def test_jobs_are_queued_once_nodes_first(sessionmaker, monkeypatch):
    monkeypatch.setattr(embeddings, "MAX_PENDING_JOBS", 3)
    await _world(sessionmaker)
    async with sessionmaker() as s:
        assert await embeddings.enqueue(s) == 3  # 140 nodes: three jobs (64, 64, 12)
        await s.commit()
        assert await embeddings.enqueue(s) == 0
        jobs = (await s.execute(select(Job).where(Job.type == "embed"))).scalars().all()
        assert {j.payload["kind"] for j in jobs} == {"node"}
        assert all(j.payload["model"] == embedding.MODEL_KEY for j in jobs)
        ids = [it["id"] for j in jobs for it in j.payload["items"]]
        assert len(ids) == len(set(ids)) == 140


async def test_results_are_checked_stored_and_queried(sessionmaker):
    await _world(sessionmaker)
    async with sessionmaker() as s:
        await embeddings.enqueue(s)
        await s.commit()
        jobs = (await s.execute(select(Job).where(Job.type == "embed"))).scalars().all()
        fake = FakeModel()
        for j in jobs:
            items = j.payload["items"]
            vectors = embedding.encode(fake.embed([it["text"] for it in items]))
            out = {"model": j.payload["model"], "ids": [it["id"] for it in items],
                   "vectors": vectors}  # fmt: skip
            bad = {**out, "ids": list(reversed(out["ids"]))}
            if len(items) > 1:
                with pytest.raises(ValueError):
                    await embeddings.ingest(s, j, bad)
            with pytest.raises(ValueError):
                await embeddings.ingest(s, j, {**out, "model": "other"})
            await embeddings.ingest(s, j, out)
            j.status = "done"
        await s.commit()
        nodes = (await s.execute(select(TaxonomyEmbedding))).scalars().all()
        products = (await s.execute(select(Embedding))).scalars().all()
        total = (await s.execute(select(TaxonomyNode))).scalars().all()
        assert len(nodes) == len(total) and len(products) == 2
        shelf = (
            await s.execute(select(Product).where(Product.title == "VITTSJÖ Regal"))
        ).scalar_one()
        # a shared word ("Möbel" in the shop path and the German taxonomy name) ranks first
        cands = await embeddings.candidates(s, shelf.id, k=5)
        assert len(cands) == 5 and cands[0][2] >= cands[-1][2]
        assert cands[0][1].startswith("Furniture")
        # a changed product text is embedded again; unchanged ones are not
        shelf.title = "VITTSJÖ Regal Glas"
        await s.commit()
        await embeddings.enqueue(s)
        await s.commit()
        again = (
            (await s.execute(select(Job).where(Job.type == "embed", Job.status == "queued")))
            .scalars()
            .all()
        )
        assert [it["id"] for j in again for it in j.payload["items"]] == [str(shelf.id)]


async def test_embed_jobs_end_to_end(app, admin, sessionmaker, tmp_path, monkeypatch):
    """Coordinator queues, a real worker runs the job (fake model), the vectors land."""
    monkeypatch.setattr(worker_embed, "load", lambda: FakeModel())
    await _world(sessionmaker)
    async with sessionmaker() as s:
        await embeddings.enqueue(s)
        await s.commit()
    worker = await make_agent(app, admin, tmp_path / "w", "w")
    task = asyncio.create_task(worker.run(drain_timeout_s=5))
    try:
        for _ in range(300):
            async with sessionmaker() as s:
                left = (
                    (await s.execute(select(Job).where(Job.type == "embed", Job.status != "done")))
                    .scalars()
                    .all()
                )
            if not left:
                break
            await asyncio.sleep(0.1)
    finally:
        worker.stop()
        await task
    async with sessionmaker() as s:
        assert (await s.execute(select(TaxonomyEmbedding))).scalars().all()
