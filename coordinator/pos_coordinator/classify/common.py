"""What the classification tiers share: who may overwrite whom, how a result is
stored, the facts about a product the classifiers see, and the taxonomy nodes as a
matrix for nearest-category search.

A product has at most one current classification. Tiers rank by trust:

    manual > llm > shop_mapping > knn

A tier never replaces the current classification of a higher one. A result that
does not become current (a k-NN guess below its threshold, an LLM answer "none",
an invalid answer) is kept as a non-current row: the attempt is recorded, so the
same classifier version does not ask again, and history is never deleted.
"""

import asyncio
import contextlib
import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pos_coordinator import runtime_settings
from pos_coordinator.models import (
    Classification,
    Measurement,
    Product,
    TaxonomyEmbedding,
    TaxonomyNode,
)
from pos_shared import embedding

RANK = {"knn": 1, "shop_mapping": 2, "llm": 3, "manual": 4}
# tiers whose labels teach the k-NN classifier (never its own guesses)
LABEL_TIERS = ("manual", "llm", "shop_mapping")
# an LLM's stated confidence as a number
CONFIDENCE = {"high": 0.9, "medium": 0.7, "low": 0.5}


def rank(tier: str | None) -> int:
    return RANK.get(tier or "", 0)


async def assign(
    session: AsyncSession,
    product_id: uuid.UUID,
    node_id: uuid.UUID,
    tier: str,
    confidence: float | None,
    version: str,
) -> bool:
    """Make the node the product's current classification, unless a higher tier
    holds it. Returns whether anything changed."""
    # the product row serialises classifiers writing the same product
    await session.execute(select(Product.id).where(Product.id == product_id).with_for_update())
    cur = (
        await session.execute(
            select(Classification)
            .where(Classification.product_id == product_id, Classification.is_current)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cur is not None:
        if rank(cur.tier) > rank(tier):
            await attempt(session, product_id, node_id, tier, confidence, version)
            return False
        if (cur.tier, cur.taxonomy_node_id, cur.classifier_version, cur.confidence) == (
            tier,
            node_id,
            version,
            confidence,
        ):
            return False
        cur.is_current = False
    session.add(
        Classification(
            product_id=product_id,
            taxonomy_node_id=node_id,
            tier=tier,
            confidence=confidence,
            classifier_version=version,
        )
    )
    await session.execute(
        update(Product).where(Product.id == product_id).values(category_id=node_id)
    )
    await session.flush()
    return True


async def withdraw(session: AsyncSession, product_id: uuid.UUID, tier: str) -> bool:
    """End the product's current classification if `tier` made it (its classifier now
    says otherwise); one from another tier stays. Returns whether one was ended."""
    await session.execute(select(Product.id).where(Product.id == product_id).with_for_update())
    cur = (
        await session.execute(
            select(Classification)
            .where(Classification.product_id == product_id, Classification.is_current)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cur is None or cur.tier != tier:
        return False
    cur.is_current = False
    await session.execute(update(Product).where(Product.id == product_id).values(category_id=None))
    await session.flush()
    return True


async def attempt(
    session: AsyncSession,
    product_id: uuid.UUID,
    node_id: uuid.UUID | None,
    tier: str,
    confidence: float | None,
    version: str,
) -> None:
    """Record a result that does not become the current classification."""
    session.add(
        Classification(
            product_id=product_id,
            taxonomy_node_id=node_id,
            tier=tier,
            confidence=confidence,
            classifier_version=version,
            is_current=False,
        )
    )
    await session.flush()


async def taxonomy_version(session: AsyncSession) -> str:
    """The taxonomy as the classifiers see it: a new import changes this, and every
    classifier version that includes it runs again."""
    rows = (
        await session.execute(
            select(TaxonomyNode.version, func.count())
            .group_by(TaxonomyNode.version)
            .order_by(TaxonomyNode.version)
        )
    ).all()
    if not rows:
        return "none"
    # e.g. "2021-09-21+pos-1.5640": Google's release, the extension's, and the size
    return "+".join(str(r[0]) for r in rows) + f".{sum(r[1] for r in rows)}"


async def generation(session: AsyncSession) -> int:
    """Bumped in the panel to run every classifier again over the stored data."""
    return int(await runtime_settings.get(session, "classification_generation"))


def lock_key(name: str) -> int:
    return int.from_bytes(hashlib.sha256(f"pos:{name}".encode()).digest()[:8], "big", signed=True)


async def try_lock(session: AsyncSession, name: str) -> bool:
    """Take the named lock for this transaction if no other coordinator holds it."""
    return bool(
        (await session.execute(select(func.pg_try_advisory_xact_lock(lock_key(name))))).scalar()
    )


@contextlib.asynccontextmanager
async def exclusive(sm: async_sessionmaker[AsyncSession], name: str) -> AsyncIterator[bool]:
    """One coordinator at a time runs the block (others get False and skip it)."""
    async with sm() as s:
        got = await try_lock(s, name)
        try:
            yield got
        finally:
            await s.rollback()


# --- what a classifier sees of a product ---------------------------------------------


@dataclass
class ProductFacts:
    id: uuid.UUID
    source: str | None
    title: str
    brand: str | None
    category_path: list[str]
    dims_m: tuple[float, ...]
    mass_kg: float | None


def _clip(text: Any, n: int) -> str:
    return " ".join(str(text or "").split())[:n]


async def facts(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, ProductFacts]:
    """Title, brand, shop category and sizes (the product's own clean measurement
    first). No price: it changes with every visit, and a classification prompt must
    stay the same for the same product, so a rebuild is answered from the ledger."""
    if not ids:
        return {}
    m = (
        select(
            Measurement.product_id,
            Measurement.a_m,
            Measurement.b_m,
            Measurement.c_m,
            Measurement.mass_kg,
            func.row_number()
            .over(
                partition_by=Measurement.product_id,
                order_by=[(Measurement.basis == "product").desc(), Measurement.id],
            )
            .label("rn"),
        )
        .where(Measurement.product_id.in_(ids), func.cardinality(Measurement.flags) == 0)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                Product.id,
                Product.source,
                Product.title,
                Product.brand,
                Product.attributes,
                m.c.a_m,
                m.c.b_m,
                m.c.c_m,
                m.c.mass_kg,
            )
            .outerjoin(m, (m.c.product_id == Product.id) & (m.c.rn == 1))
            .where(Product.id.in_(ids))
        )
    ).all()
    out = {}
    for pid, source, title, brand, attrs, a, b, c, mass in rows:
        path = (attrs or {}).get("category_path") if isinstance(attrs, dict) else None
        out[pid] = ProductFacts(
            id=pid,
            source=source,
            title=_clip(title, 300),
            brand=_clip(brand, 100) or None,
            category_path=[_clip(s, 100) for s in path if _clip(s, 100)][:8]
            if isinstance(path, list)
            else [],
            dims_m=tuple(d for d in (a, b, c) if d),
            mass_kg=mass,
        )
    return out


def _num(x: float) -> str:
    return f"{x:.3g}" if x < 1000 else f"{x:.0f}"


def describe(f: ProductFacts) -> str:
    lines = [f"Product: {f.title}"]
    if f.brand:
        lines.append(f"Brand: {f.brand}")
    if f.source:
        lines.append(f"Shop: {f.source}")
    if f.category_path:
        lines.append("Shop category: " + " > ".join(f.category_path))
    if f.dims_m:
        lines.append("Size: " + " x ".join(_num(d * 100) for d in f.dims_m) + " cm")
    if f.mass_kg:
        lines.append(f"Weight: {_num(f.mass_kg)} kg")
    return "\n".join(lines)


def numbered(paths: list[str]) -> str:
    return "\n".join(f"{i}. {p}" for i, p in enumerate(paths, 1))


# --- the taxonomy as a matrix --------------------------------------------------------


@dataclass
class NodeMatrix:
    ids: list[uuid.UUID]
    paths: list[str]
    vectors: np.ndarray  # (n, DIM) float32, unit rows

    def nearest(self, queries: np.ndarray, k: int) -> list[list[int]]:
        """Row indexes of the k nearest nodes per query, nearest first; ties broken
        by path, so the same vectors always give the same list (and prompt)."""
        if not self.ids:
            return [[] for _ in range(len(queries))]
        sims = np.asarray(queries, dtype=np.float32) @ self.vectors.T
        k = min(k, len(self.ids))
        out = []
        for row in sims:
            top = np.argpartition(-row, k - 1)[:k] if k < len(row) else np.arange(len(row))
            out.append(
                sorted(top.tolist(), key=lambda i: (-round(float(row[i]), 6), self.paths[i]))
            )
        return out


_nodes: tuple[Any, NodeMatrix] | None = None
_nodes_lock = asyncio.Lock()


async def node_matrix(session: AsyncSession) -> NodeMatrix:
    """All taxonomy node vectors under the current model key (a few thousand: they
    fit in memory), reloaded when they change."""
    global _nodes
    key = (
        await session.execute(
            select(func.count(), func.max(TaxonomyEmbedding.created_at)).where(
                TaxonomyEmbedding.model == embedding.MODEL_KEY
            )
        )
    ).one()
    key = (key[0], key[1].isoformat() if isinstance(key[1], datetime) else None)
    async with _nodes_lock:
        if _nodes is not None and _nodes[0] == key:
            return _nodes[1]
        rows = (
            await session.execute(
                select(TaxonomyNode.id, TaxonomyNode.path, TaxonomyEmbedding.vector)
                .join(TaxonomyEmbedding, TaxonomyEmbedding.node_id == TaxonomyNode.id)
                .where(TaxonomyEmbedding.model == embedding.MODEL_KEY)
                .order_by(TaxonomyNode.path)
            )
        ).all()
        vectors = (
            np.vstack([np.asarray(r[2], dtype=np.float32) for r in rows])
            if rows
            else np.zeros((0, embedding.DIM), dtype=np.float32)
        )
        matrix = NodeMatrix([r[0] for r in rows], [r[1] for r in rows], vectors)
        _nodes = (key, matrix)
        return matrix


def stack(vectors: list[Any]) -> np.ndarray:
    """Vectors from the database as one float32 matrix (rows stay unit length: they
    were checked when stored)."""
    if not vectors:
        return np.zeros((0, embedding.DIM), dtype=np.float32)
    return np.vstack([np.asarray(v, dtype=np.float32) for v in vectors])


async def taxonomy_ready(session: AsyncSession) -> bool:
    """Every taxonomy node has its current vector. Shortlists from a partial matrix
    would stay wrong: an answer is never asked again under the same version."""
    from pos_coordinator.classify import embeddings

    has_nodes = (await session.execute(select(func.count()).select_from(TaxonomyNode))).scalar()
    return bool(has_nodes) and not await embeddings._node_items(session, 1)


def unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v
