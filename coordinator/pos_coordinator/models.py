"""ORM models.

Two layers (see docs/spec.md, "Database schema and storage"):

* Raw / operational layer: `source`, `domain_state`, `job`, `node*`, `fetch`,
  `llm_call`, `setting`, `audit_log`. The `fetch` table is the immutable raw
  evidence and its schema should practically never change.
* Derived layer: `product`, `offer`, `measurement`, `classification`,
  `embedding`, `parser`, `gold_label`, `taxonomy_node`. Rebuilt from raw by
  re-running stages, so these drafts can change freely.

Statuses are text columns with CHECK constraints rather than PG enums, so new
values can be added in an expand-only migration.

The spec's `worker` table is called `node` here: a node runs the worker, and
may later also run other roles.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pos_shared.ids import uuid7

TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, datetime: TS, uuid.UUID: UUID(as_uuid=True)}


def pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)


def created() -> Mapped[datetime]:
    return mapped_column(TS, server_default=func.now(), nullable=False)


# =========================================================================
# Raw / operational layer
# =========================================================================


class Source(Base):
    __tablename__ = "source"
    __table_args__ = (
        CheckConstraint("type IN ('dataset','api','scraper','reference')", name="source_type"),
        CheckConstraint("status IN ('active','paused','disabled')", name="source_status"),
    )
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(Text, unique=True)
    type: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, server_default="active")
    created_at: Mapped[datetime] = created()


class DomainState(Base):
    """Per-domain state shared by all egress groups."""

    __tablename__ = "domain_state"
    __table_args__ = (
        CheckConstraint("fetch_mode IN ('http','browser')", name="domain_fetch_mode"),
        CheckConstraint("block_status IN ('ok','throttled','blocked')", name="domain_block_status"),
    )
    domain: Mapped[str] = mapped_column(Text, primary_key=True)
    fetch_mode: Mapped[str] = mapped_column(Text, server_default="http")
    block_status: Mapped[str] = mapped_column(Text, server_default="ok")
    block_reason: Mapped[str | None] = mapped_column(Text)
    blocked_at: Mapped[datetime | None]
    # Last unblock (by hand or automatic retry): leases carry it, so a worker drops a
    # local refusal pause from before it (see pos_worker.fetch.breaker).
    unblocked_at: Mapped[datetime | None]
    robots_txt: Mapped[str | None] = mapped_column(Text)
    robots_fetched_at: Mapped[datetime | None]
    # rules | none (4xx: allow all) | unreachable (5xx/network: disallow until refetched)
    robots_status: Mapped[str | None] = mapped_column(Text)
    robots_http_status: Mapped[int | None] = mapped_column(SmallInteger)
    robots_crawl_delay_s: Mapped[float | None] = mapped_column(Float)
    robots_next_check_at: Mapped[datetime | None]
    sitemaps: Mapped[list[str] | None] = mapped_column(JSONB)  # advertised in robots.txt
    # sources whose discovery waits for this domain's robots.txt (its Sitemap: lines)
    pending_discovery: Mapped[list[str] | None] = mapped_column(JSONB)
    # Slot schedule for the optional hard cap across all egress groups.
    next_global_slot_at: Mapped[datetime | None]
    pages_fetched: Mapped[int] = mapped_column(BigInteger, server_default="0")
    last_fetch_at: Mapped[datetime | None]
    overrides: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = created()


class DomainEgressState(Base):
    """Rate and lease per (domain, egress group): the adaptive back-off state."""

    __tablename__ = "domain_egress_state"
    domain: Mapped[str] = mapped_column(
        Text, ForeignKey("domain_state.domain", ondelete="CASCADE"), primary_key=True
    )
    egress_group: Mapped[str] = mapped_column(Text, primary_key=True)
    rate_rps: Mapped[float] = mapped_column(Float, server_default="0.2")
    lease_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node.id"))
    lease_expires_at: Mapped[datetime | None]
    cooldown_until: Mapped[datetime | None]
    clean_streak: Mapped[int] = mapped_column(Integer, server_default="0")
    throttle_count: Mapped[int] = mapped_column(Integer, server_default="0")
    # When this group may send the next request to the domain (rate slot schedule).
    next_slot_at: Mapped[datetime | None]
    latency_ewma_ms: Mapped[float | None] = mapped_column(Float)
    latency_base_ms: Mapped[float | None] = mapped_column(Float)
    consecutive_errors: Mapped[int] = mapped_column(Integer, server_default="0")
    consecutive_403: Mapped[int] = mapped_column(Integer, server_default="0")
    consecutive_blocks: Mapped[int] = mapped_column(Integer, server_default="0")
    updated_at: Mapped[datetime] = created()


class DomainEvent(Base):
    """Every rate change, cooldown, block and robots.txt update, for the panel and
    for measuring source-specific bias later."""

    __tablename__ = "domain_event"
    __table_args__ = (Index("ix_domain_event_domain_at", "domain", "at"),)
    id: Mapped[uuid.UUID] = pk()
    domain: Mapped[str] = mapped_column(Text)
    egress_group: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    old_rate_rps: Mapped[float | None] = mapped_column(Float)
    new_rate_rps: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    at: Mapped[datetime] = created()


class Node(Base):
    __tablename__ = "node"
    __table_args__ = (
        CheckConstraint("trust_level IN ('owned','community')", name="node_trust_level"),
        CheckConstraint(
            "desired_run_state IN ('running','paused','draining')", name="node_desired"
        ),
    )
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(Text)
    public_key: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    trust_level: Mapped[str] = mapped_column(Text, server_default="owned")
    capabilities: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    version: Mapped[str | None] = mapped_column(Text)
    egress_ip: Mapped[str | None] = mapped_column(INET)
    egress_group: Mapped[str | None] = mapped_column(Text)
    actual_state: Mapped[str | None] = mapped_column(Text)
    resources: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    concurrency: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    desired_run_state: Mapped[str] = mapped_column(Text, server_default="running")
    desired_target_version: Mapped[str | None] = mapped_column(Text)
    desired_limits: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    desired_job_types: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    agent_status: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_heartbeat_at: Mapped[datetime | None]
    enrolled_at: Mapped[datetime] = created()
    revoked_at: Mapped[datetime | None]


class EnrollmentCode(Base):
    __tablename__ = "enrollment_code"
    id: Mapped[uuid.UUID] = pk()
    code_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    label: Mapped[str | None] = mapped_column(Text)
    trust_level: Mapped[str] = mapped_column(Text, server_default="owned")
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    used_by_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node.id"))
    created_at: Mapped[datetime] = created()


class NodeChallenge(Base):
    __tablename__ = "node_challenge"
    nonce_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("node.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime]


class NodeToken(Base):
    __tablename__ = "node_token"
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("node.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = created()


class NodeCommand(Base):
    """One-off command for a node. Delivered with heartbeats until acknowledged."""

    __tablename__ = "node_command"
    __table_args__ = (
        Index("ix_node_command_pending", "node_id", postgresql_where="acked_at IS NULL"),
    )
    id: Mapped[uuid.UUID] = pk()
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("node.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(Text)
    args: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created()
    delivered_at: Mapped[datetime | None]
    acked_at: Mapped[datetime | None]
    ok: Mapped[bool | None] = mapped_column(Boolean)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','leased','done','failed','cancelled')", name="job_status"
        ),
        Index(
            "ix_job_queue",
            "type",
            text("priority DESC"),
            "id",
            postgresql_where="status = 'queued'",
        ),
        Index("ix_job_lease_expiry", "lease_expires_at", postgresql_where="status = 'leased'"),
        Index("ix_job_status_type", "status", "type"),
        # The crawl scheduler picks queued jobs per domain.
        Index(
            "ix_job_domain_queue",
            "domain",
            text("priority DESC"),
            "id",
            postgresql_where="status = 'queued' AND domain IS NOT NULL",
        ),
        # The same URL is queued at most once at a time.
        Index(
            "uq_job_active_dedup",
            "dedup_key",
            unique=True,
            postgresql_where="status IN ('queued', 'leased') AND dedup_key IS NOT NULL",
        ),
    )
    id: Mapped[uuid.UUID] = pk()
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    priority: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    # Attempt whose result was committed (None: no result stored yet). Lets the queue
    # tell a replayed result from a late one, see queue.complete().
    result_attempt: Mapped[int | None] = mapped_column(SmallInteger)
    max_attempts: Mapped[int] = mapped_column(SmallInteger, server_default="5")
    not_before: Mapped[datetime] = created()
    # Holder of the latest attempt. Kept when a lease expires, so a late result
    # from that holder can still be accepted until the job is leased again.
    node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node.id"))
    lease_expires_at: Mapped[datetime | None]
    domain: Mapped[str | None] = mapped_column(Text)
    dedup_key: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("source.id"))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = created()
    finished_at: Mapped[datetime | None]


class RawFetch(Base):
    """Immutable raw fetch record. Range-partitioned by month on `fetched_at`.

    The primary key includes the partition key, as PostgreSQL requires.
    Derived tables refer to fetches by id without a foreign key.
    """

    __tablename__ = "raw_fetch"
    __table_args__ = (
        PrimaryKeyConstraint("id", "fetched_at"),
        Index("ix_raw_fetch_url", "url"),
        Index("ix_raw_fetch_received", "received_at", "id"),  # derive stage cursor
        {"postgresql_partition_by": "RANGE (fetched_at)"},
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    fetched_at: Mapped[datetime] = mapped_column(TS)
    url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[int | None] = mapped_column(SmallInteger)
    mode: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(Text)
    response_bytes: Mapped[int | None] = mapped_column(BigInteger)
    blob_sha256: Mapped[str | None] = mapped_column(Text)
    html_sha256: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    node_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    received_at: Mapped[datetime] = created()


class RawObject(Base):
    """Content-addressed blob on the raw store (NAS). Postgres keeps only the reference."""

    __tablename__ = "raw_object"
    sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(Text, server_default="snippets")
    uploaded_by_node_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created()


class LlmCall(Base):
    """Cost ledger and LLM response cache (keyed by input hash)."""

    __tablename__ = "llm_call"
    __table_args__ = (
        Index("ix_llm_call_created", "created_at"),
        Index("ix_llm_call_cache", "input_hash", "model"),
    )
    id: Mapped[uuid.UUID] = pk()
    model: Mapped[str] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    batch: Mapped[bool] = mapped_column(Boolean, server_default="false")
    input_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    input_hash: Mapped[str | None] = mapped_column(Text)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created()


class LlmBatch(Base):
    """A Message Batch submitted to the Claude API, until its results are collected.
    `requests` maps each custom_id to its input hash and the stage's metadata. The row
    is written (status submitting, no remote id yet) before the request, so its
    estimate is reserved in the budget from the start."""

    __tablename__ = "llm_batch"
    __table_args__ = (
        CheckConstraint(
            "status IN ('submitting','submitted','ended','failed')", name="llm_batch_status"
        ),
        Index("ix_llm_batch_status", "status"),
    )
    id: Mapped[uuid.UUID] = pk()
    remote_id: Mapped[str | None] = mapped_column(Text, unique=True)
    stage: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="submitted")
    request_count: Mapped[int] = mapped_column(Integer)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    requests: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created()
    ended_at: Mapped[datetime | None]


class Setting(Base):
    """Runtime switches, editable in the control panel. Defaults live in code."""

    __tablename__ = "setting"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = created()
    updated_by: Mapped[str | None] = mapped_column(Text)


class Release(Base):
    """A release the coordinator has approved and signed (images pinned by digest)."""

    __tablename__ = "release"
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    signature: Mapped[str] = mapped_column(Text)
    key_id: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created()


class AppUser(Base):
    """A person using the control panel."""

    __tablename__ = "app_user"
    __table_args__ = (CheckConstraint("role IN ('admin','viewer')", name="app_user_role"),)
    id: Mapped[uuid.UUID] = pk()
    username: Mapped[str] = mapped_column(Text, unique=True)  # lowercase
    password_hash: Mapped[str] = mapped_column(Text)  # Argon2id
    totp_secret: Mapped[str | None] = mapped_column(Text)  # base32, set once confirmed
    totp_pending_secret: Mapped[str | None] = mapped_column(Text)  # during setup
    totp_last_counter: Mapped[int | None] = mapped_column(BigInteger)  # no code reuse
    role: Mapped[str] = mapped_column(Text, server_default="viewer")
    disabled_at: Mapped[datetime | None]
    password_changed_at: Mapped[datetime] = created()
    last_login_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created()


class UserSession(Base):
    __tablename__ = "user_session"
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), index=True
    )
    csrf_token: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created()
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime] = created()
    reauth_at: Mapped[datetime] = created()
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)


class LoginAttempt(Base):
    """Failed and successful logins, for rate limiting and the audit trail."""

    __tablename__ = "login_attempt"
    __table_args__ = (
        Index("ix_login_attempt_user_at", "username", "at"),
        Index("ix_login_attempt_ip_at", "ip", "at"),
    )
    id: Mapped[uuid.UUID] = pk()
    username: Mapped[str] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)
    ok: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = created()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[uuid.UUID] = pk()
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created()


# =========================================================================
# Derived layer (draft; rebuilt from raw, not migrated in place)
# =========================================================================


class TaxonomyNode(Base):
    """A category of the target taxonomy (see classify/taxonomy.py). `path` holds the
    English names; `names` other locales ({"de": {"name", "path"}})."""

    __tablename__ = "taxonomy_node"
    __table_args__ = (
        UniqueConstraint("taxonomy", "external_id", name="uq_taxonomy_node_external"),
    )
    id: Mapped[uuid.UUID] = pk()
    taxonomy: Mapped[str] = mapped_column(Text, server_default="google")
    external_id: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("taxonomy_node.id"))
    name: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, index=True)
    depth: Mapped[int] = mapped_column(SmallInteger)
    is_custom: Mapped[bool] = mapped_column(Boolean, server_default="false")
    names: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    version: Mapped[str | None] = mapped_column(Text)  # of the taxonomy that last wrote it


class CategoryMapping(Base):
    """A shop's category path (breadcrumbs) -> a taxonomy node, made once per shop and
    path. Curated, not derived: it survives a rebuild of the derived layer. A changed
    mapping is a new row; the old one stays with is_current = false. A null node means
    the path was looked at and has no fitting category ("unmappable")."""

    __tablename__ = "category_mapping"
    __table_args__ = (
        CheckConstraint(
            "method IN ('manual','llm','rule','import')", name="category_mapping_method"
        ),
        Index(
            "uq_category_mapping_current",
            "source",
            "category_path",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )
    id: Mapped[uuid.UUID] = pk()
    source: Mapped[str] = mapped_column(Text)
    category_path: Mapped[str] = mapped_column(Text)  # segments joined with " > "
    taxonomy_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("taxonomy_node.id"))
    method: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_by: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = created()


class FxRate(Base):
    """ECB euro reference rates: 1 EUR = rate_per_eur units of currency, per day."""

    __tablename__ = "fx_rate"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    currency: Mapped[str] = mapped_column(Text, primary_key=True)
    rate_per_eur: Mapped[Decimal] = mapped_column(Numeric(18, 8))


class DeriveCursor(Base):
    """How far a derive stage got through the raw layer (per stage version)."""

    __tablename__ = "derive_cursor"
    stage: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text)
    last_received_at: Mapped[datetime | None]
    last_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    processed: Mapped[int] = mapped_column(BigInteger, server_default="0")
    errors: Mapped[int] = mapped_column(BigInteger, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = created()


class Product(Base):
    __tablename__ = "product"
    __table_args__ = (UniqueConstraint("match_key", name="uq_product_match_key"),)
    id: Mapped[uuid.UUID] = pk()
    # gtin:<gtin> | bm:<brand>|<mpn> | url:<normalized url>; how offers are grouped
    match_key: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    gtin: Mapped[str | None] = mapped_column(Text, index=True)
    brand: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    canonical_price_eur: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("taxonomy_node.id"))
    dimensionality: Mapped[str | None] = mapped_column(Text)  # 3d_object | 2d_area | bulk_unit
    attributes: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    match_method: Mapped[str | None] = mapped_column(Text)
    match_confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = created()


class Offer(Base):
    __tablename__ = "offer"
    __table_args__ = (
        # offers waiting for an exchange rate (backfilled when the ECB rates arrive)
        Index(
            "ix_offer_no_fx_rate",
            "id",
            postgresql_where=text("flags @> ARRAY['no_fx_rate']::text[]"),
        ),
    )
    id: Mapped[uuid.UUID] = pk()
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("product.id"), index=True)
    fetch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    source: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    observed_on: Mapped[datetime]
    raw_price: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(Text)
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    price_eur: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    vat: Mapped[str | None] = mapped_column(Text)  # gross | net | unknown
    availability: Mapped[str | None] = mapped_column(Text)
    condition: Mapped[str | None] = mapped_column(Text)
    pack_size: Mapped[float | None] = mapped_column(Float)
    flags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    fields: Mapped[dict[str, Any]] = mapped_column(
        server_default=text("'{}'::jsonb"),
        comment="Per-field provenance: method, confidence, parser_version",
    )
    parser_version: Mapped[str | None] = mapped_column(Text)
    validator_version: Mapped[str | None] = mapped_column(Text)


class Measurement(Base):
    __tablename__ = "measurement"
    __table_args__ = (
        CheckConstraint("basis IN ('product','package','unknown')", name="measurement_basis"),
        CheckConstraint("a_m IS NULL OR b_m IS NULL OR a_m >= b_m", name="measurement_a_ge_b"),
        CheckConstraint("b_m IS NULL OR c_m IS NULL OR b_m >= c_m", name="measurement_b_ge_c"),
    )
    id: Mapped[uuid.UUID] = pk()
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("product.id"), index=True)
    offer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("offer.id"))
    fetch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    basis: Mapped[str] = mapped_column(Text, server_default="unknown")
    basis_method: Mapped[str | None] = mapped_column(Text)
    basis_confidence: Mapped[float | None] = mapped_column(Float)
    a_m: Mapped[float | None] = mapped_column(Float)
    b_m: Mapped[float | None] = mapped_column(Float)
    c_m: Mapped[float | None] = mapped_column(Float)
    mass_kg: Mapped[float | None] = mapped_column(Float)
    net_or_gross: Mapped[str | None] = mapped_column(Text)
    true_volume_m3: Mapped[float | None] = mapped_column(Float)
    area_m2: Mapped[float | None] = mapped_column(Float)
    raw: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    method: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    parser_version: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")


class Classification(Base):
    __tablename__ = "classification"
    id: Mapped[uuid.UUID] = pk()
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product.id"), index=True)
    taxonomy_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("taxonomy_node.id"))
    tier: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    classifier_version: Mapped[str] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = created()


class Embedding(Base):
    __tablename__ = "embedding"
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product.id"), primary_key=True)
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    vector: Mapped[Any] = mapped_column(Vector())
    created_at: Mapped[datetime] = created()


class Parser(Base):
    __tablename__ = "parser"
    id: Mapped[uuid.UUID] = pk()
    domain: Mapped[str] = mapped_column(Text, index=True)
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text, server_default="declarative")
    artifact: Mapped[dict[str, Any]] = mapped_column(JSONB)
    signature: Mapped[str | None] = mapped_column(Text)
    validation_score: Mapped[float | None] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created()


class GoldLabel(Base):
    __tablename__ = "gold_label"
    id: Mapped[uuid.UUID] = pk()
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("product.id"))
    offer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("offer.id"))
    fetch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB)
    labelled_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created()
