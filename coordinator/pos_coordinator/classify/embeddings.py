"""Embeddings of products and taxonomy nodes, computed by workers (`embed` jobs).

The coordinator decides what needs a vector (no vector under the current model key,
or its text changed), queues jobs of up to 64 texts, and stores what comes back
after checking it (worker output is untrusted: same ids in the same order, exact
size, finite unit vectors). Taxonomy nodes go first: without them there are no
candidates for classification.

`candidates()` gives the nearest taxonomy nodes of a product (cosine similarity),
the shortlist the LLM classifier picks from; `neighbours()` the nearest products
that are already classified, for the k-NN classifier.
"""

import hashlib
import logging
import uuid
from typing import Any

from sqlalchemy import func, select, true
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator import queue
from pos_coordinator.models import (
    Classification,
    Embedding,
    Job,
    Measurement,
    Product,
    TaxonomyEmbedding,
    TaxonomyNode,
)
from pos_shared import embedding

log = logging.getLogger(__name__)
JOB_TYPE = "embed"
MAX_PENDING_JOBS = 40  # queued or leased at once
PRIORITY = -10  # after fetches: embeddings wait, pages do not


def _hash(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()[:32]


async def _pending_ids(session: AsyncSession) -> tuple[int, set[str]]:
    rows = (
        await session.execute(
            select(Job.payload).where(Job.type == JOB_TYPE, Job.status.in_(("queued", "leased")))
        )
    ).scalars()
    jobs, ids = 0, set()
    for p in rows:
        jobs += 1
        ids |= {str(it.get("id")) for it in (p or {}).get("items") or [] if isinstance(it, dict)}
    return jobs, ids


async def _node_items(session: AsyncSession, limit: int) -> list[dict[str, str]]:
    rows = (
        await session.execute(
            select(
                TaxonomyNode.id, TaxonomyNode.path, TaxonomyNode.names, TaxonomyEmbedding.text_hash
            )
            .outerjoin(
                TaxonomyEmbedding,
                (TaxonomyEmbedding.node_id == TaxonomyNode.id)
                & (TaxonomyEmbedding.model == embedding.MODEL_KEY),
            )
            .order_by(TaxonomyNode.depth, TaxonomyNode.path)
        )
    ).all()
    out = []
    for node_id, path, names, have in rows:
        t = embedding.node_text(path, ((names or {}).get("de") or {}).get("path"))
        if have != _hash(t):
            out.append({"id": str(node_id), "text": t, "h": _hash(t)})
            if len(out) >= limit:
                break
    return out


# Products that have a vector are re-checked for a changed text a slice at a time,
# going round the whole catalogue (from the start again after a restart).
RECHECK_SLICE = 5_000
_recheck_after: uuid.UUID | None = None


async def _product_items(session: AsyncSession, limit: int) -> list[dict[str, str]]:
    """Products without a vector first; then a slice of those with one, whose text
    may have changed since."""
    global _recheck_after
    out = await _product_slice(session, limit, missing=True)
    if len(out) < limit:
        start = _recheck_after
        found, last, full = await _product_slice(
            session, limit - len(out), missing=False, after=start, with_cursor=True
        )
        out += found
        # a short slice reached the end of the catalogue: round again from the start
        _recheck_after = last if full else None
    return out


async def _product_slice(
    session: AsyncSession,
    limit: int,
    missing: bool,
    after: uuid.UUID | None = None,
    with_cursor: bool = False,
) -> Any:
    # the product's own measurement (else any): sizes help tell a shelf from a bookcase
    m = (
        select(Measurement.a_m, Measurement.b_m, Measurement.c_m, Measurement.mass_kg)
        .where(Measurement.product_id == Product.id, func.cardinality(Measurement.flags) == 0)
        .order_by((Measurement.basis == "product").desc(), Measurement.id)
        .limit(1)
        .lateral()
    )
    q = (
        select(
            Product.id,
            Product.title,
            Product.brand,
            Product.attributes,
            Embedding.text_hash,
            m.c.a_m,
            m.c.b_m,
            m.c.c_m,
            m.c.mass_kg,
        )
        .outerjoin(
            Embedding,
            (Embedding.product_id == Product.id) & (Embedding.model == embedding.MODEL_KEY),
        )
        .outerjoin(m, true())
        .where(Product.title.is_not(None))
    )
    if missing:
        q = q.where(Embedding.text_hash.is_(None)).order_by(Product.created_at, Product.id)
        q = q.limit(limit * 4)
    else:
        q = q.where(Embedding.text_hash.is_not(None)).order_by(Product.id).limit(RECHECK_SLICE)
        if after is not None:
            q = q.where(Product.id > after)
    rows = (await session.execute(q)).all()
    out = []
    last, filled = None, False
    for pid, title, brand, attrs, have, a, b, c, mass in rows:
        last = pid
        path = (attrs or {}).get("category_path") if isinstance(attrs, dict) else None
        t = embedding.product_text(
            title, path if isinstance(path, list) else None, (a, b, c), mass, brand
        )
        if have != _hash(t):
            out.append({"id": str(pid), "text": t, "h": _hash(t)})
            if len(out) >= limit:
                filled = True
                break
    if with_cursor:
        return out, last, filled or len(rows) == RECHECK_SLICE
    return out


async def enqueue(session: AsyncSession) -> int:
    """Queue embed jobs for what has no (current) vector; at most MAX_PENDING_JOBS
    in flight. Returns the number of jobs queued."""
    jobs, pending = await _pending_ids(session)
    room = MAX_PENDING_JOBS - jobs
    if room <= 0:
        return 0
    per_job = embedding.MAX_ITEMS_PER_JOB
    new: list[queue.NewJob] = []
    for kind, fetch in (("node", _node_items), ("product", _product_items)):
        if len(new) >= room:
            break
        items = [
            it
            for it in await fetch(session, (room - len(new)) * per_job + len(pending))
            if it["id"] not in pending
        ]
        for i in range(0, len(items), per_job):
            if len(new) >= room:
                break
            new.append(
                queue.NewJob(
                    type=JOB_TYPE,
                    payload={
                        "model": embedding.MODEL_KEY,
                        "kind": kind,
                        "items": items[i : i + per_job],
                    },  # fmt: skip
                    priority=PRIORITY,
                )
            )
    await queue.enqueue(session, new)
    return len(new)


async def ingest(session: AsyncSession, job: Job, output: dict[str, Any] | None) -> int:
    """Store the vectors of a finished embed job. Raises ValueError on output that
    does not match the job (the result is then logged and dropped by the caller)."""
    payload = job.payload or {}
    items = payload.get("items") or []
    if not output or output.get("model") != payload.get("model"):
        raise ValueError("embed result for another model")
    ids = [str(it.get("id")) for it in items]
    if output.get("ids") != ids:
        raise ValueError("embed result ids do not match the job")
    vectors = embedding.decode(str(output.get("vectors") or ""), len(ids))
    model = payload["model"]
    if payload.get("kind") == "node":
        table, key = TaxonomyEmbedding, "node_id"
        known = set(
            (
                await session.execute(select(TaxonomyNode.id).where(TaxonomyNode.id.in_(ids)))
            ).scalars()
        )
    else:
        table, key = Embedding, "product_id"
        known = set(
            (await session.execute(select(Product.id).where(Product.id.in_(ids)))).scalars()
        )
    n = 0
    for it, vec in zip(items, vectors, strict=True):
        ref = uuid.UUID(str(it["id"]))
        if ref not in known:
            continue  # deleted meanwhile (a rebuild): nothing to attach it to
        stmt = pg_insert(table).values(
            **{key: ref}, model=model, vector=vec, text_hash=str(it.get("h"))
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[key, "model"],
            set_={
                "vector": stmt.excluded.vector,
                "text_hash": stmt.excluded.text_hash,
                "created_at": func.now(),
            },  # fmt: skip
        )
        await session.execute(stmt)
        n += 1
    return n


async def candidates(
    session: AsyncSession, product_id: uuid.UUID, k: int = 20
) -> list[tuple[uuid.UUID, str, float]]:
    """The k taxonomy nodes nearest to the product: (node id, path, similarity)."""
    vec = (
        await session.execute(
            select(Embedding.vector).where(
                Embedding.product_id == product_id, Embedding.model == embedding.MODEL_KEY
            )
        )
    ).scalar_one_or_none()
    if vec is None:
        return []
    dist = TaxonomyEmbedding.vector.cosine_distance(vec)
    rows = (
        await session.execute(
            select(TaxonomyNode.id, TaxonomyNode.path, dist)
            .join(TaxonomyEmbedding, TaxonomyEmbedding.node_id == TaxonomyNode.id)
            .where(TaxonomyEmbedding.model == embedding.MODEL_KEY)
            .order_by(dist)
            .limit(k)
        )
    ).all()
    return [(r[0], r[1], 1.0 - float(r[2])) for r in rows]


async def neighbours(
    session: AsyncSession, product_id: uuid.UUID, k: int = 15
) -> list[tuple[uuid.UUID, uuid.UUID, str, float]]:
    """The k nearest products that have a current classification:
    (product id, its node id, its tier, similarity)."""
    vec = (
        await session.execute(
            select(Embedding.vector).where(
                Embedding.product_id == product_id, Embedding.model == embedding.MODEL_KEY
            )
        )
    ).scalar_one_or_none()
    if vec is None:
        return []
    dist = Embedding.vector.cosine_distance(vec)
    rows = (
        await session.execute(
            select(Embedding.product_id, Classification.taxonomy_node_id, Classification.tier, dist)
            .join(
                Classification,
                (Classification.product_id == Embedding.product_id) & Classification.is_current,
            )
            .where(
                Embedding.model == embedding.MODEL_KEY,
                Embedding.product_id != product_id,
                Classification.taxonomy_node_id.is_not(None),
            )
            .order_by(dist)
            .limit(k)
        )
    ).all()
    return [(r[0], r[1], r[2], 1.0 - float(r[3])) for r in rows]
