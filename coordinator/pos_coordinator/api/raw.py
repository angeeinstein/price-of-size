"""Raw object upload: content-addressed, idempotent, size-limited."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pos_coordinator.api.deps import (
    AppSettings,
    CurrentNode,
    Session,
    check_api_version,
    get_rawstore,
)
from pos_coordinator.models import RawObject
from pos_coordinator.rawstore import KINDS, SHA256_RE, HashMismatch, RawStore, StoreUnavailable

router = APIRouter(prefix="/api/v1/raw", dependencies=[Depends(check_api_version)])

Store = Annotated[RawStore, Depends(get_rawstore)]


def _check(kind: str, sha256: str) -> None:
    if kind not in KINDS or not SHA256_RE.match(sha256):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown kind or malformed hash")


@router.head("/{kind}/{sha256}")
async def exists(kind: str, sha256: str, node: CurrentNode, session: Session, store: Store):
    """200 only if the object is fully stored: file *and* database row. A file without
    a row (upload interrupted between the two) counts as missing, so the next upload
    adds the row instead of being skipped."""
    _check(kind, sha256)
    _available(store)
    row = await session.get(RawObject, sha256)
    return Response(status_code=200 if row is not None and store.exists(kind, sha256) else 404)


def _available(store: RawStore) -> None:
    if not store.available():
        # Nodes keep the object in their spool and try again.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "raw store unavailable",
            headers={"Retry-After": "60"},
        )


@router.put("/{kind}/{sha256}", status_code=status.HTTP_201_CREATED)
async def upload(
    kind: str,
    sha256: str,
    request: Request,
    node: CurrentNode,
    session: Session,
    settings: AppSettings,
    store: Store,
):
    _check(kind, sha256)
    declared = request.headers.get("content-length")
    if declared is not None and int(declared) > settings.max_raw_object_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "object too large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > settings.max_raw_object_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "object too large")
        chunks.append(chunk)
    data = b"".join(chunks)
    _available(store)
    try:
        created = store.put(kind, sha256, data)
    except HashMismatch as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except StoreUnavailable:
        _available(store)  # it went away just now
        raise
    await session.execute(
        pg_insert(RawObject)
        .values(sha256=sha256, size_bytes=size, kind=kind, uploaded_by_node_id=node.id)
        .on_conflict_do_nothing(index_elements=["sha256"])
    )
    await session.commit()
    return {"sha256": sha256, "created": created}
