"""Wire schemas for the worker <-> coordinator API (`/api/v1`).

Everything a node sends is validated strictly (extra fields rejected), because
authentication proves who sent data, not that it is well formed.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- enrollment and auth -------------------------------------------------


class EnrollRequest(Strict):
    enrollment_code: str = Field(min_length=8, max_length=128)
    public_key: str = Field(description="Ed25519 public key, raw 32 bytes, base64url")
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(max_length=50)
    capabilities: list[str] = Field(default_factory=list, max_length=50)


class EnrollResponse(Strict):
    node_id: UUID
    coordinator_api_version: str
    signing_public_key: str = Field(
        description="Coordinator's Ed25519 signing key (base64url); nodes pin it"
    )


class ChallengeRequest(Strict):
    node_id: UUID


class ChallengeResponse(Strict):
    nonce: str
    expires_at: datetime


class TokenRequest(Strict):
    node_id: UUID
    nonce: str
    signature: str = Field(description="Ed25519 signature over the nonce bytes, base64url")


class TokenResponse(Strict):
    access_token: str
    expires_at: datetime


# --- releases --------------------------------------------------------------

VERSION_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$"
# A digest-pinned image reference: name[:tag]@sha256:<64 hex>
IMAGE_REF_PATTERN = r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$"


class ReleaseManifest(Strict):
    """What a release consists of, by immutable image digest."""

    version: str = Field(pattern=VERSION_PATTERN)
    images: dict[str, str] = Field(
        description="component (worker, agent, coordinator) -> digest-pinned image reference"
    )
    created_at: datetime

    @field_validator("images")
    @classmethod
    def _digest_pinned(cls, v: dict[str, str]) -> dict[str, str]:
        import re

        for component, ref in v.items():
            if not re.fullmatch(r"[a-z_]{1,30}", component):
                raise ValueError(f"bad component name {component!r}")
            if not re.fullmatch(IMAGE_REF_PATTERN, ref):
                raise ValueError(f"image for {component} must be pinned by digest: {ref!r}")
        return v


class SignedManifest(Strict):
    manifest: ReleaseManifest
    signature: str
    key_id: str


# --- heartbeat and control channel ---------------------------------------


class RunState(StrEnum):
    running = "running"
    paused = "paused"
    draining = "draining"


class Resources(Strict):
    cpu_limit: float | None = Field(None, description="Usable cores from the cgroup quota")
    mem_limit_bytes: int | None = None
    mem_used_bytes: int | None = None
    cpu_load: float | None = None
    disk_free_bytes: int | None = None


class Concurrency(Strict):
    http_slots: int = 0
    browser_slots: int = 0
    embed_batch: int = 0


class CommandResult(Strict):
    id: UUID
    ok: bool
    output: Any = None


class AgentStatus(Strict):
    """What the node agent (A/B updater) last reported through the shared volume."""

    agent_version: str | None = Field(None, max_length=50)
    active_slot: str | None = Field(None, max_length=5)
    running_version: str | None = Field(None, max_length=50)
    update_state: str | None = Field(None, max_length=30)  # idle | updating | failed | ...
    update_target: str | None = Field(None, max_length=50)
    update_error: str | None = Field(None, max_length=2000)
    updated_at: datetime | None = None


class Heartbeat(Strict):
    version: str
    state: RunState
    agent: AgentStatus | None = None
    resources: Resources = Field(default_factory=Resources)
    concurrency: Concurrency = Field(default_factory=Concurrency)
    active_jobs: int = 0
    spooled_results: int = 0
    command_results: list[CommandResult] = Field(default_factory=list, max_length=100)


class NodeLimits(Strict):
    max_http_slots: int | None = None
    max_browser_slots: int | None = None


class DesiredState(Strict):
    run_state: RunState = RunState.running
    target_version: str | None = None
    limits: NodeLimits = Field(default_factory=NodeLimits)
    job_types: list[str] | None = Field(
        None, description="Job types this node should pull; None means all it can run"
    )


class Command(Strict):
    id: UUID
    type: str
    args: dict[str, Any] = Field(default_factory=dict)


class HeartbeatResponse(Strict):
    desired: DesiredState
    commands: list[Command] = Field(default_factory=list)
    coordinator_api_version: str
    server_time: datetime
    heartbeat_interval_s: float = 15.0


# --- jobs ----------------------------------------------------------------


class LeaseRequest(Strict):
    job_types: list[str] = Field(min_length=1, max_length=50)
    max_jobs: int = Field(ge=1, le=500)


class LeasedJob(Strict):
    id: UUID
    type: str
    payload: dict[str, Any]
    attempt: int = Field(ge=1, description="Lease generation; results must quote it")
    lease_expires_at: datetime
    lease_seconds: int = Field(description="Lease duration; renew well before it runs out")


class LeaseResponse(Strict):
    jobs: list[LeasedJob]


class LeaseRef(Strict):
    job_id: UUID
    attempt: int = Field(ge=1)


class ExtendRequest(Strict):
    leases: list[LeaseRef] = Field(max_length=500)


class ExtendResponse(Strict):
    extended: list[UUID]
    lost: list[UUID] = Field(description="Jobs whose lease this node no longer holds")
    lease_seconds: int


class JobOutcome(StrEnum):
    done = "done"
    failed = "failed"


class FetchRecord(Strict):
    """One raw fetch. Immutable once stored; the raw layer's unit of evidence."""

    id: UUID
    url: str = Field(max_length=8192)
    final_url: str | None = Field(None, max_length=8192)
    status: int | None = None
    fetched_at: datetime
    mode: str = Field(max_length=20, description="http | browser | api | dataset")
    content_type: str | None = Field(None, max_length=200)
    response_bytes: int | None = None
    blob_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    html_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    content_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    source: str | None = Field(None, max_length=100)
    error: str | None = Field(None, max_length=2000)


class JobResult(Strict):
    job_id: UUID
    attempt: int = Field(ge=1, description="The lease attempt this result belongs to")
    outcome: JobOutcome
    output: dict[str, Any] | None = None
    error: str | None = Field(None, max_length=20000)
    retryable: bool = True
    fetches: list[FetchRecord] = Field(default_factory=list, max_length=1000)


class ResultBatch(Strict):
    results: list[JobResult] = Field(min_length=1, max_length=1000)


class ResultAck(Strict):
    accepted: list[UUID]
    rejected: dict[str, str] = Field(
        default_factory=dict, description="job_id -> reason; rejected results must not be resent"
    )
