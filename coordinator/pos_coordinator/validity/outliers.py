"""Category outliers: a product's price per kilogram and per cubic metre against its
category (docs/spec.md, "Detection pipeline", step 3).

Prices per unit spread over orders of magnitude, so the comparison is on the log
scale with a robust z-score: (x - median) / (1.4826 * MAD), or with the mean
absolute deviation where more than half the values are equal (MAD 0). A category with fewer
than MIN_GROUP comparable products borrows its parent's distribution (the closest
ancestor with enough). A product beyond `category_outlier_z` on either measure gets
the flag `category_outlier` on the offers behind its price; the flag does not drop
the price by itself, it sends the offers to the LLM review.

The flags are derived state: each run sets them afresh over all products.
"""

import asyncio
import logging
import math
import statistics
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator import runtime_settings
from pos_coordinator.models import Offer, TaxonomyNode

log = logging.getLogger(__name__)

VERSION = "outlier-1"
FLAG = "category_outlier"
MIN_GROUP = 20
MAD_SCALE = 1.4826  # MAD of a normal distribution -> its standard deviation
MEAN_AD_SCALE = 1.2533  # mean absolute deviation -> standard deviation (normal)
INTERVAL_S = 3600.0

# Mass and volume each from the best clean measurement that has it (the product's own
# first): a weight from the spec table and dimensions from JSON-LD both count.
_ROWS_SQL = """
    SELECT p.id, p.category_id, p.canonical_price_eur, mass.mass_kg, vol.volume_m3
    FROM product p
    LEFT JOIN LATERAL (
      SELECT mass_kg FROM measurement
      WHERE product_id = p.id AND cardinality(flags) = 0 AND mass_kg > 0
      ORDER BY (basis = 'product') DESC, id
      LIMIT 1
    ) mass ON true
    LEFT JOIN LATERAL (
      SELECT coalesce(true_volume_m3, a_m * b_m * c_m) AS volume_m3 FROM measurement
      WHERE product_id = p.id AND cardinality(flags) = 0
        AND coalesce(true_volume_m3, a_m * b_m * c_m) > 0
      ORDER BY (basis = 'product') DESC, id
      LIMIT 1
    ) vol ON true
    WHERE p.category_id IS NOT NULL AND p.canonical_price_eur > 0
      AND (mass.mass_kg IS NOT NULL OR vol.volume_m3 IS NOT NULL)
"""


@dataclass
class Finding:
    product_id: uuid.UUID
    z_kg: float | None
    z_m3: float | None
    group_kg: uuid.UUID | None
    group_m3: uuid.UUID | None


def scale(values: list[float], med: float) -> float:
    """The robust spread behind the z-score: 1.4826 * MAD; when more than half of the
    values are equal (MAD 0), 1.2533 * the mean absolute deviation instead, as the
    modified z-score does, so a group of repeated prices still shows its outliers."""
    mad = statistics.median(abs(v - med) for v in values)
    if mad > 0:
        return MAD_SCALE * mad
    return MEAN_AD_SCALE * statistics.fmean(abs(v - med) for v in values)


def find(
    rows: list[tuple[uuid.UUID, uuid.UUID, float, float | None, float | None]],
    parents: dict[uuid.UUID, uuid.UUID | None],
    z_limit: float,
    min_group: int = MIN_GROUP,
) -> dict[uuid.UUID, Finding]:
    """rows: (product, category, price EUR, kg, m³). Returns the outliers."""

    def ancestors(node: uuid.UUID) -> list[uuid.UUID]:
        out, seen = [], set()
        while node is not None and node not in seen:
            seen.add(node)
            out.append(node)
            node = parents.get(node)
        return out

    chains = {n: ancestors(n) for n in {r[1] for r in rows}}
    values: dict[str, dict[uuid.UUID, float]] = {"kg": {}, "m3": {}}
    for pid, _node, price, kg, m3 in rows:
        if kg and kg > 0:
            values["kg"][pid] = math.log(price / kg)
        if m3 and m3 > 0:
            values["m3"][pid] = math.log(price / m3)
    node_of = {r[0]: r[1] for r in rows}
    z: dict[str, dict[uuid.UUID, tuple[float, uuid.UUID]]] = {"kg": {}, "m3": {}}
    for metric, vals in values.items():
        groups: dict[uuid.UUID, list[float]] = defaultdict(list)
        for pid, v in vals.items():
            for n in chains[node_of[pid]]:
                groups[n].append(v)
        stats: dict[uuid.UUID, tuple[float, float]] = {}
        for n, vs in groups.items():
            if len(vs) >= min_group:
                med = statistics.median(vs)
                spread = scale(vs, med)
                if spread > 0:
                    stats[n] = (med, spread)
        for pid, v in vals.items():
            for n in chains[node_of[pid]]:
                if n in stats:
                    med, spread = stats[n]
                    z[metric][pid] = ((v - med) / spread, n)
                    break
    out = {}
    for pid in set(z["kg"]) | set(z["m3"]):
        zk, zm = z["kg"].get(pid), z["m3"].get(pid)
        if (zk and abs(zk[0]) > z_limit) or (zm and abs(zm[0]) > z_limit):
            out[pid] = Finding(
                pid,
                round(zk[0], 2) if zk else None,
                round(zm[0], 2) if zm else None,
                zk[1] if zk else None,
                zm[1] if zm else None,
            )
    return out


async def run(session: AsyncSession) -> dict[str, int]:
    """Set the category-outlier flags afresh over all products."""
    from pos_coordinator.derive.stage import NOT_A_VALID_PRICE

    z_limit = float(await runtime_settings.get(session, "category_outlier_z"))
    rows = [(r[0], r[1], float(r[2]), r[3], r[4]) for r in (await session.execute(text(_ROWS_SQL)))]
    parents = dict((await session.execute(select(TaxonomyNode.id, TaxonomyNode.parent_id))).all())
    findings = await asyncio.to_thread(find, rows, parents, z_limit)
    # the offers behind each outlier's price get the flag; every other offer loses it
    want: dict[uuid.UUID, Finding] = {}
    if findings:
        offers = (
            await session.execute(
                select(Offer.id, Offer.product_id, Offer.price_eur, Offer.flags).where(
                    Offer.product_id.in_(list(findings))
                )
            )
        ).all()
        for oid, pid, price, flags in offers:
            if price is not None and not (set(flags or []) & NOT_A_VALID_PRICE):
                want[oid] = findings[pid]
    have = set(
        (await session.execute(select(Offer.id).where(Offer.flags.contains([FLAG])))).scalars()
    )
    out = {"category_outliers": len(findings), "flagged": 0, "cleared": 0}
    for oid in have - set(want):
        o = await session.get(Offer, oid)
        o.flags = sorted(set(o.flags or []) - {FLAG})
        o.fields = {k: v for k, v in (o.fields or {}).items() if k != "category_check"}
        out["cleared"] += 1
    for oid, f in want.items():
        check = {
            "version": VERSION,
            "z_kg": f.z_kg,
            "z_m3": f.z_m3,
            "group_kg": str(f.group_kg) if f.group_kg else None,
            "group_m3": str(f.group_m3) if f.group_m3 else None,
        }
        o = await session.get(Offer, oid)
        if FLAG in (o.flags or []) and (o.fields or {}).get("category_check") == check:
            continue
        o.flags = sorted(set(o.flags or []) | {FLAG})
        o.fields = {**(o.fields or {}), "category_check": check}
        out["flagged"] += oid not in have
    await session.flush()
    return out


_last_run = 0.0


async def maybe_run(session: AsyncSession, force: bool = False) -> dict[str, Any]:
    """At most every INTERVAL_S (the maintenance loop asks every round)."""
    global _last_run
    if not force and time.monotonic() - _last_run < INTERVAL_S:
        return {}
    _last_run = time.monotonic()
    return await run(session)
