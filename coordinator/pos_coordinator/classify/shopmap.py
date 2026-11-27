"""Tier 1 of classification: a shop's own categories, mapped once per shop.

Every product page carries its shop's breadcrumbs (`attributes.category_path`). A
mapping turns one (source, path) into a taxonomy node; all products under that
path follow it. Mappings are made by hand in the panel or API (later also proposed
by the LLM, spot-checked by a person) and are curated data: a rebuild of the
derived layer keeps them and classifies again.

A product has at most one current classification (see common.py for the order of
the tiers). A shop mapping replaces a k-NN guess but never a manual or LLM
classification; a changed mapping re-classifies the products it had classified,
and an "unmappable" path withdraws them.
"""

import re
import uuid
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator.classify import common
from pos_coordinator.models import CategoryMapping, TaxonomyNode

TIER = "shop_mapping"
VERSION = "shopmap-1"
SEP = " > "
MAX_PATH_CHARS = 1000
METHODS = ("manual", "llm", "rule", "import")
LOWER_TIERS = ["knn"]  # tiers a shop mapping replaces (common.RANK)

# Whitespace collapsed in a path: the same characters in Python and SQL (Python's
# own notion of whitespace is wider than PostgreSQL's).
_WS = " \t\n\v\f\r "
_WS_RE = re.compile(f"[{re.escape(_WS)}]+")
_WS_SQL = "'[' || chr(32) || chr(9) || chr(10) || chr(11) || chr(12) || chr(13) || chr(160) || ']+'"

# The product's category path as one string, normalised exactly like path_string():
# segments joined with " > ", split again at every ">", whitespace collapsed, empty
# parts dropped, at most MAX_PATH_CHARS.
PATH_SQL = f"""left(array_to_string(ARRAY(
    SELECT btrim(regexp_replace(part, {_WS_SQL}, ' ', 'g'))
    FROM regexp_split_to_table(array_to_string(ARRAY(
        SELECT jsonb_array_elements_text(CASE WHEN jsonb_typeof(p.attributes->'category_path')
            = 'array' THEN p.attributes->'category_path' ELSE '[]'::jsonb END)), ' > '), '>')
        WITH ORDINALITY AS t(part, n)
    WHERE btrim(regexp_replace(part, {_WS_SQL}, ' ', 'g')) <> ''
    ORDER BY n), ' > '), {MAX_PATH_CHARS})"""


def path_string(segments: list[Any] | str) -> str:
    """'Home >  Office  Chairs' and ["Home ", "Office  Chairs"] -> 'Home > Office Chairs'.
    Must stay in step with PATH_SQL: mappings and products meet by exact equality."""
    text = segments if isinstance(segments, str) else SEP.join(str(s) for s in segments)
    parts = [_WS_RE.sub(" ", p).strip(" ") for p in text.split(">")]
    return SEP.join(p for p in parts if p)[:MAX_PATH_CHARS]


async def lock_path(session: AsyncSession, source: str, path: str) -> None:
    """Serialise changes to one (source, path) until the transaction ends: a person's
    mapping and an LLM proposal arriving at the same time never overwrite each other
    unseen."""
    await session.execute(
        select(func.pg_advisory_xact_lock(common.lock_key(f"mapping\x1f{source}\x1f{path}")))
    )


async def set_mapping(
    session: AsyncSession,
    source: str,
    path: str,
    node_id: uuid.UUID | None,
    method: str = "manual",
    confidence: float | None = None,
    actor: str | None = None,
    note: str | None = None,
) -> CategoryMapping:
    """Map (source, path) to a node (None: no fitting category). The previous mapping
    stays as history."""
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}")
    path = path_string(path)
    if not source or not path:
        raise ValueError("source and path are required")
    if node_id is not None and await session.get(TaxonomyNode, node_id) is None:
        raise ValueError("unknown taxonomy node")
    await lock_path(session, source, path)
    await session.execute(
        update(CategoryMapping)
        .where(
            CategoryMapping.source == source,
            CategoryMapping.category_path == path,
            CategoryMapping.is_current,
        )
        .values(is_current=False)
    )
    m = CategoryMapping(
        source=source,
        category_path=path,
        taxonomy_node_id=node_id,
        method=method,
        confidence=confidence if confidence is not None else (1.0 if method == "manual" else None),
        created_by=actor,
        note=note,
    )
    session.add(m)
    await session.flush()
    return m


async def category_paths(
    session: AsyncSession,
    source: str | None = None,
    unmapped_only: bool = False,
    q: str | None = None,
    method: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Category paths seen per source, with product counts and their current mapping,
    most frequent first (mapping the big ones first classifies the most products).
    `method` keeps paths whose current mapping was made that way ("llm": the
    proposals still to spot-check)."""
    sql = f"""
        WITH paths AS (
          SELECT p.source, {PATH_SQL} AS path, count(*) AS products
          FROM product p
          WHERE p.source IS NOT NULL AND (CAST(:source AS text) IS NULL OR p.source = :source)
          GROUP BY 1, 2
        )
        SELECT paths.source, paths.path, paths.products, m.id AS mapping_id,
               m.taxonomy_node_id, m.method, m.confidence, m.note, m.created_by,
               n.path AS node_path, n.external_id AS node_external_id
        FROM paths
        LEFT JOIN category_mapping m
          ON m.is_current AND m.source = paths.source AND m.category_path = paths.path
        LEFT JOIN taxonomy_node n ON n.id = m.taxonomy_node_id
        WHERE paths.path <> ''
          AND (NOT :unmapped OR m.id IS NULL)
          AND (CAST(:method AS text) IS NULL OR m.method = :method)
          AND (CAST(:q AS text) IS NULL OR paths.path ILIKE '%' || :q || '%')
        ORDER BY paths.products DESC, paths.source, paths.path
        LIMIT :limit OFFSET :offset
    """
    rows = await session.execute(
        text(sql),
        {
            "source": source,
            "unmapped": unmapped_only,
            "q": q,
            "method": method,
            "limit": min(max(limit, 1), 1000),
            "offset": max(offset, 0),
        },
    )
    return [dict(r._mapping) for r in rows]


async def classify(session: AsyncSession, limit: int = 5000) -> dict[str, int]:
    """Bring shop-mapping classifications in line with the current mappings (node and
    confidence: a confirmed LLM mapping is more certain) and with the products' current
    paths; at most `limit` products per call (the maintenance loop calls again)."""
    rows = (
        await session.execute(
            text(f"""
                WITH prod AS (
                  SELECT p.id, p.source, {PATH_SQL} AS path FROM product p
                  WHERE p.source IS NOT NULL
                )
                SELECT prod.id AS product_id, m.taxonomy_node_id AS node_id,
                       m.confidence, c.id AS cls_id
                FROM prod
                JOIN category_mapping m
                  ON m.is_current AND m.source = prod.source AND m.category_path = prod.path
                LEFT JOIN classification c ON c.product_id = prod.id AND c.is_current
                WHERE prod.path <> ''
                  AND (
                    ((c.id IS NULL OR c.tier = ANY(CAST(:lower AS text[])))
                     AND m.taxonomy_node_id IS NOT NULL)
                    OR (c.tier = :tier
                        AND (c.taxonomy_node_id IS DISTINCT FROM m.taxonomy_node_id
                             OR c.confidence IS DISTINCT FROM coalesce(m.confidence, 0.9)))
                  )
                ORDER BY prod.id
                LIMIT :limit
                FOR UPDATE OF m SKIP LOCKED
            """),
            {"tier": TIER, "lower": LOWER_TIERS, "limit": limit},
        )
    ).all()
    # a classification by a path the product no longer has (its breadcrumbs came from
    # an earlier fetch since, e.g. through an LLM answer) and that has no mapping: the
    # query above never meets it again, and a rebuild would not assign it
    orphans = (
        await session.execute(
            text(f"""
                SELECT p.id AS product_id
                FROM classification c
                JOIN product p ON p.id = c.product_id
                WHERE c.is_current AND c.tier = :tier
                  AND NOT EXISTS (
                    SELECT 1 FROM category_mapping m
                    WHERE m.is_current AND m.source = p.source AND m.category_path = {PATH_SQL})
                ORDER BY p.id
                LIMIT :limit
            """),
            {"tier": TIER, "limit": max(limit - len(rows), 0)},
        )
    ).all()
    out = {"classified": 0, "withdrawn": 0}
    for r in orphans:
        out["withdrawn"] += await common.withdraw(session, r.product_id, TIER)
    for r in rows:
        # under the product's lock, against its classification as it is now (another
        # classifier may have written it since the query): a more trusted tier stays
        if r.node_id is None:
            out["withdrawn"] += await common.withdraw(session, r.product_id, TIER)
            continue
        confidence = r.confidence if r.confidence is not None else 0.9
        out["classified"] += await common.assign(
            session, r.product_id, r.node_id, TIER, confidence, VERSION
        )
    await session.flush()
    return out


async def search_nodes(
    session: AsyncSession, q: str, locale: str = "en", limit: int = 30
) -> list[dict[str, Any]]:
    """Taxonomy nodes whose path (English or `locale`) contains all words of `q`."""
    words = [w for w in q.split() if w][:6]
    stmt = select(TaxonomyNode).order_by(TaxonomyNode.depth, TaxonomyNode.path).limit(limit)
    for w in words:
        like = f"%{w}%"
        stmt = stmt.where(
            TaxonomyNode.path.ilike(like)
            | func.coalesce(TaxonomyNode.names[locale]["path"].astext, "").ilike(like)
        )
    return [
        {
            "id": n.id,
            "external_id": n.external_id,
            "path": n.path,
            "local_path": (n.names or {}).get(locale, {}).get("path"),
            "is_custom": n.is_custom,
        }
        for n in (await session.execute(stmt)).scalars()
    ]
