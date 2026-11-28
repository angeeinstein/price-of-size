"""Node enrollment, challenge-response auth and the heartbeat control channel."""

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete, func, select, update

from pos_coordinator import runtime_settings
from pos_coordinator.api.deps import (
    AppSettings,
    CurrentNode,
    Session,
    check_api_version,
    client_ip,
    egress_group_for,
)
from pos_coordinator.models import EnrollmentCode, Node, NodeChallenge, NodeCommand, NodeToken
from pos_coordinator.security import (
    b64u_encode,
    hash_secret,
    new_token,
    normalize_code,
    parse_public_key,
    verify_signature,
)
from pos_shared import api, signing
from pos_shared.version import API_VERSION

router = APIRouter(prefix="/api/v1/nodes", dependencies=[Depends(check_api_version)])


@router.post("/enroll", response_model=api.EnrollResponse)
async def enroll(
    body: api.EnrollRequest, request: Request, session: Session, settings: AppSettings
):
    try:
        public_key = parse_public_key(body.public_key)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    code = (
        await session.execute(
            select(EnrollmentCode)
            .where(EnrollmentCode.code_hash == hash_secret(normalize_code(body.enrollment_code)))
            .with_for_update()
        )
    ).scalar_one_or_none()
    existing = (
        await session.execute(select(Node).where(Node.public_key == public_key))
    ).scalar_one_or_none()
    rejected = HTTPException(status.HTTP_403_FORBIDDEN, "invalid, used or expired enrollment code")
    if code is None:
        raise rejected
    if code.used_at is not None:
        # A retry after a lost response: the same code with the same key gets the
        # node it already created, so a node never ends up stranded.
        if existing is not None and code.used_by_node_id == existing.id:
            return _enroll_response(request, existing.id)
        raise rejected
    if code.expires_at <= datetime.now(UTC):
        raise rejected
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "public key already enrolled")

    ip = client_ip(request, settings)
    node = Node(
        name=body.name,
        public_key=public_key,
        trust_level=code.trust_level,
        capabilities=body.capabilities,
        version=body.version,
        egress_ip=ip,
        egress_group=egress_group_for(ip),
    )
    session.add(node)
    await session.flush()
    code.used_at = func.now()
    code.used_by_node_id = node.id
    await session.commit()
    return _enroll_response(request, node.id)


def _enroll_response(request: Request, node_id) -> api.EnrollResponse:
    return api.EnrollResponse(
        node_id=node_id,
        coordinator_api_version=API_VERSION,
        signing_public_key=signing.public_key_b64(request.app.state.signing_key),
    )


@router.post("/challenge", response_model=api.ChallengeResponse)
async def challenge(body: api.ChallengeRequest, session: Session, settings: AppSettings):
    node = await session.get(Node, body.node_id)
    # Same answer for unknown and revoked nodes; nothing to learn from probing.
    if node is None or node.revoked_at is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "node not enrolled")
    nonce = b64u_encode(secrets.token_bytes(32))
    expires = datetime.now(UTC) + timedelta(seconds=settings.challenge_ttl_s)
    await session.execute(delete(NodeChallenge).where(NodeChallenge.expires_at < func.now()))
    session.add(NodeChallenge(nonce_hash=hash_secret(nonce), node_id=node.id, expires_at=expires))
    await session.commit()
    return api.ChallengeResponse(nonce=nonce, expires_at=expires)


@router.post("/token", response_model=api.TokenResponse)
async def token(body: api.TokenRequest, session: Session, settings: AppSettings):
    ch = (
        await session.execute(
            delete(NodeChallenge)
            .where(
                NodeChallenge.nonce_hash == hash_secret(body.nonce),
                NodeChallenge.node_id == body.node_id,
            )
            .returning(NodeChallenge.expires_at)
        )
    ).scalar_one_or_none()  # single use: deleted whether or not the signature checks out
    node = await session.get(Node, body.node_id)
    ok = (
        ch is not None
        and ch > datetime.now(UTC)
        and node is not None
        and node.revoked_at is None
        and verify_signature(node.public_key, body.nonce.encode(), body.signature)
    )
    if not ok:
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "challenge failed")
    tok = new_token()
    expires = datetime.now(UTC) + timedelta(seconds=settings.access_token_ttl_s)
    await session.execute(
        delete(NodeToken).where(NodeToken.node_id == node.id, NodeToken.expires_at < func.now())
    )
    session.add(NodeToken(token_hash=hash_secret(tok), node_id=node.id, expires_at=expires))
    await session.commit()
    return api.TokenResponse(access_token=tok, expires_at=expires)


@router.get("/signing-key")
async def signing_key(request: Request, node: CurrentNode):
    """For nodes enrolled before keys were handed out at enrollment: the coordinator's
    signing key over the node's authenticated channel."""
    return {"public_key": signing.public_key_b64(request.app.state.signing_key)}


def desired_state(node: Node) -> api.DesiredState:
    return api.DesiredState(
        run_state=api.RunState(node.desired_run_state),
        target_version=node.desired_target_version,
        limits=api.NodeLimits(**(node.desired_limits or {})),
        job_types=node.desired_job_types,
    )


@router.post("/heartbeat", response_model=api.HeartbeatResponse)
async def heartbeat(
    body: api.Heartbeat,
    request: Request,
    node: CurrentNode,
    session: Session,
    settings: AppSettings,
):
    ip = client_ip(request, settings)
    node.version = body.version
    node.actual_state = body.state.value
    node.resources = body.resources.model_dump()
    node.concurrency = {
        **body.concurrency.model_dump(),
        "active_jobs": body.active_jobs,
        "spooled_results": body.spooled_results,
    }
    # what the agent reports now: a node without one (simple mode, or switched to it)
    # no longer follows release targets, and is not counted as agent-managed
    node.agent_status = body.agent.model_dump(mode="json") if body.agent is not None else None
    node.last_heartbeat_at = func.now()
    if ip is not None:
        # the group follows the address and the grouping rules of this version (a node
        # enrolled before a rule changed is regrouped by its next heartbeat)
        group = egress_group_for(ip)
        known = str(node.egress_ip) if node.egress_ip is not None else None  # INET
        if ip != known or group != node.egress_group:
            node.egress_ip = ip
            node.egress_group = group

    for res in body.command_results:
        await session.execute(
            update(NodeCommand)
            .where(
                NodeCommand.id == res.id,
                NodeCommand.node_id == node.id,
                NodeCommand.acked_at.is_(None),
            )
            .values(acked_at=func.now(), ok=res.ok, result={"output": res.output})
        )

    pending = (
        (
            await session.execute(
                select(NodeCommand)
                .where(NodeCommand.node_id == node.id, NodeCommand.acked_at.is_(None))
                .order_by(NodeCommand.id)
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    for cmd in pending:
        if cmd.delivered_at is None:
            cmd.delivered_at = func.now()

    interval = await runtime_settings.get(session, "heartbeat_interval_s")
    response = api.HeartbeatResponse(
        desired=desired_state(node),
        commands=[api.Command(id=c.id, type=c.type, args=c.args) for c in pending],
        coordinator_api_version=API_VERSION,
        server_time=datetime.now(UTC),
        heartbeat_interval_s=interval,
    )
    await session.commit()
    return response
