"""Updates of the whole installation (updates.py): what is available, start, cancel.

Used by the panel's Releases page and by `install.sh --update` (admin token)."""

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from pos_coordinator import updates
from pos_coordinator.api.deps import (
    AppSettings,
    CurrentAdmin,
    CurrentViewer,
    SensitiveAdmin,
    Session,
)
from pos_coordinator.maintenance import sync_agent_exchange
from pos_shared.api import VERSION_PATTERN

router = APIRouter(prefix="/api/admin/updates")


async def _overview(request: Request, session, settings) -> dict:
    return await updates.overview(
        session,
        agent_dir=settings.agent_dir,
        offline_after_s=settings.node_offline_after_s,
        last_check=getattr(request.app.state, "update_check", None),
        repository=settings.update_repository,
    )


@router.get("")
async def get_updates(
    request: Request, admin: CurrentViewer, session: Session, settings: AppSettings
):
    return await _overview(request, session, settings)


@router.post("/check")
async def check_now(request: Request, admin: CurrentAdmin, session: Session, settings: AppSettings):
    """Ask GitHub now (without waiting for the next scheduled check)."""
    request.app.state.update_check = await updates.check(
        settings.update_repository, getattr(request.app.state, "update_http", None)
    )
    return await _overview(request, session, settings)


class StartUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(pattern=VERSION_PATTERN)


@router.post("/start")
async def start_update(
    body: StartUpdate,
    request: Request,
    admin: SensitiveAdmin,
    session: Session,
    settings: AppSettings,
):
    try:
        await updates.start(
            session,
            key=request.app.state.signing_key,
            agent_dir=settings.agent_dir,
            repository=settings.update_repository,
            version=body.version,
            actor=admin.name,
            http=getattr(request.app.state, "update_http", None),
        )
        await session.commit()
    except updates.UpdateError as e:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except IntegrityError as e:  # started twice at once, or the version registered meanwhile
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "an update is already starting") from e
    if settings.agent_dir is not None:
        # hand the release to the agent now rather than at the next maintenance round
        await sync_agent_exchange(session, settings.agent_dir, request.app.state.signing_key)
    return await _overview(request, session, settings)


@router.post("/cancel")
async def cancel_update(
    request: Request, admin: SensitiveAdmin, session: Session, settings: AppSettings
):
    if await updates.cancel(session, admin.name) is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no update is running")
    await session.commit()
    return await _overview(request, session, settings)
