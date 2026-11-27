"""Admin API for per-site parsers: versions, their checks, activation, test runs."""

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from pos_coordinator.api.deps import CurrentViewer, SensitiveAdmin, Session
from pos_coordinator.derive import llm_tier, site_parsers
from pos_coordinator.derive.stage import _load
from pos_coordinator.models import LlmExtraction, Parser, RawFetch
from pos_shared.extract import site_parser

router = APIRouter(prefix="/api/admin")


def _row(p: Parser) -> dict[str, Any]:
    check = (p.artifact or {}).get("_check") or {}
    return {
        "id": p.id,
        "domain": p.domain,
        "version": p.version,
        "kind": p.kind,
        "active": p.active,
        "validation_score": p.validation_score,
        "held_out": check.get("held_out"),
        "train": check.get("train"),
        # checked pages with a unit, monthly, net or "from" price: not activated on its own
        "qualified_prices": check.get("qualified_prices") or 0,
        "error": (p.artifact or {}).get("error"),
        "created_by": p.created_by,
        "created_at": p.created_at,
    }


@router.get("/parsers")
async def list_parsers(admin: CurrentViewer, session: Session, domain: str | None = None):
    q = select(Parser).order_by(Parser.domain, Parser.version.desc())
    if domain:
        q = q.where(Parser.domain == domain.lower())
    return [_row(p) for p in (await session.execute(q.limit(1000))).scalars()]


async def _get(session, parser_id: uuid.UUID) -> Parser:
    p = await session.get(Parser, parser_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such parser")
    return p


@router.get("/parsers/{parser_id}")
async def get_parser(parser_id: uuid.UUID, admin: CurrentViewer, session: Session):
    p = await _get(session, parser_id)
    return {**_row(p), "artifact": p.artifact}


@router.post("/parsers/{parser_id}/activate")
async def activate(parser_id: uuid.UUID, admin: SensitiveAdmin, session: Session):
    p = await _get(session, parser_id)
    try:
        await site_parsers.set_active(session, p, True, admin.name)
    except site_parser.SpecError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    await session.commit()
    return _row(p)


@router.post("/parsers/{parser_id}/deactivate")
async def deactivate(parser_id: uuid.UUID, admin: SensitiveAdmin, session: Session):
    p = await _get(session, parser_id)
    await site_parsers.set_active(session, p, False, admin.name)
    await session.commit()
    return _row(p)


@router.get("/parsers/{parser_id}/test")
async def test_run(parser_id: uuid.UUID, admin: CurrentViewer, session: Session, limit: int = 10):
    """Run a parser version on the domain's latest archived pages: what it finds,
    beside what the LLM tier found there (when it read the page)."""
    p = await _get(session, parser_id)
    try:
        spec = site_parser.parse(p.artifact)
    except site_parser.SpecError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    store = llm_tier._store
    if store is None or not store.available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "raw store unavailable")
    rows = (
        await session.execute(
            select(RawFetch, LlmExtraction.output)
            .outerjoin(
                LlmExtraction,
                (LlmExtraction.fetch_id == RawFetch.id)
                & (LlmExtraction.fetched_at == RawFetch.fetched_at)
                & (LlmExtraction.prompt_version == llm_tier.PROMPT)
                & (LlmExtraction.status == "ok"),
            )
            .where(RawFetch.html_sha256.is_not(None), RawFetch.mode.in_(("http", "browser")))
            .where(RawFetch.url.ilike(f"%://{p.domain}/%") | RawFetch.url.ilike(f"%.{p.domain}/%"))
            .order_by(RawFetch.fetched_at.desc())
            .limit(min(max(limit, 1), 50))
        )
    ).all()
    out = []
    for f, answer in rows:
        if site_parsers.domain_of(f.final_url or f.url) != p.domain:
            continue
        page = await asyncio.to_thread(site_parsers._html, store, f.html_sha256)
        if page is None:
            continue
        evidence = await asyncio.to_thread(_load, store, f.blob_sha256) if f.blob_sha256 else None
        snip = (evidence or {}).get("snippets") or {}
        locale = (snip.get("lang") or "")[:2].lower() or None
        r = await asyncio.to_thread(site_parser.run, spec, page)
        out.append(
            {
                "url": f.final_url or f.url,
                "fetched_at": f.fetched_at,
                "found": {
                    "title": r.title,
                    "price": r.price_text,
                    "currency": site_parser.currency_of(r, spec),
                    "category_path": r.category_path,
                    "sizes": [{"label": a, "value": b} for a, b in r.pairs],
                },
                "llm": answer,
                "passes": (
                    site_parsers.page_passes(spec, page, answer, locale)
                    if isinstance(answer, dict) and answer.get("is_product_page")
                    else None
                ),
            }
        )
    return out
