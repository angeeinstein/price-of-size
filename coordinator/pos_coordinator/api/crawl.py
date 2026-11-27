"""Admin API for crawling: enqueue URLs, inspect and steer domains."""

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from pos_coordinator.api.deps import CurrentAdmin, CurrentViewer, SensitiveAdmin, Session
from pos_coordinator.crawl import discovery, scheduler
from pos_coordinator.models import (
    AuditLog,
    DomainEgressState,
    DomainEvent,
    DomainState,
    Job,
    Source,
)

router = APIRouter(prefix="/api/admin")


class EnqueueUrls(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=50_000)
    source: str | None = Field(None, max_length=100)
    priority: int = Field(0, ge=-100, le=100)


@router.post("/crawl/urls", status_code=status.HTTP_201_CREATED)
async def enqueue_urls(body: EnqueueUrls, admin: CurrentAdmin, session: Session):
    report = await scheduler.enqueue_urls(session, body.urls, body.source, body.priority)
    session.add(
        AuditLog(
            actor=admin.name,
            action="crawl.enqueue",
            target=body.source,
            details={
                "queued": report.queued,
                "duplicates": report.duplicates,
                "invalid": len(report.invalid or []),
            },
        )
    )
    await session.commit()
    return {
        "queued": report.queued,
        "duplicates": report.duplicates,
        "invalid": (report.invalid or [])[:100],
    }


def _domain_view(d: DomainState, groups: list[DomainEgressState], queued: int) -> dict[str, Any]:
    return {
        "domain": d.domain,
        "block_status": d.block_status,
        "block_reason": d.block_reason,
        "blocked_at": d.blocked_at,
        "fetch_mode": d.fetch_mode,
        "robots_status": d.robots_status,
        "robots_http_status": d.robots_http_status,
        "robots_crawl_delay_s": d.robots_crawl_delay_s,
        "robots_fetched_at": d.robots_fetched_at,
        "pages_fetched": d.pages_fetched,
        "last_fetch_at": d.last_fetch_at,
        "overrides": d.overrides,
        "queued_jobs": queued,
        "egress_groups": [
            {
                "egress_group": g.egress_group,
                "rate_rps": g.rate_rps,
                "cooldown_until": g.cooldown_until,
                "next_slot_at": g.next_slot_at,
                "throttle_count": g.throttle_count,
                "latency_ewma_ms": g.latency_ewma_ms,
            }
            for g in groups
        ],
    }


@router.get("/domains")
async def list_domains(
    admin: CurrentViewer, session: Session, limit: int = 100, blocked: bool | None = None
):
    q = select(DomainState).order_by(DomainState.last_fetch_at.desc().nullslast()).limit(limit)
    if blocked is not None:
        q = q.where((DomainState.block_status == "blocked") == blocked)
    domains = (await session.execute(q)).scalars().all()
    names = [d.domain for d in domains]
    groups: dict[str, list] = {}
    for g in (
        await session.execute(select(DomainEgressState).where(DomainEgressState.domain.in_(names)))
    ).scalars():
        groups.setdefault(g.domain, []).append(g)
    queued = dict(
        (
            await session.execute(
                select(Job.domain, func.count())
                .where(Job.domain.in_(names), Job.status == "queued")
                .group_by(Job.domain)
            )
        ).all()
    )
    return [_domain_view(d, groups.get(d.domain, []), queued.get(d.domain, 0)) for d in domains]


class DomainUpdate(BaseModel):
    unblock: bool = False
    rate_rps: float | None = Field(None, gt=0, le=50, description="per-domain rate cap")
    clear_rate_override: bool = False
    fetch_mode: str | None = Field(None, pattern="^(http|browser)$")


@router.patch("/domains/{domain}")
async def update_domain(domain: str, body: DomainUpdate, admin: SensitiveAdmin, session: Session):
    d = await session.get(DomainState, domain, with_for_update=True)
    if d is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown domain")
    changes = body.model_dump(exclude_defaults=True)
    if body.unblock and d.block_status == "blocked":
        d.block_status, d.block_reason, d.blocked_at = "ok", None, None
        session.add(DomainEvent(domain=domain, kind="unblocked", reason=f"admin:{admin.name}"))
        for g in (
            await session.execute(
                select(DomainEgressState).where(DomainEgressState.domain == domain)
            )
        ).scalars():
            g.cooldown_until, g.consecutive_403, g.consecutive_blocks = None, 0, 0
    overrides = dict(d.overrides or {})
    if body.rate_rps is not None:
        overrides["rate_rps"] = body.rate_rps
    if body.clear_rate_override:
        overrides.pop("rate_rps", None)
    d.overrides = overrides
    if body.fetch_mode:
        d.fetch_mode = body.fetch_mode
    d.updated_at = func.now()
    session.add(AuditLog(actor=admin.name, action="domain.update", target=domain, details=changes))
    await session.commit()
    return {
        "domain": domain,
        "block_status": d.block_status,
        "overrides": d.overrides,
        "fetch_mode": d.fetch_mode,
    }


@router.get("/domains/{domain}/events")
async def domain_events(domain: str, admin: CurrentViewer, session: Session, limit: int = 200):
    rows = (
        await session.execute(
            select(DomainEvent)
            .where(DomainEvent.domain == domain)
            .order_by(DomainEvent.at.desc())
            .limit(limit)
        )
    ).scalars()
    return [
        {
            "at": e.at,
            "kind": e.kind,
            "reason": e.reason,
            "egress_group": e.egress_group,
            "old_rate_rps": e.old_rate_rps,
            "new_rate_rps": e.new_rate_rps,
            "details": e.details,
        }
        for e in rows
    ]


# --- sources -----------------------------------------------------------------------


class SourceIn(BaseModel):
    type: str = Field("scraper", pattern="^(dataset|api|scraper|reference)$")
    status: str = Field("active", pattern="^(active|paused|disabled)$")
    config: dict[str, Any] = Field(default_factory=dict)


def _source_view(s: Source) -> dict[str, Any]:
    return {
        "name": s.name,
        "type": s.type,
        "status": s.status,
        "config": s.config,
        "created_at": s.created_at,
    }


@router.put("/sources/{name}")
async def put_source(name: str, body: SourceIn, admin: CurrentAdmin, session: Session):
    if not name or len(name) > 100:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "bad source name")
    try:
        discovery.validate_config(body.config)
    except discovery.SourceConfigError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    src = (await session.execute(select(Source).where(Source.name == name))).scalar_one_or_none()
    if src is None:
        src = Source(name=name, type=body.type, status=body.status, config=body.config)
        session.add(src)
    else:
        src.type, src.status, src.config = body.type, body.status, body.config
    session.add(
        AuditLog(actor=admin.name, action="source.put", target=name, details=body.model_dump())
    )
    await session.commit()
    return _source_view(src)


@router.get("/sources")
async def list_sources(admin: CurrentViewer, session: Session):
    rows = (await session.execute(select(Source).order_by(Source.name))).scalars()
    return [_source_view(s) for s in rows]


@router.post("/sources/{name}/discover")
async def discover_source(name: str, admin: CurrentAdmin, session: Session):
    src = (await session.execute(select(Source).where(Source.name == name))).scalar_one_or_none()
    if src is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown source")
    if src.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, f"source is {src.status}")
    queued = await discovery.discover(session, src)
    session.add(AuditLog(actor=admin.name, action="source.discover", target=name, details=queued))
    await session.commit()
    return queued
