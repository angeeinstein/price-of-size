"""Admin API for the target taxonomy and the shop category mappings."""

import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from pos_coordinator.api.deps import CurrentAdmin, CurrentViewer, Session
from pos_coordinator.classify import shopmap, taxonomy
from pos_coordinator.models import AuditLog, CategoryMapping, TaxonomyNode

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
    limit: int = 200,
    offset: int = 0,
):
    return await shopmap.category_paths(session, source, unmapped, q, limit, offset)


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
        }
        for m, node_path in rows
    ]
