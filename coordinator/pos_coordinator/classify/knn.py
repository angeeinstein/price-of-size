"""Tier 2 of classification: a k-nearest-neighbour classifier over embeddings.

It learns from products that already carry a trusted label (manual, LLM, shop
mapping; never its own guesses). The examples are a stratified sample: every manual
and LLM label, and at most PER_GROUP products per (shop, category) from shop
mappings, so one big shop cannot drown the others. A product's neighbours vote with
their similarity; the confidence is the winning category's share of the vote.

A prediction at or above `classify_knn_min_confidence` becomes the product's
classification (unless a higher tier holds it); below, it is kept as an attempt
and the product is left to the LLM tier. Everything is computed in memory with
numpy (exact, deterministic), not with an approximate index.

The version names the method, k, the embedding model, the generation (bumped in the
panel to run everything again) and the size class of the example set: when the
labelled examples have grown by half, the version changes and the k-NN guesses are
made again with what has been learned since.
"""

import asyncio
import logging
import math
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import numpy as np
from pgvector.sqlalchemy import Vector
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator import runtime_settings
from pos_coordinator.classify import common
from pos_shared import embedding

log = logging.getLogger(__name__)

TIER = "knn"
METHOD = "knn-1"
K = 15
PER_GROUP = 20  # shop-mapping examples per (shop, category)
MAX_EXAMPLES = 100_000
MIN_EXAMPLES = 50
EPOCH_GROWTH = 1.5
EXAMPLES_TTL_S = 900.0  # the example set is reloaded at most this often
PER_ROUND = 2_000
CHUNK = 256  # queries per matrix product (memory: CHUNK x examples floats)

_EXAMPLES_SQL = """
    WITH labelled AS (
      SELECT c.product_id, c.taxonomy_node_id AS node_id, c.tier, p.source,
             md5(coalesce(p.match_key, p.id::text)) AS h
      FROM classification c
      JOIN product p ON p.id = c.product_id
      JOIN embedding e ON e.product_id = c.product_id AND e.model = :mk
      WHERE c.is_current AND c.taxonomy_node_id IS NOT NULL AND c.tier = ANY(CAST(:tiers AS text[]))
    ), ranked AS (
      SELECT *, row_number() OVER (PARTITION BY source, node_id ORDER BY h) AS rn
      FROM labelled
    )
    SELECT product_id, node_id FROM ranked
    WHERE tier <> 'shop_mapping' OR rn <= :per_group
    ORDER BY (tier = 'shop_mapping'), h
    LIMIT :max_examples
"""


@dataclass
class Examples:
    product_ids: list[uuid.UUID]
    node_ids: list[uuid.UUID]
    vectors: np.ndarray  # (n, DIM) float32
    loaded_at: float
    generation: int | None = None  # the generation they were loaded for

    @property
    def epoch(self) -> int:
        n = max(len(self.node_ids), 1)
        return int(math.log(n) / math.log(EPOCH_GROWTH))


_examples: Examples | None = None
_lock = asyncio.Lock()


async def examples(
    session: AsyncSession, refresh: bool = False, generation: int | None = None
) -> Examples:
    """The examples, reloaded after EXAMPLES_TTL_S, on request, and whenever the
    generation changes (a "run again" must see the labels as they are now)."""
    global _examples
    async with _lock:
        if (
            not refresh
            and _examples is not None
            and time.monotonic() - _examples.loaded_at < EXAMPLES_TTL_S
            and (generation is None or _examples.generation == generation)
        ):
            return _examples
        rows = (
            await session.execute(
                text(_EXAMPLES_SQL),
                {
                    "mk": embedding.MODEL_KEY,
                    "tiers": list(common.LABEL_TIERS),
                    "per_group": PER_GROUP,
                    "max_examples": MAX_EXAMPLES,
                },
            )
        ).all()
        vecs: dict[uuid.UUID, Any] = {}
        ids = [r[0] for r in rows]
        for i in range(0, len(ids), 5_000):
            part = ids[i : i + 5_000]
            for pid, vec in (
                await session.execute(
                    text(
                        "SELECT product_id, vector FROM embedding "
                        "WHERE model = :mk AND product_id = ANY(CAST(:ids AS uuid[]))"
                    ).columns(vector=Vector()),
                    {"mk": embedding.MODEL_KEY, "ids": part},
                )  # fmt: skip
            ).all():
                vecs[pid] = vec
        kept = [(r[0], r[1]) for r in rows if r[0] in vecs]
        _examples = Examples(
            product_ids=[k[0] for k in kept],
            node_ids=[k[1] for k in kept],
            vectors=common.stack([vecs[k[0]] for k in kept]),
            loaded_at=time.monotonic(),
            generation=generation,
        )
        return _examples


def version(ex: Examples, gen: int) -> str:
    return f"{METHOD}/k{K}/{embedding.MODEL_KEY}/g{gen}/e{ex.epoch}"


def vote(
    ex: Examples, queries: np.ndarray, exclude: list[uuid.UUID | None] | None = None
) -> list[tuple[uuid.UUID, float] | None]:
    """Per query: the winning node and its share of the similarity-weighted vote of
    the K nearest examples (an example equal to `exclude[i]` does not vote)."""
    out: list[tuple[uuid.UUID, float] | None] = []
    if len(ex.node_ids) == 0:
        return [None] * len(queries)
    index = {pid: i for i, pid in enumerate(ex.product_ids)} if exclude else {}
    for start in range(0, len(queries), CHUNK):
        sims = np.asarray(queries[start : start + CHUNK], dtype=np.float32) @ ex.vectors.T
        for j, row in enumerate(sims):
            skip = index.get(exclude[start + j]) if exclude else None
            if skip is not None:
                row[skip] = -np.inf
            k = min(K, len(row) - (1 if skip is not None else 0))
            if k <= 0:
                out.append(None)
                continue
            top = np.argpartition(-row, k - 1)[:k]
            weights: dict[uuid.UUID, float] = defaultdict(float)
            for i in top:
                weights[ex.node_ids[i]] += max(float(row[i]), 0.0)
            total = sum(weights.values())
            if total <= 0:
                out.append(None)
                continue
            # the highest share wins; ties go to the smaller node id (deterministic)
            node, w = min(weights.items(), key=lambda kv: (-round(kv[1], 6), str(kv[0])))
            out.append((node, w / total))
    return out


_TODO_SQL = """
    SELECT e.product_id, e.vector
    FROM embedding e
    LEFT JOIN classification c ON c.product_id = e.product_id AND c.is_current
    WHERE e.model = :mk
      AND (c.id IS NULL OR c.tier = 'knn')
      AND NOT EXISTS (
        SELECT 1 FROM classification a
        WHERE a.product_id = e.product_id AND a.tier = 'knn' AND a.classifier_version = :v)
    ORDER BY (c.id IS NOT NULL), e.product_id
    LIMIT :limit
"""


async def classify(session: AsyncSession, limit: int = PER_ROUND) -> dict[str, int]:
    """Classify up to `limit` products that have no classification yet (or a k-NN
    one from another version)."""
    out = {"knn_classified": 0, "knn_unsure": 0}
    if not await common.try_lock(session, "knn"):
        return out  # another coordinator is at it
    cfg = await runtime_settings.get_all(session)
    gen = int(cfg["classification_generation"])
    ex = await examples(session, generation=gen)
    if len(ex.node_ids) < MIN_EXAMPLES:
        return out
    v = version(ex, gen)
    threshold = float(cfg["classify_knn_min_confidence"])
    rows = (
        await session.execute(
            text(_TODO_SQL).columns(vector=Vector()),
            {"mk": embedding.MODEL_KEY, "v": v, "limit": limit},
        )
    ).all()
    if not rows:
        return out
    queries = common.stack([r[1] for r in rows])
    votes = await asyncio.to_thread(vote, ex, queries)
    for (pid, _), result in zip(rows, votes, strict=True):
        node, share = result if result is not None else (None, 0.0)
        conf = round(share, 4) if result is not None else None
        if node is not None and share >= threshold:
            await common.assign(session, pid, node, TIER, conf, v)
            out["knn_classified"] += 1
        else:
            # unsure now: an earlier, surer k-NN guess no longer stands (the product
            # goes to the LLM tier); another tier's classification stays
            await common.withdraw(session, pid, TIER)
            await common.attempt(session, pid, node, TIER, conf, v)
            out["knn_unsure"] += 1
    return out


async def evaluate(session: AsyncSession, sample: int = 2_000) -> dict[str, Any]:
    """Leave-one-out check on the examples that carry an LLM or manual label (the
    labels the classifier imitates): accuracy and coverage per confidence threshold,
    to choose `classify_knn_min_confidence`."""
    ex = await examples(session, refresh=True)
    trusted = (
        await session.execute(
            text(
                "SELECT c.product_id, c.tier FROM classification c "
                "WHERE c.is_current AND c.tier IN ('manual', 'llm') "
                "AND c.product_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": ex.product_ids},
        )
    ).all()
    tiers = {r[0]: r[1] for r in trusted}
    idx = [i for i, pid in enumerate(ex.product_ids) if pid in tiers]
    # an even spread over the trusted examples, the same every time
    if len(idx) > sample:
        step = len(idx) / sample
        idx = [idx[int(j * step)] for j in range(sample)]
    if not idx:
        return {"examples": len(ex.node_ids), "evaluated": 0, "thresholds": []}
    queries = ex.vectors[idx]
    votes = await asyncio.to_thread(vote, ex, queries, [ex.product_ids[i] for i in idx])
    results = [
        (v[1], v[0] == ex.node_ids[i]) for i, v in zip(idx, votes, strict=True) if v is not None
    ]
    thresholds = []
    for t in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95):
        kept = [ok for share, ok in results if share >= t]
        thresholds.append(
            {
                "threshold": t,
                "coverage": round(len(kept) / len(idx), 4),
                "accuracy": round(sum(kept) / len(kept), 4) if kept else None,
            }
        )
    return {"examples": len(ex.node_ids), "evaluated": len(idx), "thresholds": thresholds}
