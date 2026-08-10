"""Control-plane system of record (ARCHITECTURE.md §8).

SQLite for dev/tests; Cloud SQL Postgres in deployment. Row-level security is a
Postgres migration (Slice 1 issue) — every table already carries tenant_id so
RLS bolts on without schema change. Secrets NEVER live here; `connections`
stores only references into the brand project's Secret Manager.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, Date, Boolean, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, relationship


def _id() -> str:
    return uuid.uuid4().hex


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(120))
    hosting_tier: Mapped[str] = mapped_column(String(16), default="shared")  # shared|dedicated
    gcp_project: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)


class Brand(Base):
    __tablename__ = "brands"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")

    properties: Mapped[list[BrandProperty]] = relationship(
        "BrandProperty", back_populates="brand", cascade="all, delete-orphan",
        lazy="selectin"
    )

    cadences: Mapped[list[Cadence]] = relationship(
        "Cadence", back_populates="brand", cascade="all, delete-orphan",
        lazy="selectin"
    )

    objective_association: Mapped[BrandObjective | None] = relationship(
        "BrandObjective", back_populates="brand", cascade="all, delete-orphan",
        lazy="selectin", uselist=False
    )


class BrandProperty(Base):
    __tablename__ = "brand_properties"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    brand_id: Mapped[str] = mapped_column(ForeignKey("brands.id"), index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    connection_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="absent")
    last_checked: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    findings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(default=_now, onupdate=_now)

    brand: Mapped[Brand] = relationship("Brand", back_populates="properties")
    tenant: Mapped[Tenant] = relationship("Tenant")


class Cadence(Base):
    __tablename__ = "cadences"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    brand_id: Mapped[str] = mapped_column(ForeignKey("brands.id"), index=True)
    domain: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(120))
    schedule: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="on_track")
    last_run: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    next_run: Mapped[dt.datetime] = mapped_column()
    last_finding_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(default=_now, onupdate=_now)

    brand: Mapped[Brand] = relationship("Brand", back_populates="cadences")
    tenant: Mapped[Tenant] = relationship("Tenant")


class OpRow(Base):
    __tablename__ = "ops"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    brand_id: Mapped[str] = mapped_column(String(32), index=True)
    domain: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(120))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(24), index=True)
    impact: Mapped[int] = mapped_column(Integer)
    reversibility: Mapped[str] = mapped_column(String(16))
    statutory: Mapped[bool] = mapped_column(default=False)
    cost_amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    preview_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_op_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sequence_order: Mapped[int] = mapped_column(Integer, default=0)
    idem_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(default=_now, onupdate=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")


class OpTrace(Base):
    """Execution trace (§4.5): every gate, call, retry, with reasons."""
    __tablename__ = "op_traces"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    op_id: Mapped[str] = mapped_column(String(32), index=True)
    ts: Mapped[dt.datetime] = mapped_column(default=_now)
    kind: Mapped[str] = mapped_column(String(40))  # transition|gate|adapter_call|retry|note
    detail: Mapped[dict] = mapped_column(JSON, default=dict)

    tenant: Mapped[Tenant] = relationship("Tenant")


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    op_id: Mapped[str] = mapped_column(String(32), index=True)
    actor: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(60))
    surface: Mapped[str] = mapped_column(String(24))  # whatsapp|web|auto
    decision: Mapped[str] = mapped_column(String(16))  # approve|reject|modify
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ts: Mapped[dt.datetime] = mapped_column(default=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")


class AuditEvent(Base):
    """Append-only, hash-chained (§4.5). Never UPDATE, never DELETE."""
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(40))  # iso8601, part of the hash preimage
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(120))
    op_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)

    tenant: Mapped[Tenant] = relationship("Tenant")


class TrustEvent(Base):
    __tablename__ = "trust_events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    brand_id: Mapped[str] = mapped_column(String(32), index=True)
    domain: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(40))  # verified_success|override|verify_failure|rejection
    base_delta: Mapped[float] = mapped_column()
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # override reasons are gold (§4.4)
    ts: Mapped[dt.datetime] = mapped_column(default=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")


class TrustSnapshot(Base):
    __tablename__ = "trust_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    brand_id: Mapped[str] = mapped_column(String(32), index=True)
    domain: Mapped[str] = mapped_column(String(16))
    score: Mapped[float] = mapped_column()
    tier: Mapped[int] = mapped_column(Integer)
    ts: Mapped[dt.datetime] = mapped_column(default=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")


class CostEntry(Base):
    """Per-Op cost attribution from day one (§2.6)."""
    __tablename__ = "cost_ledger"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    op_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    actor: Mapped[str | None] = mapped_column(String(120), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))  # llm_tokens|api_call|gcp_resource
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[dt.datetime] = mapped_column(default=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")


class OutboxItem(Base):
    """Transactional outbox (§4.2). Written in the same txn as APPROVED."""
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    op_id: Mapped[str] = mapped_column(String(32), unique=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)  # PENDING|IN_FLIGHT|DONE|DEAD
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[dt.datetime] = mapped_column(default=_now)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")


class Connection(Base):
    """Manage-pillar credential metadata. credential points into the BRAND
    project's Secret Manager — the secret itself never touches this DB (§3)."""
    __tablename__ = "connections"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    brand_id: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    scope: Mapped[str] = mapped_column(String(128), default="read")  # e.g. read|write or comma-separated scopes
    credential: Mapped[str] = mapped_column("secret_ref", String(255), nullable=True)  # Mapped to physical column 'secret_ref' in DB, nullable to support revocation
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="unverified")  # unverified|active|error|revoked|degraded
    last_verified_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")

class OpDependency(Base):
    """Stores dependency edges for DAG-based sagas [L4]."""
    __tablename__ = "op_dependencies"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    parent_op_id: Mapped[str] = mapped_column(String(32), ForeignKey("ops.id"), primary_key=True)
    from_op_id: Mapped[str] = mapped_column(String(32), ForeignKey("ops.id"), primary_key=True)
    to_op_id: Mapped[str] = mapped_column(String(32), ForeignKey("ops.id"), primary_key=True)

    tenant: Mapped[Tenant] = relationship("Tenant")


class PolicyVersion(Base):
    """Dynamic versioned policy configurations [L5]."""
    __tablename__ = "policy_versions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # active|proposed|superseded
    params: Mapped[dict] = mapped_column(JSON, default=dict)  # serialized RulesetParams
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(default=_now, onupdate=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")


class ProcessedWebhookMessage(Base):
    __tablename__ = "processed_webhook_messages"
    message_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    processed_at: Mapped[dt.datetime] = mapped_column(default=_now)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    brand_id: Mapped[str] = mapped_column(String(32), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    attributed_campaign_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    placed_at: Mapped[dt.datetime] = mapped_column(default=_now)
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")


class OrderLine(Base):
    __tablename__ = "order_lines"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    unit_price_minor: Mapped[int] = mapped_column(Integer)
    line_discount_minor: Mapped[int] = mapped_column(Integer, default=0)
    qty: Mapped[int] = mapped_column(Integer, default=1)
    unit_cost_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")


class Refund(Base):
    __tablename__ = "refunds"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    order_line_id: Mapped[str] = mapped_column(ForeignKey("order_lines.id"), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")


class FulfillmentCost(Base):
    __tablename__ = "fulfillment_costs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    shipping_cost_minor: Mapped[int] = mapped_column(Integer, default=0)
    marketplace_fee_minor: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    brand_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(120))
    platform: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")


class SpendFact(Base):
    __tablename__ = "spend_facts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    date: Mapped[dt.date] = mapped_column(Date)
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")


class Touchpoint(Base):
    __tablename__ = "touchpoints"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(16))  # click|impression
    occurred_at: Mapped[dt.datetime] = mapped_column(default=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")


Index("ix_ops_tenant_state", OpRow.tenant_id, OpRow.state)


class CircuitBreakerRow(Base):
    __tablename__ = "circuit_breakers"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True)
    brand_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    domain: Mapped[str] = mapped_column(String(32), primary_key=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(16), default="CLOSED")  # CLOSED|OPEN
    tripped_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    last_failure_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    tenant: Mapped[Tenant] = relationship("Tenant")


class ConsentBasis(Base):
    __tablename__ = "consent_bases"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    category: Mapped[str] = mapped_column(String(32))  # pii_upload | vendor_sharing
    action_or_vendor: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(16), default="granted")  # granted | revoked
    granted_at: Mapped[dt.datetime] = mapped_column(default=_now)
    expires_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    granted_by: Mapped[str] = mapped_column(String(64))

    tenant: Mapped[Tenant] = relationship("Tenant")


class ShadowDecision(Base):
    __tablename__ = "shadow_decisions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    op_id: Mapped[str] = mapped_column(String(32), index=True)
    human_decision: Mapped[str] = mapped_column(String(16))  # approve|reject|modify
    shadow_tier: Mapped[int] = mapped_column(Integer, default=2)
    shadow_requirement: Mapped[str | None] = mapped_column(String(60), nullable=True)
    agreed: Mapped[bool] = mapped_column(default=True)
    violations: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[dt.datetime] = mapped_column(default=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")


class BrandObjective(Base):
    __tablename__ = "brand_objectives"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    brand_id: Mapped[str] = mapped_column(ForeignKey("brands.id"), index=True)
    objective: Mapped[str] = mapped_column(String(32), nullable=False) # footprint | growth | retention
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(default=_now, onupdate=_now)

    tenant: Mapped[Tenant] = relationship("Tenant")

    brand: Mapped[Brand] = relationship("Brand", back_populates="objective_association")


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    brand_id: Mapped[str] = mapped_column(String(32), index=True)
    lead_id: Mapped[str] = mapped_column(String(64), unique=True) # HubSpot/Salesforce Deal ID
    email_hashed: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32)) # lead | mql | sql | closed_won
    deal_value_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gclid: Mapped[str | None] = mapped_column(String(120), nullable=True)
    placed_at: Mapped[dt.datetime] = mapped_column(default=_now) # when the stage updated
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)


def make_engine(url: str = "sqlite:///./agencyos.db"):
    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool
        kwargs = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool  # one shared in-memory DB across connections
        return create_engine(url, future=True, **kwargs)
    return create_engine(url, future=True)


def make_session_factory(engine):
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False, future=True)
