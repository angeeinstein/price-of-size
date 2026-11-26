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
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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
    robots_txt: Mapped[str | None] = mapped_column(Text)
    robots_fetched_at: Mapped[datetime | None]
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
    updated_at: Mapped[datetime] = created()


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
    )
    id: Mapped[uuid.UUID] = pk()
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    priority: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    max_attempts: Mapped[int] = mapped_column(SmallInteger, server_default="5")
    not_before: Mapped[datetime] = created()
    node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node.id"))
    lease_expires_at: Mapped[datetime | None]
    domain: Mapped[str | None] = mapped_column(Text)
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


class Setting(Base):
    """Runtime switches, editable in the control panel. Defaults live in code."""

    __tablename__ = "setting"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = created()
    updated_by: Mapped[str | None] = mapped_column(Text)


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
    __tablename__ = "taxonomy_node"
    id: Mapped[uuid.UUID] = pk()
    taxonomy: Mapped[str] = mapped_column(Text, server_default="google")
    external_id: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("taxonomy_node.id"))
    name: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, index=True)
    depth: Mapped[int] = mapped_column(SmallInteger)
    is_custom: Mapped[bool] = mapped_column(Boolean, server_default="false")


class Product(Base):
    __tablename__ = "product"
    id: Mapped[uuid.UUID] = pk()
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
