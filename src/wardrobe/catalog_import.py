"""Operator-only first creation; no agent publication or existing garment edits."""

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import Field, ValidationError, model_validator
from sqlalchemy import func, select

from .auth import _unique_json
from .contracts import StrictModel, success
from .errors import BusinessError
from .labels import LabelValues
from .models import (
    Asset,
    AuditEvent,
    CatalogImport,
    CatalogSource,
    Garment,
    GarmentAsset,
    GarmentProductEvidence,
    Owner,
    OwnerAsset,
    ProductSnapshot,
    ServiceState,
)
from .writes import bump_catalog, canonical, digest

HASH = r"^[0-9a-f]{64}$"
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
MAX_BATCH = 10 * 1024 * 1024
MAX_SNAPSHOT = 2 * 1024 * 1024


class LabelField(StrictModel):
    value: Any
    source_kind: Literal[
        "unknown",
        "observed",
        "inferred",
        "source_filename",
        "product_api",
        "user_input",
    ]
    verification_state: Literal["unknown", "unverified", "user_verified"]
    evidence: list[dict] = Field(max_length=20)
    updated_at: int | None = Field(default=None, ge=0)


class HumanReview(StrictModel):
    source_id: str = Field(pattern=UUID_PATTERN)
    index: int = Field(ge=1, le=200)
    filename: str = Field(max_length=200)
    filename_brand: str | None = Field(default=None, max_length=100)
    status: Literal["unreviewed", "correct", "incorrect", "uncertain"]
    correct_product_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$")
    candidate_checks: dict[str, Literal["correct", "incorrect", "uncertain"]]
    note: str = Field(max_length=2000)
    entered_article_number: str = Field(max_length=100)
    updated_at: str | None = Field(default=None, max_length=60)


class WebSource(StrictModel):
    index: int = Field(ge=1, le=200)
    url: str = Field(max_length=2048)
    match: Literal[
        "exact_user_article",
        "article_family_only",
        "exact_article_brand_alias_unconfirmed",
        "exact_article_brand_conflict",
        "similar_unconfirmed",
    ]
    article: str | None = Field(default=None, max_length=100)
    brand: str = Field(min_length=1, max_length=100)
    title_zh: str = Field(min_length=1, max_length=300)
    reported: dict
    caveat: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def safe_url(self):
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Web evidence must use a public HTTPS source")
        return self


class ProductEvidence(StrictModel):
    link_state: Literal[
        "human_image_matched",
        "candidates_unreviewed",
        "user_code_web_linked_unverified",
        "web_candidates_unverified",
        "photo_only",
        "style_confirmed_color_unmatched",
        "rejected_search_abandoned",
    ]
    confirmed_global_spu_id: str | None = Field(
        default=None, pattern=r"^[1-9][0-9]{0,19}$"
    )
    candidate_global_spu_ids: list[str] = Field(max_length=50)
    rejected_global_spu_ids: list[str] = Field(max_length=50)
    style_confirmed_article_number: str | None = Field(default=None, max_length=100)
    owned_sku_id: None
    owned_size: None
    entered_article_number: str = Field(max_length=100)
    full_api_response_ids: list[str] = Field(max_length=50)
    web_sources: list[WebSource] = Field(max_length=20)
    human_review: HumanReview
    issues: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def match_consistency(self):
        candidates = self.candidate_global_spu_ids
        if len(set(candidates)) != len(candidates) or any(
            not re.fullmatch(r"[1-9][0-9]{0,19}", x) for x in candidates
        ):
            raise ValueError("Invalid product identifiers")
        review = self.human_review
        if self.full_api_response_ids != candidates or not set(
            self.rejected_global_spu_ids
        ) <= set(candidates):
            raise ValueError(
                "Snapshot and rejected candidates must belong to this item"
            )
        rejected = {
            spu
            for spu, state in review.candidate_checks.items()
            if state == "incorrect"
        }
        if set(self.rejected_global_spu_ids) != rejected or not set(
            review.candidate_checks
        ) <= set(candidates):
            raise ValueError("Candidate judgments cannot be lost")
        if self.entered_article_number != review.entered_article_number:
            raise ValueError("User article must be preserved")
        selected = self.confirmed_global_spu_id
        if self.link_state == "human_image_matched":
            if (
                not selected
                or selected not in candidates
                or review.status != "correct"
                or review.correct_product_id != selected
                or review.candidate_checks.get(selected) != "correct"
                or selected in rejected
            ):
                raise ValueError("Confirmed link needs an existing human image match")
        elif (
            selected is not None
            or review.correct_product_id is not None
            or review.status == "correct"
        ):
            raise ValueError("Unreviewed or partial matches cannot become confirmed")
        if self.link_state == "photo_only" and (candidates or self.web_sources):
            raise ValueError("Photo-only item cannot have product candidates")
        if self.link_state == "user_code_web_linked_unverified" and not any(
            w.match == "exact_user_article" and w.article == self.entered_article_number
            for w in self.web_sources
        ):
            raise ValueError("Web article must match the human-supplied article")
        return self


class ImportEntry(StrictModel):
    index: int = Field(ge=1, le=200)
    source_id: str = Field(pattern=UUID_PATTERN)
    proposed_garment_id: str = Field(pattern=UUID_PATTERN)
    original_asset_id: str = Field(pattern=UUID_PATTERN)
    original_sha256: str = Field(pattern=HASH)
    filename: str = Field(min_length=1, max_length=200)
    filename_brand: str | None = Field(default=None, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=100)
    roles: list[
        Literal[
            "upper",
            "lower",
            "footwear",
            "outerwear",
            "base_layer",
            "accessory",
            "one_piece",
        ]
    ] = Field(min_length=1, max_length=8)
    availability: Literal["unknown"]
    annotation_fields: dict[str, LabelField]
    annotation_review_state: Literal["initial"]
    product_evidence: ProductEvidence
    source_metadata: dict
    analysis_sha256: str = Field(pattern=HASH)

    @model_validator(mode="after")
    def evidence_consistency(self):
        if set(self.annotation_fields) != set(LabelValues.model_fields):
            raise ValueError("All annotation fields must be explicit")
        LabelValues.model_validate(
            {k: f.value for k, f in self.annotation_fields.items()}
        )
        review = self.product_evidence.human_review
        if (review.source_id, review.index, review.filename, review.filename_brand) != (
            self.source_id,
            self.index,
            self.filename,
            self.filename_brand,
        ):
            raise ValueError("Human review must belong to the same original")
        if self.annotation_fields["size"].value is not None:
            raise ValueError("Owned size is unknown for this first import")
        for key, field in self.annotation_fields.items():
            if field.value is None:
                if (
                    field.source_kind != "unknown"
                    or field.verification_state != "unknown"
                ):
                    raise ValueError("Null values must remain unknown")
                continue
            if (
                field.source_kind == "unknown"
                or field.verification_state == "unknown"
                or not field.evidence
            ):
                raise ValueError("Known labels need provenance")
            if field.verification_state == "user_verified" and (
                key != "brand"
                or field.source_kind != "user_input"
                or not any(
                    e.get("kind") == "human_brand_alias_confirmation"
                    and e.get("original") == self.filename_brand
                    and e.get("accepted") == field.value
                    for e in field.evidence
                )
            ):
                raise ValueError("Only explicit human brand alias evidence is verified")
            if field.source_kind == "observed" and (
                field.verification_state != "unverified"
                or not all(
                    e.get("asset_id") == self.original_asset_id
                    and e.get("analysis_sha256") == self.analysis_sha256
                    for e in field.evidence
                )
            ):
                raise ValueError(
                    "Photo labels must reference this original and remain unverified"
                )
            if field.source_kind in {"observed", "inferred"} and key in {
                "composition",
                "brand",
                "model",
                "size",
                "waterproof",
                "windproof",
                "fit_feedback",
                "preference",
            }:
                raise ValueError(
                    "Photos cannot substantiate exact identity or functional claims"
                )
            if field.source_kind == "source_filename" and (
                key != "brand" or field.value != self.filename_brand
            ):
                raise ValueError(
                    "Source brand must preserve the original filename hint"
                )
            if field.source_kind == "product_api" and not all(
                e.get("global_spu_id") == self.product_evidence.confirmed_global_spu_id
                and e.get("provider") == "manual"
                for e in field.evidence
            ):
                raise ValueError("Effective API labels need the confirmed product")
            if field.source_kind == "product_api" and key not in {"brand", "model"}:
                raise ValueError(
                    "This captured API does not substantiate other label fields"
                )
        if any(w.index != self.index for w in self.product_evidence.web_sources):
            raise ValueError("Web evidence belongs to another item")
        return self


class ImportBatch(StrictModel):
    schema_version: Literal["wardrobe-enriched-import-preview/1"]
    prepared_at: str = Field(min_length=1, max_length=60)
    owner_id: str = Field(pattern=UUID_PATTERN)
    published: Literal[False]
    production_write: Literal[False]
    parent_batch_id: str = Field(pattern=UUID_PATTERN)
    parent_batch_digest: str = Field(pattern=HASH)
    human_review_dataset_id: str = Field(min_length=1, max_length=100)
    summary: dict
    policy: dict
    entries: list[ImportEntry] = Field(min_length=1, max_length=200)
    api_snapshots: dict[str, dict]
    digest: str = Field(pattern=HASH)

    @model_validator(mode="after")
    def unique_and_bounded(self):
        for field in ["index", "source_id", "proposed_garment_id", "original_asset_id"]:
            values = [getattr(e, field) for e in self.entries]
            if len(set(values)) != len(values):
                raise ValueError("Repeated import identity")
        required = {
            spu
            for e in self.entries
            for spu in e.product_evidence.full_api_response_ids
        }
        if required != set(self.api_snapshots):
            raise ValueError(
                "Every snapshot must be referenced and every reference resolved"
            )
        for spu, envelope in self.api_snapshots.items():
            if len(canonical(envelope)) > MAX_SNAPSHOT or envelope.get("code") not in [
                200,
                "200",
            ]:
                raise ValueError("Invalid complete product response")
            data = envelope.get("data")
            if (
                not isinstance(data, list)
                or len(data) != 1
                or not isinstance(data[0], dict)
                or str(data[0].get("globalSpuId")) != spu
            ):
                raise ValueError("Wrong product response identity")
            if not isinstance(data[0].get("spuInfo"), dict) or not isinstance(
                data[0].get("skuInfoList"), list
            ):
                raise ValueError("Incomplete product response")  # noqa: TRY004 - Pydantic validation error
            info = data[0]["spuInfo"]
            if data[0].get("region") != "US" or str(info.get("globalSpuId")) != spu:
                raise ValueError(
                    "Product identity and region must match the captured catalog"
                )
            if not all(
                isinstance(info.get(k), str) and 0 < len(info[k]) <= n
                for k, n in [("title", 300), ("brandName", 100), ("articleNumber", 100)]
            ):
                raise ValueError("Incomplete product identity")
        for entry in self.entries:
            expected = str(
                uuid5(
                    NAMESPACE_URL,
                    "wardrobe-first-catalog:" + self.owner_id + ":" + entry.source_id,
                )
            )
            if entry.proposed_garment_id != expected:
                raise ValueError("Garment identifier must bind owner and source")
            selected = entry.product_evidence.confirmed_global_spu_id
            if selected:
                info = self.api_snapshots[selected]["data"][0]["spuInfo"]
                for key, source in [("brand", "brandName"), ("model", "articleNumber")]:
                    field = entry.annotation_fields[key]
                    if (
                        field.source_kind == "product_api"
                        and field.value != info[source]
                    ):
                        raise ValueError(
                            "Product labels must equal the selected API identity"
                        )
        return self


def load_batch(path: Path, expected_digest: str, owner_id: str) -> ImportBatch:
    try:
        if not re.fullmatch(HASH, expected_digest) or str(UUID(owner_id)) != owner_id:
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_size > MAX_BATCH
            ):
                raise ValueError()
            raw = file.read(MAX_BATCH + 1)
        value = json.loads(
            raw,
            object_pairs_hook=_unique_json,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(value, dict):
            raise TypeError()
        body = dict(value)
        claimed = body.pop("digest", None)
        if claimed != expected_digest or digest(body) != expected_digest:
            raise ValueError()
        batch = ImportBatch.model_validate(value)
        if batch.owner_id != owner_id:
            raise ValueError()
        return batch
    except (ValueError, TypeError, OSError, RecursionError, ValidationError):
        raise BusinessError(
            "INVALID_IMPORT_BATCH",
            "Private batch, owner, digest or evidence validation failed",
            422,
        ) from None


def _check_assets(session, root: Path, batch: ImportBatch):
    if (
        session.get(Owner, batch.owner_id) is None
        or session.get(ServiceState, batch.owner_id) is None
    ):
        raise BusinessError(
            "IMPORT_OWNER_MISSING", "Initialize the intended owner first", 409
        )
    for entry in batch.entries:
        asset = session.get(Asset, entry.original_asset_id)
        if (
            asset is None
            or session.get(OwnerAsset, (batch.owner_id, entry.original_asset_id))
            is None
            or asset.sha256 != entry.original_sha256
        ):
            raise BusinessError(
                "IMPORT_ASSET_CONFLICT",
                "Import original is missing or belongs to another owner",
                409,
            )
        try:
            fd = os.open(
                root / "assets" / "original" / asset.sha256, os.O_RDONLY | os.O_NOFOLLOW
            )
            with os.fdopen(fd, "rb") as file:
                info = os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != asset.size:
                    raise ValueError()
                sha = hashlib.file_digest(file, "sha256").hexdigest()
            if sha != asset.sha256:
                raise ValueError()
        except (OSError, ValueError):
            raise BusinessError(
                "IMPORT_IMAGE_INVALID",
                "An original image failed integrity verification",
                409,
            ) from None


def import_catalog(database, root: Path, batch: ImportBatch, *, apply=False):
    value = batch.model_dump(exclude_unset=True)
    expected = value.pop("digest")
    if digest(value) != expected:
        raise BusinessError("INVALID_IMPORT_BATCH", "Frozen batch content changed", 422)
    # Hold one write lock across preflight and application. This is an operator
    # operation, not a request-facing model/publication permission.
    with database.write() as session:
        _check_assets(session, root, batch)
        saved = session.get(CatalogImport, (batch.owner_id, batch.digest))
        if saved is not None:
            for entry in batch.entries:
                binding = session.get(CatalogSource, (batch.owner_id, entry.source_id))
                if binding is None or (
                    binding.garment_id,
                    binding.asset_id,
                    binding.original_sha256,
                    binding.batch_digest,
                ) != (
                    entry.proposed_garment_id,
                    entry.original_asset_id,
                    entry.original_sha256,
                    batch.digest,
                ):
                    raise BusinessError(
                        "IMPORT_REPLAY_CONFLICT",
                        "Existing import source mapping changed",
                        409,
                    )
            return saved.receipt
        existing = session.scalar(
            select(func.count())
            .select_from(Garment)
            .where(Garment.owner_id == batch.owner_id)
        )
        if existing or session.scalar(
            select(CatalogImport).where(CatalogImport.owner_id == batch.owner_id)
        ):
            raise BusinessError(
                "IMPORT_CATALOG_NOT_EMPTY",
                "First import cannot overwrite an existing catalog",
                409,
            )
        for entry in batch.entries:
            if session.get(
                CatalogSource, (batch.owner_id, entry.source_id)
            ) or session.get(Garment, entry.proposed_garment_id):
                raise BusinessError(
                    "IMPORT_SOURCE_CONFLICT",
                    "A proposed identity is already bound",
                    409,
                )
        if not apply:
            return success(
                {
                    "checked_entries": len(batch.entries),
                    "batch_digest": batch.digest,
                    "effective": False,
                    "state": "validated",
                }
            )
        snapshot_ids = {}
        for spu, response in batch.api_snapshots.items():
            snapshot = ProductSnapshot(
                id=str(uuid4()),
                owner_id=batch.owner_id,
                provider="manual",
                region="US",
                global_spu_id=spu,
                digest=digest(response),
                response=response,
                recorded_at=batch.prepared_at,
            )
            session.add(snapshot)
            snapshot_ids[spu] = snapshot.id
        now = int(time.time())
        receipt_id = str(uuid4())
        mappings = []
        for entry in batch.entries:
            row = Garment(
                id=entry.proposed_garment_id,
                owner_id=batch.owner_id,
                name=entry.name,
                category=entry.category,
                roles=sorted(set(entry.roles)),
                availability=entry.availability,
                state="active",
                revision=1,
                annotation_fields={
                    k: f.model_dump(exclude_none=True)
                    if f.value is not None
                    else {
                        "value": None,
                        "source_kind": "unknown",
                        "verification_state": "unknown",
                        "evidence": [],
                    }
                    for k, f in entry.annotation_fields.items()
                },
                annotation_review_state="initial",
                product_reference=None,
                created_at=now,
            )
            session.add(row)
            session.flush()
            session.add(
                GarmentAsset(garment_id=row.id, asset_id=entry.original_asset_id)
            )
            session.add(
                CatalogSource(
                    owner_id=batch.owner_id,
                    source_id=entry.source_id,
                    garment_id=row.id,
                    asset_id=entry.original_asset_id,
                    original_sha256=entry.original_sha256,
                    batch_digest=batch.digest,
                    source_metadata={
                        "filename": entry.filename,
                        "filename_brand": entry.filename_brand,
                        **entry.source_metadata,
                    },
                )
            )
            session.add(
                GarmentProductEvidence(
                    garment_id=row.id,
                    owner_id=batch.owner_id,
                    evidence=entry.product_evidence.model_dump(),
                    snapshot_ids=[
                        snapshot_ids[spu]
                        for spu in entry.product_evidence.full_api_response_ids
                    ],
                )
            )
            session.add(
                AuditEvent(
                    id=str(uuid4()),
                    owner_id=batch.owner_id,
                    client_id="operator-first-catalog",
                    action="first_catalog_create",
                    target_id=row.id,
                    receipt_id=receipt_id,
                    created_at=now,
                )
            )
            mappings.append(
                {
                    "source_id": entry.source_id,
                    "garment_id": row.id,
                    "asset_id": entry.original_asset_id,
                }
            )
        bump_catalog(session, batch.owner_id)
        result = success(
            {
                "receipt_id": receipt_id,
                "batch_digest": batch.digest,
                "created_garments": len(mappings),
                "product_snapshots": len(snapshot_ids),
                "effective": True,
                "mappings": mappings,
            }
        )
        session.add(
            CatalogImport(
                owner_id=batch.owner_id,
                digest=batch.digest,
                parent_batch_id=batch.parent_batch_id,
                receipt=result,
                created_at=now,
            )
        )
        session.flush()
        return result


def evidence_summary(row: Garment, session):
    evidence = session.get(GarmentProductEvidence, row.id)
    if evidence is None or evidence.owner_id != row.owner_id:
        return None
    value = evidence.evidence
    if "schema_version" in value:
        raise BusinessError(
            "EVIDENCE_UNAVAILABLE",
            "Legacy channel product evidence is read-only and unsupported",
            503,
        )
    return {
        key: value[key]
        for key in [
            "link_state",
            "confirmed_global_spu_id",
            "candidate_global_spu_ids",
            "rejected_global_spu_ids",
            "style_confirmed_article_number",
            "owned_sku_id",
            "owned_size",
            "entered_article_number",
            "issues",
        ]
    }


def get_product_evidence(sessions, principal, garment_id):
    principal.require("wardrobe.read")
    try:
        garment_id = str(UUID(garment_id))
    except (ValueError, TypeError, AttributeError):
        raise BusinessError(
            "INVALID_ARGUMENT", "A garment UUID is required", 422
        ) from None
    with sessions.begin() as session:
        garment = session.get(Garment, garment_id)
        if (
            garment is None
            or garment.owner_id != principal.owner_id
            or garment.state != "active"
        ):
            raise BusinessError("NOT_FOUND", "Garment not found", 404)
        evidence = session.get(GarmentProductEvidence, garment_id)
        if evidence is None:
            return success(
                {
                    "garment_id": garment_id,
                    "product_evidence": None,
                    "full_api_responses": {},
                }
            )
        if evidence.owner_id != principal.owner_id:
            raise BusinessError("NOT_FOUND", "Product evidence not found", 404)
        if "schema_version" in evidence.evidence:
            raise BusinessError(
                "EVIDENCE_UNAVAILABLE",
                "Legacy channel product evidence is read-only and unsupported",
                503,
            )
        snapshots = {}
        for identifier in evidence.snapshot_ids:
            row = session.get(ProductSnapshot, identifier)
            if row is None or row.owner_id != principal.owner_id:
                raise BusinessError(
                    "EVIDENCE_UNAVAILABLE", "A product snapshot is unavailable", 503
                )
            snapshots[row.global_spu_id] = row.response
        return success(
            {
                "garment_id": garment_id,
                "product_evidence": evidence.evidence,
                "full_api_responses": snapshots,
            }
        )
