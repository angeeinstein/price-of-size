"""Request dependencies: DB session, node and admin authentication, API version check."""

import hmac
import ipaddress
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

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
    name: str


def current_admin(request: Request, settings: AppSettings) -> Admin:
    """Bootstrap admin auth via POS_ADMIN_TOKEN. Replaced by panel accounts (passkeys/TOTP)."""
    token = _bearer(request)
    if not settings.admin_token or not hmac.compare_digest(token, settings.admin_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "admin authentication required")
    return Admin(name="admin-token")


CurrentAdmin = Annotated[Admin, Depends(current_admin)]


def client_ip(request: Request, settings: Settings) -> str | None:
    """Public egress IP of the caller as the coordinator sees it."""
    candidates: list[str] = []
    if settings.trust_proxy_headers:
        if cf := request.headers.get("cf-connecting-ip"):
            candidates.append(cf.strip())
        if xff := request.headers.get("x-forwarded-for"):
            candidates.append(xff.split(",")[0].strip())
    if request.client:
        candidates.append(request.client.host)
    for c in candidates:
        try:
            return str(ipaddress.ip_address(c))
        except ValueError:
            continue
    return None


def egress_group_for(ip: str | None) -> str | None:
    """Nodes behind the same public IP share one per-domain budget.

    IPv6 hosts usually get a whole /64, so group by that prefix.
    """
    if ip is None:
        return None
    addr = ipaddress.ip_address(ip)
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)
