from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Owner(Base):
    __tablename__ = "owners"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)


class ClientGrant(Base):
    __tablename__ = "client_grants"
    __table_args__ = (
        CheckConstraint("length(token_hash)=64"),
        CheckConstraint(
            "(renewed_at IS NULL OR renewed_at >= created_at) AND "
            "expires_at > COALESCE(renewed_at,created_at) AND "
            "expires_at <= COALESCE(renewed_at,created_at) + 604800"
        ),
        CheckConstraint("channel IN ('agent','human_adapter')"),
    )
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    client_id: Mapped[str] = mapped_column(String(100))
    scopes: Mapped[list] = mapped_column(JSON)
    audience: Mapped[str] = mapped_column(String(100))
    channel: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[int] = mapped_column(Integer)
    renewed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revoked_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ServiceState(Base):
    __tablename__ = "service_state"
    __table_args__ = (CheckConstraint("catalog_revision>=0 AND history_revision>=0"),)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), primary_key=True)
    catalog_revision: Mapped[int] = mapped_column(Integer, default=0)
    history_revision: Mapped[int] = mapped_column(Integer, default=0)


class Garment(Base):
    __tablename__ = "garments"
    __table_args__ = (
        CheckConstraint("revision>=1"),
        CheckConstraint("availability IN ('available','unavailable','unknown')"),
        CheckConstraint("state IN ('active','deleted')"),
        CheckConstraint(
            "annotation_review_state IN ('initial','needs_review','published')"
        ),
        Index("ix_garments_owner_state_id", "owner_id", "state", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    name: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    roles: Mapped[list] = mapped_column(JSON, default=list)
    availability: Mapped[str] = mapped_column(String(20), default="unknown")
    state: Mapped[str] = mapped_column(String(20), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    annotation_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    annotation_review_state: Mapped[str] = mapped_column(String(20), default="initial")
    identity_fields: Mapped[dict] = mapped_column(
        JSON, default=dict, server_default="{}"
    )
    product_reference: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer)
    deleted_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    restore_until: Mapped[int | None] = mapped_column(Integer, nullable=True)


class WardrobePreference(Base):
    __tablename__ = "wardrobe_preferences"
    __table_args__ = (
        CheckConstraint("revision>=1"),
        CheckConstraint("active_season IN ('spring_summer','autumn_winter')"),
    )
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), primary_key=True)
    active_season: Mapped[str] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(Integer)
    rules_version: Mapped[str] = mapped_column(String(80))
    rules_digest: Mapped[str] = mapped_column(String(64))
    initialized_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(Integer)


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (CheckConstraint("size>0 AND width>0 AND height>0"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    size: Mapped[int] = mapped_column(Integer)
    mime: Mapped[str] = mapped_column(String(50))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)


class ProductDraft(Base):
    __tablename__ = "product_drafts"
    __table_args__ = (
        CheckConstraint("revision>=1"),
        CheckConstraint("state IN ('draft','published')"),
        CheckConstraint("expires_at>created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    client_id: Mapped[str] = mapped_column(String(100))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(20), default="draft")
    payload: Mapped[dict] = mapped_column(JSON)
    published_garment_id: Mapped[str | None] = mapped_column(
        ForeignKey("garments.id"), nullable=True
    )
    created_at: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[int] = mapped_column(Integer)


class OwnerAsset(Base):
    __tablename__ = "owner_assets"
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), primary_key=True)


class GarmentAsset(Base):
    __tablename__ = "garment_assets"
    garment_id: Mapped[str] = mapped_column(ForeignKey("garments.id"), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), primary_key=True)


class Upload(Base):
    __tablename__ = "asset_uploads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    client_id: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"), nullable=True)


class Idempotency(Base):
    __tablename__ = "idempotency"
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    action: Mapped[str] = mapped_column(String(50), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)


class AnnotationVersion(Base):
    __tablename__ = "annotation_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    garment_id: Mapped[str] = mapped_column(ForeignKey("garments.id"))
    base_revision: Mapped[int] = mapped_column(Integer)
    fields: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[int] = mapped_column(Integer)


class ChangeProposal(Base):
    __tablename__ = "change_proposals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    created_by_client: Mapped[str] = mapped_column(String(100))
    changes: Mapped[list] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    nonce: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20))
    receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class TrustedIssuer(Base):
    __tablename__ = "trusted_issuers"
    issuer: Mapped[str] = mapped_column(String(100), primary_key=True)
    kid: Mapped[str] = mapped_column(String(100), primary_key=True)
    public_key: Mapped[str] = mapped_column(Text)
    bindings: Mapped[list] = mapped_column(JSON)
    purposes: Mapped[list] = mapped_column(JSON)
    audience: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[int] = mapped_column(Integer)
    revoked_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class HumanDecision(Base):
    __tablename__ = "human_decisions"
    __table_args__ = (UniqueConstraint("proposal_id"),)
    issuer: Mapped[str] = mapped_column(String(100), primary_key=True)
    jti: Mapped[str] = mapped_column(String(100), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("change_proposals.id"))
    assertion_digest: Mapped[str] = mapped_column(String(64))
    receipt: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(Integer)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    client_id: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[str] = mapped_column(String(36))
    receipt_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[int] = mapped_column(Integer)


class Recommendation(Base):
    __tablename__ = "recommendations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    client_id: Mapped[str] = mapped_column(String(100))
    grant_id: Mapped[str] = mapped_column(ForeignKey("client_grants.token_hash"))
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[int] = mapped_column(Integer)
    deadline: Mapped[int] = mapped_column(Integer)
    lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    wardrobe_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class HistoryEvent(Base):
    __tablename__ = "history_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    recommendation_id: Mapped[str] = mapped_column(ForeignKey("recommendations.id"))
    outfit_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(20))
    occurred_at: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(default=True)


class HistoryAmendment(Base):
    __tablename__ = "history_amendments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_id: Mapped[str] = mapped_column(ForeignKey("history_events.id"))
    proposal_id: Mapped[str] = mapped_column(ForeignKey("change_proposals.id"))
    before: Mapped[dict] = mapped_column(JSON)
    after: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(Integer)


class CatalogImport(Base):
    __tablename__ = "catalog_imports"
    __table_args__ = (UniqueConstraint("owner_id", "parent_batch_id"),)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_batch_id: Mapped[str] = mapped_column(String(36))
    receipt: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(Integer)


class CatalogSource(Base):
    __tablename__ = "catalog_sources"
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    garment_id: Mapped[str] = mapped_column(ForeignKey("garments.id"), unique=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"))
    original_sha256: Mapped[str] = mapped_column(String(64))
    batch_digest: Mapped[str] = mapped_column(String(64))
    source_metadata: Mapped[dict] = mapped_column(JSON)


class ProductSnapshot(Base):
    __tablename__ = "product_snapshots"
    __table_args__ = (
        UniqueConstraint("owner_id", "provider", "region", "global_spu_id", "digest"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    provider: Mapped[str] = mapped_column(String(30))
    region: Mapped[str] = mapped_column(String(2))
    global_spu_id: Mapped[str] = mapped_column(String(20))
    digest: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    recorded_at: Mapped[str] = mapped_column(String(60))


class GarmentProductEvidence(Base):
    __tablename__ = "garment_product_evidence"
    garment_id: Mapped[str] = mapped_column(ForeignKey("garments.id"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    evidence: Mapped[dict] = mapped_column(JSON)
    snapshot_ids: Mapped[list] = mapped_column(JSON)


class GarmentIntake(Base):
    __tablename__ = "garment_intakes"
    __table_args__ = (
        UniqueConstraint("owner_id", "client_id", "origin_scope"),
        CheckConstraint("revision>=1 AND image_revision>=0"),
        CheckConstraint(
            "state IN ('collecting','review','frozen','commit_pending','submitted','closed','expired')"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    client_id: Mapped[str] = mapped_column(String(100))
    origin_scope: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(30), default="collecting")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    image_revision: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    field_sources: Mapped[dict] = mapped_column(JSON, default=dict)
    frozen: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer)
    last_user_edit_at: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[int] = mapped_column(Integer)
    published_garment_id: Mapped[str | None] = mapped_column(
        ForeignKey("garments.id"), nullable=True, unique=True
    )
    receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class IntakeUpload(Base):
    __tablename__ = "intake_uploads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("garment_intakes.id"), index=True)
    image_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(10))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    metadata_json: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    expires_at: Mapped[int] = mapped_column(Integer)


class IntakeSourceObject(Base):
    __tablename__ = "intake_source_objects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("garment_intakes.id"))
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"))
    sha256: Mapped[str] = mapped_column(String(64))
    view_sha256: Mapped[str] = mapped_column(String(64))
    report: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20), default="draft")


class IntakeImage(Base):
    __tablename__ = "intake_images"
    __table_args__ = (UniqueConstraint("task_id", "ordinal"),)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("garment_intakes.id"), primary_key=True
    )
    image_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))
    source_kind: Mapped[str] = mapped_column(String(20))
    source_object_id: Mapped[str] = mapped_column(
        ForeignKey("intake_source_objects.id")
    )


class IntakeObservation(Base):
    __tablename__ = "intake_observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("garment_intakes.id"))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    producer: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)


class IntakeCommitAttempt(Base):
    __tablename__ = "intake_commit_attempts"
    jti: Mapped[str] = mapped_column(String(100), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("garment_intakes.id"), index=True)
    signed_request_hash: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(30))
    permit: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class IntakeDecision(Base):
    __tablename__ = "intake_decisions"
    issuer: Mapped[str] = mapped_column(String(100), primary_key=True)
    jti: Mapped[str] = mapped_column(String(100), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("garment_intakes.id"), unique=True)
    assertion_digest: Mapped[str] = mapped_column(String(64))
    receipt: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(Integer)


class IntakeGCJob(Base):
    __tablename__ = "intake_gc_jobs"
    source_id: Mapped[str] = mapped_column(
        ForeignKey("intake_source_objects.id"), primary_key=True
    )
    grace_until: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20))
    retries: Mapped[int] = mapped_column(Integer, default=0)


class GarmentIntakeOrigin(Base):
    __tablename__ = "garment_intake_origins"
    garment_id: Mapped[str] = mapped_column(ForeignKey("garments.id"), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("garment_intakes.id"), unique=True)
    sources: Mapped[list] = mapped_column(JSON)
    receipt: Mapped[dict] = mapped_column(JSON)


class IntakeProductEnrichment(Base):
    __tablename__ = "intake_product_enrichments"
    task_id: Mapped[str] = mapped_column(
        ForeignKey("garment_intakes.id"), primary_key=True
    )
    schema_version: Mapped[str] = mapped_column(String(60))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20))
    attempts_reserved: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    evidence_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[int] = mapped_column(Integer)
    reserved_bytes: Mapped[int] = mapped_column(Integer)


class AgentOperation(Base):
    __tablename__ = "agent_operations"
    __table_args__ = (
        UniqueConstraint("owner_id", "client_id", "kind", "request_key"),
        CheckConstraint("kind IN ('intake','recommendation')"),
        CheckConstraint(
            "state IN ('receiving','processing','needs_input','needs_confirmation','succeeded','failed','cancelled','unknown')"
        ),
        Index("ix_agent_operations_dispatch", "state", "lease_until"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    client_id: Mapped[str] = mapped_column(String(100))
    grant_id: Mapped[str] = mapped_column(ForeignKey("client_grants.token_hash"))
    kind: Mapped[str] = mapped_column(String(20))
    request_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("garment_intakes.id"), nullable=True
    )
    recommendation_id: Mapped[str | None] = mapped_column(
        ForeignKey("recommendations.id"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(30))
    stage: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict] = mapped_column(JSON)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[int] = mapped_column(Integer)


class AgentConfirmation(Base):
    __tablename__ = "agent_confirmations"
    __table_args__ = (
        UniqueConstraint("operation_id", "revision"),
        CheckConstraint("revision>=1"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("agent_operations.id"))
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[int] = mapped_column(Integer)
