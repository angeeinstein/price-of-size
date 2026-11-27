"""Admin API for the target taxonomy and the shop category mappings."""

import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text

from pos_coordinator import runtime_settings
from pos_coordinator.api.deps import CurrentAdmin, CurrentViewer, Session
from pos_coordinator.classify import knn, shopmap, taxonomy
from pos_coordinator.models import (
    AuditLog,
    CategoryMapping,
    Classification,
    Embedding,
    LlmBatch,
    Product,
    TaxonomyNode,
)
from pos_shared import embedding

router = APIRouter(prefix="/api/admin")


@router.get("/taxonomy")
async def taxonomy_status(admin: CurrentViewer, session: Session):
    rows = (
        await session.execute(
            select(TaxonomyNode.version, TaxonomyNode.is_custom, func.count())
            .group_by(TaxonomyNode.version, TaxonomyNode.is_custom)
            .order_by(TaxonomyNode.version)
        )
    ).all()
    return {
        "nodes": sum(r[2] for r in rows),
        "versions": [{"version": r[0], "custom": r[1], "nodes": r[2]} for r in rows],
    }


@router.get("/taxonomy/search")
async def taxonomy_search(
    admin: CurrentViewer, session: Session, q: str, locale: str = "en", limit: int = 30
):
    if not q.strip():
        return []
    return await shopmap.search_nodes(session, q[:200], locale, min(max(limit, 1), 100))


class TaxonomyImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # locale -> file content; empty: download Google's files (en, de)
    texts: dict[str, str] = Field(default_factory=dict, max_length=5)
    custom: bool = True


@router.post("/taxonomy/import")
async def taxonomy_import(body: TaxonomyImport, admin: CurrentAdmin, session: Session):
    texts = body.texts
    if not texts:
        try:
            texts = await taxonomy.download()
        except httpx.HTTPError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"download failed: {e}") from e
    if any(len(t) > 5_000_000 for t in texts.values()):
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "taxonomy file too large")
    try:
        result = await taxonomy.import_google(session, texts, custom=body.custom)
    except taxonomy.TaxonomyError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    session.add(
        AuditLog(actor=admin.name, action="taxonomy.import", target="google", details=result)
    )
    await session.commit()
    return result


@router.get("/category-paths")
async def category_paths(
    admin: CurrentViewer,
    session: Session,
    source: str | None = None,
    unmapped: bool = False,
    q: str | None = None,
    method: str | None = None,
    limit: int = 200,
    offset: int = 0,
):
    """`method=llm`: the LLM's mapping proposals, to spot-check."""
    if method is not None and method not in shopmap.METHODS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown method")
    return await shopmap.category_paths(
        session, source, unmapped, q, limit=limit, offset=offset, method=method
    )


class MappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=shopmap.MAX_PATH_CHARS)
    # one of: the node's id, or its external id ("632", "pos-101"); neither: unmappable
    node_id: uuid.UUID | None = None
    external_id: str | None = Field(None, max_length=50)


@router.put("/category-mappings")
async def put_mapping(body: MappingIn, admin: CurrentAdmin, session: Session) -> dict[str, Any]:
    node_id = body.node_id
    if node_id is None and body.external_id:
        node_id = (
            await session.execute(
                select(TaxonomyNode.id).where(
                    TaxonomyNode.taxonomy == taxonomy.TAXONOMY,
                    TaxonomyNode.external_id == body.external_id,
                )
            )
        ).scalar_one_or_none()
        if node_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown external id")
    try:
        m = await shopmap.set_mapping(
            session, body.source, body.path, node_id, "manual", actor=admin.name
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    session.add(
        AuditLog(
            actor=admin.name,
            action="category_mapping.put",
            target=f"{m.source}: {m.category_path}",
            details={"node_id": str(node_id) if node_id else None},
        )
    )
    await session.commit()
    return {
        "id": m.id,
        "source": m.source,
        "path": m.category_path,
        "node_id": m.taxonomy_node_id,
        "method": m.method,
    }


@router.get("/category-mappings/history")
async def mapping_history(admin: CurrentViewer, session: Session, source: str, path: str):
    rows = (
        await session.execute(
            select(CategoryMapping, TaxonomyNode.path)
            .outerjoin(TaxonomyNode, TaxonomyNode.id == CategoryMapping.taxonomy_node_id)
            .where(
                CategoryMapping.source == source,
                CategoryMapping.category_path == shopmap.path_string(path),
            )
            .order_by(CategoryMapping.created_at.desc())
        )
    ).all()
    return [
        {
            "node_path": node_path,
            "method": m.method,
            "confidence": m.confidence,
            "created_by": m.created_by,
            "created_at": m.created_at,
            "is_current": m.is_current,
            "note": m.note,
        }
        for m, node_path in rows
    ]


# --- classification -------------------------------------------------------------------


@router.get("/classification")
async def classification_status(admin: CurrentViewer, session: Session) -> dict[str, Any]:
    """How the products are classified: per tier and version (current rows), the
    attempts that did not become current, confidence spread, and what is waiting."""
    current = (
        await session.execute(
            select(
                Classification.tier,
                Classification.classifier_version,
                func.count(),
                func.avg(Classification.confidence),
                func.count().filter(Classification.confidence < 0.6),
                func.count().filter(Classification.confidence >= 0.8),
            )
            .where(Classification.is_current)
            .group_by(Classification.tier, Classification.classifier_version)
            .order_by(Classification.tier, Classification.classifier_version)
        )
    ).all()
    attempts = (
        await session.execute(
            select(
                Classification.tier,
                func.count(),
                func.count().filter(Classification.taxonomy_node_id.is_(None)),
            )
            .where(~Classification.is_current)
            .group_by(Classification.tier)
            .order_by(Classification.tier)
        )
    ).all()
    products, unclassified = (
        await session.execute(
            select(func.count(), func.count().filter(Product.category_id.is_(None)))
        )
    ).one()
    embedded = (
        await session.execute(select(func.count()).where(Embedding.model == embedding.MODEL_KEY))
    ).scalar_one()
    batches = (
        await session.execute(
            select(LlmBatch.stage, func.count(), func.sum(LlmBatch.request_count))
            .where(LlmBatch.status.in_(("submitting", "submitted")))
            .group_by(LlmBatch.stage)
        )
    ).all()
    # the LLM's labels against what the cheaper tiers said before for the same product
    # (mostly the labelling sample): how often they agree
    agreement = (
        await session.execute(
            text("""
                SELECT o.tier, count(*),
                       count(*) FILTER (WHERE o.taxonomy_node_id = c.taxonomy_node_id)
                FROM classification c
                JOIN LATERAL (
                  SELECT DISTINCT ON (x.tier) x.tier, x.taxonomy_node_id
                  FROM classification x
                  WHERE x.product_id = c.product_id AND NOT x.is_current
                    AND x.tier IN ('shop_mapping', 'knn') AND x.taxonomy_node_id IS NOT NULL
                  ORDER BY x.tier, x.created_at DESC
                ) o ON true
                WHERE c.is_current AND c.tier = 'llm'
                GROUP BY o.tier ORDER BY o.tier
            """)
        )
    ).all()
    cfg = await runtime_settings.get_all(session)
    return {
        "agreement": [{"tier": r[0], "compared": r[1], "agreed": r[2]} for r in agreement],
        "products": products,
        "unclassified": unclassified,
        "embedded": embedded,
        "generation": cfg["classification_generation"],
        "current": [
            {
                "tier": r[0],
                "version": r[1],
                "products": r[2],
                "avg_confidence": round(float(r[3]), 3) if r[3] is not None else None,
                "below_0_6": r[4],
                "at_least_0_8": r[5],
            }
            for r in current
        ],
        "attempts": [{"tier": r[0], "rows": r[1], "no_category": r[2]} for r in attempts],
        "batches": [{"stage": r[0], "batches": r[1], "requests": r[2]} for r in batches],
    }


@router.get("/classification/knn-eval")
async def knn_eval(admin: CurrentViewer, session: Session, sample: int = 2000):
    """Leave-one-out accuracy of the k-NN classifier against LLM and manual labels,
    per confidence threshold (to choose classify_knn_min_confidence)."""
    return await knn.evaluate(session, min(max(sample, 10), 20_000))


@router.post("/classification/rerun")
async def classification_rerun(admin: CurrentAdmin, session: Session):
    """Run every classifier again over the stored data (a new generation). LLM
    answers to unchanged questions come from the ledger; new ones cost budget."""
    gen = int(await runtime_settings.get(session, "classification_generation")) + 1
    await runtime_settings.put(session, "classification_generation", gen, admin.name)
    session.add(
        AuditLog(
            actor=admin.name,
            action="classification.rerun",
            target="classification",
            details={"generation": gen},
        )
    )
    await session.commit()
    return {"generation": gen}
