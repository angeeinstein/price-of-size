"""Admin API (bootstrap token for now; the control panel will sit on top of this).

Every state-changing action is written to `audit_log`.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from pos_coordinator import budget, queue, runtime_settings
from pos_coordinator.api.deps import (
    AppSettings,
    CurrentAdmin,
    CurrentViewer,
    SensitiveAdmin,
    Session,
)
from pos_coordinator.models import (
    AuditLog,
    EnrollmentCode,
    Job,
    Node,
    NodeCommand,
    NodeToken,
    Release,
)
from pos_coordinator.security import hash_secret, new_enrollment_code, normalize_code
from pos_shared.api import NodeLimits, RunState

router = APIRouter(prefix="/api/admin")


def _audit(session, admin, action: str, target: str | None = None, **details: Any) -> None:
    session.add(AuditLog(actor=admin.name, action=action, target=target, details=details))


# --- enrollment ----------------------------------------------------------


class EnrollmentCodeRequest(BaseModel):
    label: str | None = None
    trust_level: str = Field("owned", pattern="^(owned|community)$")
    ttl_s: int | None = Field(None, ge=60, le=30 * 86400)


@router.post("/enrollment-codes", status_code=status.HTTP_201_CREATED)
async def create_enrollment_code(
    body: EnrollmentCodeRequest, admin: SensitiveAdmin, session: Session, settings: AppSettings
):
    code = new_enrollment_code()
    expires = datetime.now(UTC) + timedelta(seconds=body.ttl_s or settings.enrollment_code_ttl_s)
    session.add(
        EnrollmentCode(
            code_hash=hash_secret(normalize_code(code)),
            label=body.label,
            trust_level=body.trust_level,
            expires_at=expires,
        )
    )
    _audit(session, admin, "enrollment_code.create", body.label, trust_level=body.trust_level)
    await session.commit()
    return {"code": code, "expires_at": expires}


# --- nodes ---------------------------------------------------------------


def _node_view(n: Node, offline_after_s: int) -> dict[str, Any]:
    online = (
        n.last_heartbeat_at is not None
        and (datetime.now(UTC) - n.last_heartbeat_at).total_seconds() < offline_after_s
    )
    return {
        "id": n.id,
        "name": n.name,
        "trust_level": n.trust_level,
        "version": n.version,
        "capabilities": n.capabilities,
        "egress_ip": str(n.egress_ip) if n.egress_ip else None,
        "egress_group": n.egress_group,
        "actual_state": n.actual_state,
        "desired_run_state": n.desired_run_state,
        "desired_target_version": n.desired_target_version,
        "desired_limits": n.desired_limits,
        "resources": n.resources,
        "concurrency": n.concurrency,
        "agent_status": n.agent_status,
        "last_heartbeat_at": n.last_heartbeat_at,
        "online": online and n.revoked_at is None,
        "revoked_at": n.revoked_at,
    }


@router.get("/nodes")
async def list_nodes(admin: CurrentViewer, session: Session, settings: AppSettings):
    nodes = (await session.execute(select(Node).order_by(Node.enrolled_at))).scalars().all()
    return [_node_view(n, settings.node_offline_after_s) for n in nodes]


class DesiredStateUpdate(BaseModel):
    run_state: RunState | None = None
    target_version: str | None = None
    limits: NodeLimits | None = None
    job_types: list[str] | None = None


async def _get_node(session, node_id: UUID) -> Node:
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such node")
    return node


@router.patch("/nodes/{node_id}")
async def update_node(
    node_id: UUID,
    body: DesiredStateUpdate,
    admin: SensitiveAdmin,
    session: Session,
    settings: AppSettings,
):
    node = await _get_node(session, node_id)
    changes = body.model_dump(exclude_unset=True, mode="json")
    if "run_state" in changes:
        node.desired_run_state = changes["run_state"]
    if "target_version" in changes:
        target = changes["target_version"]
        if target is not None and await session.get(Release, target) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown release {target}")
        node.desired_target_version = target
    if "limits" in changes:
        node.desired_limits = changes["limits"]
    if "job_types" in changes:
        node.desired_job_types = changes["job_types"]
    _audit(session, admin, "node.update", str(node_id), **changes)
    await session.commit()
    return _node_view(node, settings.node_offline_after_s)


@router.post("/nodes/{node_id}/revoke")
async def revoke_node(node_id: UUID, admin: SensitiveAdmin, session: Session):
    node = await _get_node(session, node_id)
    node.revoked_at = func.now()
    await session.execute(delete(NodeToken).where(NodeToken.node_id == node_id))
    released = await queue.release_node(session, node_id)
    _audit(session, admin, "node.revoke", str(node_id), released_jobs=released)
    await session.commit()
    return {"revoked": True, "released_jobs": released}


class CommandRequest(BaseModel):
    type: str = Field(pattern=r"^[a-z_]{1,40}$")
    args: dict[str, Any] = Field(default_factory=dict)


@router.post("/nodes/{node_id}/commands", status_code=status.HTTP_201_CREATED)
async def send_command(node_id: UUID, body: CommandRequest, admin: CurrentAdmin, session: Session):
    await _get_node(session, node_id)
    cmd = NodeCommand(node_id=node_id, type=body.type, args=body.args)
    session.add(cmd)
    _audit(session, admin, "node.command", str(node_id), type=body.type)
    await session.commit()
    return {"id": cmd.id}


@router.get("/nodes/{node_id}/commands")
async def list_commands(node_id: UUID, admin: CurrentViewer, session: Session):
    rows = (
        await session.execute(
            select(NodeCommand)
            .where(NodeCommand.node_id == node_id)
            .order_by(NodeCommand.id.desc())
            .limit(100)
        )
    ).scalars()
    return [
        {
            "id": c.id,
            "type": c.type,
            "args": c.args,
            "created_at": c.created_at,
            "delivered_at": c.delivered_at,
            "acked_at": c.acked_at,
            "ok": c.ok,
            "result": c.result,
        }
        for c in rows
    ]


# --- jobs ----------------------------------------------------------------


class EnqueueJob(BaseModel):
    type: str = Field(pattern=r"^[a-z_]{1,40}$")
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(0, ge=-100, le=100)
    max_attempts: int = Field(5, ge=1, le=50)
    domain: str | None = None


class EnqueueRequest(BaseModel):
    jobs: list[EnqueueJob] = Field(min_length=1, max_length=10000)


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
async def enqueue_jobs(body: EnqueueRequest, admin: CurrentAdmin, session: Session):
    ids = await queue.enqueue(session, [queue.NewJob(**j.model_dump()) for j in body.jobs])
    _audit(session, admin, "jobs.enqueue", None, count=len(ids))
    await session.commit()
    return {"ids": ids}


JOB_STATUS_PATTERN = "^(queued|leased|done|failed|cancelled)$"


@router.get("/jobs")
async def list_jobs(
    viewer: CurrentViewer,
    session: Session,
    status_: str | None = Query(
        None, alias="status", pattern="^(queued|leased|done|failed|cancelled)$"
    ),
    type_: str | None = Query(None, alias="type", max_length=40),
    limit: int = Query(100, ge=1, le=1000),
):
    q = select(Job).order_by(Job.updated_at.desc()).limit(limit)
    if status_:
        q = q.where(Job.status == status_)
    if type_:
        q = q.where(Job.type == type_)
    return [
        {
            "id": j.id,
            "type": j.type,
            "status": j.status,
            "attempts": j.attempts,
            "max_attempts": j.max_attempts,
            "domain": j.domain,
            "url": (j.payload or {}).get("url"),
            "last_error": (j.last_error or "")[-2000:] or None,
            "updated_at": j.updated_at,
            "not_before": j.not_before,
        }
        for j in (await session.execute(q)).scalars()
    ]


class JobAction(BaseModel):
    action: str = Field(pattern="^(retry|cancel)$")


@router.post("/jobs/{job_id}")
async def job_action(job_id: UUID, body: JobAction, admin: CurrentAdmin, session: Session):
    job = await session.get(Job, job_id, with_for_update=True)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such job")
    if body.action == "retry":
        if job.status not in ("failed", "cancelled"):
            raise HTTPException(status.HTTP_409_CONFLICT, f"job is {job.status}")
        job.status, job.attempts, job.not_before = "queued", 0, func.now()
        job.finished_at = None
    else:
        if job.status not in ("queued", "failed"):
            raise HTTPException(status.HTTP_409_CONFLICT, f"job is {job.status}")
        job.status, job.finished_at = "cancelled", func.now()
    job.updated_at = func.now()
    _audit(session, admin, f"job.{body.action}", str(job_id))
    await session.commit()
    return {"id": job_id, "action": body.action}


@router.get("/audit")
async def audit_log(viewer: CurrentViewer, session: Session, limit: int = Query(200, le=1000)):
    rows = (
        await session.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))
    ).scalars()
    return [
        {
            "at": a.created_at,
            "actor": a.actor,
            "action": a.action,
            "target": a.target,
            "details": a.details,
        }
        for a in rows
    ]


@router.get("/jobs/stats")
async def job_stats(admin: CurrentViewer, session: Session):
    return await queue.queue_stats(session)


# --- settings and budget ---------------------------------------------------


@router.get("/settings")
async def get_settings(admin: CurrentViewer, session: Session):
    return await runtime_settings.get_all(session)


@router.put("/settings/{key}")
async def put_setting(key: str, body: dict[str, Any], admin: SensitiveAdmin, session: Session):
    if "value" not in body:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, 'body must be {"value": ...}')
    try:
        await runtime_settings.put(session, key, body["value"], admin.name)
    except runtime_settings.UnknownSetting as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown setting {key}") from e
    except runtime_settings.InvalidSetting as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    _audit(session, admin, "setting.put", key, value=body["value"])
    await session.commit()
    return {"key": key, "value": body["value"]}


@router.get("/budget")
async def get_budget(admin: CurrentViewer, session: Session):
    s = await budget.status(session)
    return {**s.__dict__, "ahead_of_pace": s.ahead_of_pace}
