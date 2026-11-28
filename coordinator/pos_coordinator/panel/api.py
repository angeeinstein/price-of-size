"""Panel endpoints: login/logout, account (password, TOTP), users, overview + live stream."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pyotp
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text

from pos_coordinator import budget
from pos_coordinator.api.deps import (
    AppSettings,
    CurrentAdmin,
    CurrentViewer,
    SensitiveAdmin,
    Session,
    client_ip,
    is_local,
    request_https,
)
from pos_coordinator.models import (
    AppUser,
    AuditLog,
    DomainEvent,
    DomainState,
    Job,
    Node,
    RawFetch,
    UserSession,
)
from pos_coordinator.panel import auth

router = APIRouter(prefix="/api/panel")
ISSUER = "The Price of Size"


def _user_view(u: AppUser) -> dict[str, Any]:
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role,
        "totp": bool(u.totp_secret),
        "disabled": u.disabled_at is not None,
        "last_login_at": u.last_login_at,
        "created_at": u.created_at,
    }


def _set_cookie(response: Response, token: str, secure: bool) -> None:
    response.set_cookie(
        auth.COOKIE,
        token,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=int(auth.SESSION_MAX.total_seconds()),
        path="/",
    )


# --- login ----------------------------------------------------------------------------


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1000)
    totp: str | None = Field(None, max_length=10)


@router.post("/login")
async def login(
    body: LoginIn, request: Request, response: Response, session: Session, settings: AppSettings
):
    ip = client_ip(request, settings)
    try:
        user = await auth.authenticate(session, body.username, body.password, body.totp, ip)
    except auth.RateLimited as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, e.reason) from e
    except auth.AuthError as e:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, {"detail": e.reason, "totp_required": e.totp_required}
        ) from e
    token, s = await auth.create_session(session, user, ip, request.headers.get("user-agent"))
    session.add(AuditLog(actor=user.username, action="login", target=ip))
    await session.commit()
    # Secure, except behind Caddy for a browser on the LAN over plain HTTP (the panel
    # by its LAN address): it would never send a Secure cookie back
    plain_lan = (
        settings.trust_proxy_headers and not request_https(request, settings) and is_local(ip)
    )
    _set_cookie(response, token, settings.cookie_secure and not plain_lan)
    return {"user": _user_view(user), "csrf_token": s.csrf_token}


@router.post("/logout")
async def logout(request: Request, response: Response, session: Session):
    token = request.cookies.get(auth.COOKIE)
    if token:
        await session.execute(
            delete(UserSession).where(UserSession.token_hash == auth.hash_secret(token))
        )
        await session.commit()
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(principal: CurrentViewer, session: Session):
    if principal.session is None:
        return {
            "user": {"username": principal.name, "role": principal.role, "totp": False},
            "csrf_token": None,
            "token_auth": True,
        }
    user = await session.get(AppUser, principal.user_id)
    await session.commit()  # persists last_seen_at
    return {
        "user": _user_view(user),
        "csrf_token": principal.session.csrf_token,
        "reauth_valid": auth.recently_reauthenticated(principal.session),
    }


class ReauthIn(BaseModel):
    password: str = Field(min_length=1, max_length=1000)
    totp: str | None = Field(None, max_length=10)


@router.post("/reauth")
async def reauth(
    body: ReauthIn,
    request: Request,
    principal: CurrentViewer,
    session: Session,
    settings: AppSettings,
):
    if principal.session is None:
        return {"ok": True}
    user = await session.get(AppUser, principal.user_id)
    try:
        await auth.authenticate(
            session, user.username, body.password, body.totp, client_ip(request, settings)
        )
    except auth.RateLimited as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, e.reason) from e
    except auth.AuthError as e:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, {"detail": e.reason, "totp_required": e.totp_required}
        ) from e
    s = await session.get(UserSession, principal.session.token_hash)
    s.reauth_at = datetime.now(UTC)
    await session.commit()
    return {"ok": True, "valid_until": s.reauth_at + auth.REAUTH_WINDOW}


# --- account ---------------------------------------------------------------------------


class PasswordIn(BaseModel):
    current: str = Field(max_length=1000)
    new: str = Field(max_length=1000)


async def _session_user(principal, session) -> AppUser:
    if principal.session is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "only for panel accounts")
    return await session.get(AppUser, principal.user_id)


@router.post("/me/password")
async def change_password(body: PasswordIn, principal: CurrentViewer, session: Session):
    user = await _session_user(principal, session)
    if not auth.verify_password(user, body.current):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is wrong")
    try:
        user.password_hash = auth.hash_password(body.new)
    except auth.AuthError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.reason) from e
    user.password_changed_at = datetime.now(UTC)
    # every other session of this user ends
    await session.execute(
        delete(UserSession).where(
            UserSession.user_id == user.id, UserSession.token_hash != principal.session.token_hash
        )
    )
    session.add(AuditLog(actor=user.username, action="user.password", target=user.username))
    await session.commit()
    return {"ok": True}


@router.post("/me/totp/setup")
async def totp_setup(principal: CurrentViewer, session: Session):
    user = await _session_user(principal, session)
    if not auth.recently_reauthenticated(principal.session):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "reauthentication required")
    secret = pyotp.random_base32()
    user.totp_pending_secret = secret
    await session.commit()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name=ISSUER)
    return {"secret": secret, "otpauth_uri": uri}


class TotpIn(BaseModel):
    code: str = Field(min_length=6, max_length=10)


@router.post("/me/totp/enable")
async def totp_enable(body: TotpIn, principal: CurrentViewer, session: Session):
    user = await _session_user(principal, session)
    if not user.totp_pending_secret:
        raise HTTPException(status.HTTP_409_CONFLICT, "start the setup first")
    if not pyotp.TOTP(user.totp_pending_secret).verify(body.code.strip(), valid_window=1):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "code does not match")
    user.totp_secret, user.totp_pending_secret = user.totp_pending_secret, None
    user.totp_last_counter = int(datetime.now(UTC).timestamp()) // 30
    session.add(AuditLog(actor=user.username, action="user.totp_enable", target=user.username))
    await session.commit()
    return {"ok": True}


@router.post("/me/totp/disable")
async def totp_disable(principal: CurrentViewer, session: Session):
    user = await _session_user(principal, session)
    if not auth.recently_reauthenticated(principal.session):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "reauthentication required")
    user.totp_secret = user.totp_pending_secret = None
    session.add(AuditLog(actor=user.username, action="user.totp_disable", target=user.username))
    await session.commit()
    return {"ok": True}


# --- users (admins) --------------------------------------------------------------------


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=60, pattern=r"^[A-Za-z0-9._@-]+$")
    password: str = Field(max_length=1000)
    role: str = Field("viewer", pattern="^(admin|viewer)$")


@router.get("/users")
async def list_users(admin: CurrentAdmin, session: Session):
    rows = (await session.execute(select(AppUser).order_by(AppUser.username))).scalars()
    return [_user_view(u) for u in rows]


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(body: UserIn, admin: SensitiveAdmin, session: Session):
    name = auth.normalize_username(body.username)
    if (await session.execute(select(AppUser).where(AppUser.username == name))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "username taken")
    try:
        user = AppUser(
            username=name, password_hash=auth.hash_password(body.password), role=body.role
        )
    except auth.AuthError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.reason) from e
    session.add(user)
    session.add(
        AuditLog(actor=admin.name, action="user.create", target=name, details={"role": body.role})
    )
    await session.commit()
    return _user_view(user)


class UserUpdate(BaseModel):
    role: str | None = Field(None, pattern="^(admin|viewer)$")
    disabled: bool | None = None
    reset_totp: bool = False


@router.patch("/users/{user_id}")
async def update_user(user_id: UUID, body: UserUpdate, admin: SensitiveAdmin, session: Session):
    user = await session.get(AppUser, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
    if admin.user_id == user.id and (body.role == "viewer" or body.disabled):
        raise HTTPException(status.HTTP_409_CONFLICT, "you cannot demote or disable yourself")
    if body.role:
        user.role = body.role
    if body.disabled is not None:
        user.disabled_at = datetime.now(UTC) if body.disabled else None
        if body.disabled:
            await session.execute(delete(UserSession).where(UserSession.user_id == user.id))
    if body.reset_totp:
        user.totp_secret = user.totp_pending_secret = None
    session.add(
        AuditLog(
            actor=admin.name,
            action="user.update",
            target=user.username,
            details=body.model_dump(exclude_defaults=True),
        )
    )
    await session.commit()
    return _user_view(user)


# --- overview ---------------------------------------------------------------------------


async def overview(session) -> dict[str, Any]:
    now = datetime.now(UTC)
    jobs = [
        {"type": t, "status": s, "count": c}
        for t, s, c in (
            await session.execute(
                select(Job.type, Job.status, func.count()).group_by(Job.type, Job.status)
            )
        ).all()
    ]
    nodes = (await session.execute(select(Node).where(Node.revoked_at.is_(None)))).scalars().all()
    online = sum(
        1 for n in nodes if n.last_heartbeat_at and (now - n.last_heartbeat_at).total_seconds() < 90
    )
    since = now - timedelta(hours=24)
    per_hour = [
        {"hour": h, "fetches": c}
        for h, c in (
            await session.execute(
                select(func.date_trunc("hour", RawFetch.fetched_at).label("h"), func.count())
                .where(RawFetch.fetched_at >= since)
                .group_by(text("h"))
                .order_by(text("h"))
            )
        ).all()
    ]
    domains = dict(
        (
            await session.execute(
                select(DomainState.block_status, func.count()).group_by(DomainState.block_status)
            )
        ).all()
    )
    events = [
        {"at": e.at, "domain": e.domain, "kind": e.kind, "reason": e.reason}
        for e in (
            await session.execute(select(DomainEvent).order_by(DomainEvent.at.desc()).limit(15))
        ).scalars()
    ]
    b = await budget.status(session)
    return {
        "at": now,
        "jobs": jobs,
        "nodes": {"total": len(nodes), "online": online},
        "fetches_per_hour": per_hour,
        "fetches_24h": sum(x["fetches"] for x in per_hour),
        "domains": {"total": sum(domains.values()), "blocked": domains.get("blocked", 0)},
        "budget": {
            "state": b.state,
            "spent_month_usd": b.spent_month_usd,
            "hard_cap_usd": b.hard_cap_usd,
            "daily_allowance_usd": b.daily_allowance_usd,
        },
        "recent_domain_events": events,
    }


@router.get("/overview")
async def get_overview(viewer: CurrentViewer, session: Session):
    return await overview(session)


@router.get("/stream")
async def stream(request: Request, viewer: CurrentViewer, session: Session):
    """Server-sent events: an overview snapshot every few seconds."""
    sm = request.app.state.sessionmaker
    # The request's session (authentication) would otherwise hold a pooled connection
    # for as long as the stream stays open; every snapshot takes its own.
    await session.close()

    async def events():
        while not await request.is_disconnected():
            async with sm() as s:
                data = await overview(s)
            yield f"event: overview\ndata: {json.dumps(data, default=str)}\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# --- analysis -------------------------------------------------------------------------

SCALARS = {
    "mass": "m.mass_kg",
    "volume": "m.a_m * m.b_m * m.c_m",
    "longest": "m.a_m",
    "charlen": "cbrt(m.a_m * m.b_m * m.c_m)",
}


@router.get("/scatter")
async def scatter(
    viewer: CurrentViewer,
    session: Session,
    x: str = "mass",
    limit: int = 5000,
    source: str | None = None,
):
    """Price against a size scalar, one point per product: its canonical price and its
    most trustworthy measurement (no implausibility flags; product > unknown > package)."""
    import math

    if x not in SCALARS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"x must be one of {list(SCALARS)}"
        )
    limit = max(1, min(limit, 20000))
    rows = (
        await session.execute(
            text(f"""
        SELECT p.id, p.title, p.source, p.canonical_price_eur::float AS price, best.x
        FROM product p
        JOIN LATERAL (
            SELECT {SCALARS[x]} AS x
            FROM measurement m
            WHERE m.product_id = p.id AND cardinality(m.flags) = 0
              AND {SCALARS[x]} IS NOT NULL AND {SCALARS[x]} > 0
            ORDER BY CASE m.basis WHEN 'product' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,
                     m.confidence DESC NULLS LAST
            LIMIT 1
        ) best ON true
        WHERE p.canonical_price_eur > 0 AND (CAST(:source AS text) IS NULL OR p.source = :source)
        ORDER BY random()
        LIMIT :limit
    """),
            {"source": source, "limit": limit},
        )
    ).all()
    points = [{"id": r.id, "t": r.title, "s": r.source, "p": r.price, "x": r.x} for r in rows]
    fit = None
    if len(points) >= 3:
        lx = [math.log10(pt["x"]) for pt in points]
        ly = [math.log10(pt["p"]) for pt in points]
        mx, my = sum(lx) / len(lx), sum(ly) / len(ly)
        sxx = sum((a - mx) ** 2 for a in lx)
        if sxx > 0:
            slope = sum((a - mx) * (b - my) for a, b in zip(lx, ly, strict=True)) / sxx
            fit = {"slope": slope, "intercept": my - slope * mx}
    return {"x": x, "n": len(points), "points": points, "fit": fit}
