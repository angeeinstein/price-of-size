"""Request dependencies: DB session, node and admin authentication, API version check."""

import hmac
import ipaddress
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator.config import Settings
from pos_coordinator.models import Node, NodeToken
from pos_coordinator.rawstore import RawStore
from pos_coordinator.security import hash_secret
from pos_shared.version import API_VERSION, API_VERSION_HEADER, is_compatible


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_rawstore(request: Request) -> RawStore:
    return request.app.state.rawstore


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(get_settings_dep)]


def check_api_version(
    x_pos_api_version: Annotated[str | None, Header(alias=API_VERSION_HEADER)] = None,
) -> None:
    """Refuse clients whose API version this coordinator cannot serve (HTTP 426)."""
    if x_pos_api_version is None or not is_compatible(x_pos_api_version, API_VERSION):
        raise HTTPException(
            status.HTTP_426_UPGRADE_REQUIRED,
            f"incompatible API version {x_pos_api_version!r}; coordinator speaks {API_VERSION}",
        )


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    return token


async def current_node(request: Request, session: Session) -> Node:
    token = _bearer(request)
    row = (
        await session.execute(
            select(Node)
            .join(NodeToken, NodeToken.node_id == Node.id)
            .where(
                NodeToken.token_hash == hash_secret(token),
                NodeToken.expires_at > func.now(),
                Node.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    return row


CurrentNode = Annotated[Node, Depends(current_node)]


@dataclass(frozen=True)
class Admin:
    """Who is calling an admin endpoint: the bootstrap token or a panel user."""

    name: str
    role: str = "admin"  # admin | viewer
    session: Any = None  # UserSession for panel users, None for the bootstrap token
    user_id: Any = None


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def current_principal(request: Request, session: Session, settings: AppSettings) -> Admin:
    """Bootstrap token (Authorization: Bearer POS_ADMIN_TOKEN) or a panel session cookie.

    Cookie-authenticated state changes must carry the CSRF token; bearer-token
    requests are not sent automatically by browsers, so they need none.
    """
    from pos_coordinator.panel import auth as panel_auth

    header = request.headers.get("authorization", "")
    if header:
        token = _bearer(request)
        if settings.admin_token and hmac.compare_digest(token, settings.admin_token):
            return Admin(name="admin-token")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "admin authentication required")
    cookie = request.cookies.get(panel_auth.COOKIE)
    if cookie:
        loaded = await panel_auth.load_session(session, cookie)
        if loaded is not None:
            user_session, user = loaded
            if request.method in UNSAFE_METHODS and not panel_auth.csrf_ok(
                user_session, request.headers.get(panel_auth.CSRF_HEADER)
            ):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "missing or wrong CSRF token")
            return Admin(name=user.username, role=user.role, session=user_session, user_id=user.id)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")


def current_viewer(principal: Annotated[Admin, Depends(current_principal)]) -> Admin:
    return principal


def current_admin(principal: Annotated[Admin, Depends(current_principal)]) -> Admin:
    if principal.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return principal


def recent_reauth(principal: Annotated[Admin, Depends(current_admin)]) -> Admin:
    """Sensitive actions: a panel user must have re-entered password (+TOTP) recently."""
    from pos_coordinator.panel import auth as panel_auth

    if principal.session is not None and not panel_auth.recently_reauthenticated(principal.session):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "reauthentication required")
    return principal


CurrentAdmin = Annotated[Admin, Depends(current_admin)]
CurrentViewer = Annotated[Admin, Depends(current_viewer)]
SensitiveAdmin = Annotated[Admin, Depends(recent_reauth)]


CLIENT_IP_HEADER = "x-pos-client-ip"  # set by Caddy, overwriting what callers send


def client_ip(request: Request, settings: Settings) -> str | None:
    """Public egress IP of the caller as the coordinator sees it. Behind the agent's
    Caddy (trust_proxy_headers), only its X-Pos-Client-Ip: Caddy believes
    CF-Connecting-IP / X-Forwarded-For only from trusted proxies (the cloudflared
    host) and sets the header on every upstream request, overwriting the caller's.
    Those headers themselves are never read here: a caller could send them."""
    candidates: list[str] = []
    if settings.trust_proxy_headers and (own := request.headers.get(CLIENT_IP_HEADER)):
        candidates.append(own.strip())
    if request.client:
        candidates.append(request.client.host)
    for c in candidates:
        try:
            return str(ipaddress.ip_address(c))
        except ValueError:
            continue
    return None


LAN_GROUP = "lan"
# A local network: RFC 1918, loopback, link-local and IPv6 unique local addresses
# (not Python's is_private, which also counts documentation and other reserved ranges).
LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )  # fmt: skip
)


def is_local(addr: ipaddress.IPv4Address | ipaddress.IPv6Address | str | None) -> bool:
    """An address on a local network: a caller on the coordinator's LAN."""
    if addr is None:
        return False
    if isinstance(addr, str):
        try:
            addr = ipaddress.ip_address(addr)
        except ValueError:
            return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in n for n in LOCAL_NETWORKS if n.version == addr.version)


def request_https(request: Request, settings: Settings) -> bool:
    """Whether the caller reached us over HTTPS. Behind Caddy, X-Forwarded-Proto is
    Caddy's: passed on from trusted proxies (Cloudflare), else the scheme it saw."""
    if settings.trust_proxy_headers and (proto := request.headers.get("x-forwarded-proto")):
        return proto.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def egress_group_for(ip: str | None) -> str | None:
    """Nodes behind the same public IP share one per-domain budget.

    IPv6 hosts usually get a whole /64, so group by that prefix.
    """
    if ip is None:
        return None
    addr = ipaddress.ip_address(ip)
    if is_local(addr):
        # callers on the coordinator's own network (a worker beside it, nodes on the
        # LAN) leave through the same public address: one budget
        return LAN_GROUP
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)
