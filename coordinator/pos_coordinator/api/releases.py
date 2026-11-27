"""Signed releases.

An admin registers a release (images pinned by digest); the coordinator signs
the manifest with its own key. Nodes fetch the signed manifest for the version
they are told to run, and their agent verifies it against the key pinned at
enrollment before pulling anything. So a node only ever runs images that its
own coordinator approved, whatever happens to the registry.
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pos_coordinator.api.deps import CurrentAdmin, CurrentNode, Session, check_api_version
from pos_coordinator.models import AuditLog, Release
from pos_shared import signing
from pos_shared.api import VERSION_PATTERN, ReleaseManifest, SignedManifest

node_router = APIRouter(prefix="/api/v1/releases", dependencies=[Depends(check_api_version)])
admin_router = APIRouter(prefix="/api/admin/releases")


def signing_public_key(request: Request) -> str:
    return signing.public_key_b64(request.app.state.signing_key)


def _signed(r: Release) -> SignedManifest:
    return SignedManifest(
        manifest=ReleaseManifest.model_validate(r.manifest), signature=r.signature, key_id=r.key_id
    )


@node_router.get("/{version}", response_model=SignedManifest)
async def get_release_for_node(version: str, node: CurrentNode, session: Session):
    r = await session.get(Release, version)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown release")
    return _signed(r)


class ReleaseCreate(BaseModel):
    version: str = Field(pattern=VERSION_PATTERN)
    images: dict[str, str]
    notes: str | None = Field(None, max_length=10000)


@admin_router.post("", status_code=status.HTTP_201_CREATED, response_model=SignedManifest)
async def create_release(
    body: ReleaseCreate, request: Request, admin: CurrentAdmin, session: Session
):
    try:
        manifest = ReleaseManifest(
            version=body.version, images=body.images, created_at=datetime.now(UTC)
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    payload = manifest.model_dump(mode="json")
    key = request.app.state.signing_key
    release = Release(
        version=manifest.version,
        manifest=payload,
        signature=signing.sign(key, payload),
        key_id=signing.key_id(signing.public_key_b64(key)),
        notes=body.notes,
        created_by=admin.name,
    )
    session.add(release)
    session.add(
        AuditLog(
            actor=admin.name, action="release.create", target=manifest.version, details=payload
        )
    )
    try:
        await session.commit()
    except IntegrityError as e:
        # Releases are immutable: a version means one set of digests forever.
        raise HTTPException(status.HTTP_409_CONFLICT, "release version already exists") from e
    return _signed(release)


@admin_router.get("")
async def list_releases(admin: CurrentAdmin, session: Session) -> list[dict[str, Any]]:
    rows = (await session.execute(select(Release).order_by(Release.created_at.desc()))).scalars()
    return [
        {**_signed(r).model_dump(mode="json"), "notes": r.notes, "created_at": r.created_at}
        for r in rows
    ]


@admin_router.get("/signing-key")
async def get_signing_key(request: Request, admin: CurrentAdmin):
    pub = signing_public_key(request)
    return {"public_key": pub, "key_id": signing.key_id(pub)}


router = APIRouter()
router.include_router(node_router)
router.include_router(admin_router)
