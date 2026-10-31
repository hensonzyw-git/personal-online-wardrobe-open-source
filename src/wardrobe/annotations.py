"""Suggestions are versioned data, never effective garment edits."""

import time
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator
from sqlalchemy import select

from .auth import recheck_grant
from .contracts import StrictModel, success
from .errors import BusinessError
from .labels import LabelValues, caller_fields
from .models import AnnotationVersion, Garment, GarmentAsset
from .writes import bump_catalog, idempotent


class SuggestionInput(StrictModel):
    garment_id: str
    base_revision: int = Field(ge=1)
    values: LabelValues


class VisionOutput(StrictModel):
    schema_version: Literal["wardrobe-labels/1"]
    model_version: str = Field(min_length=1, max_length=100)
    prompt_version: str = Field(min_length=1, max_length=100)
    values: LabelValues
    observed_fields: list[str] = Field(default_factory=list)
    evidence_asset_ids: list[str] = Field(min_length=1, max_length=20)
    multi_subject: bool

    @model_validator(mode="after")
    def conservative_fields(self):
        fields = self.values.model_dump(exclude_unset=True)
        if any(field not in fields for field in self.observed_fields):
            raise ValueError("Observed fields must exist in output")
        # The adapter does not support label/OCR evidence yet; keep these unknown.
        forbidden = (
            "composition",
            "brand",
            "model",
            "size",
            "fit_feedback",
            "preference",
            "waterproof",
            "windproof",
        )
        if any(fields.get(field) is not None for field in forbidden):
            raise ValueError(
                "This vision schema cannot substantiate identity, user facts or exact functional claims"
            )
        return self


class AnnotationService:
    def __init__(self, database, catalog):
        self.database, self.catalog = database, catalog

    def save_vision_output(
        self, principal, garment_id, base_revision, output: VisionOutput, key
    ):
        """Worker-only seam; no public route accepts model/provider provenance."""
        principal.require("wardrobe.propose_change")
        with self.database.write() as session:
            recheck_grant(session, principal, required_scope="wardrobe.propose_change")

            def work():
                target = session.get(Garment, garment_id)
                if (
                    target is None
                    or target.owner_id != principal.owner_id
                    or target.state != "active"
                ):
                    raise BusinessError("NOT_FOUND", "Garment not found", 404)
                if target.revision != base_revision:
                    raise BusinessError(
                        "REVISION_CONFLICT", "Garment changed during annotation", 409
                    )
                if output.multi_subject:
                    raise BusinessError(
                        "MULTI_SUBJECT_REVIEW_REQUIRED",
                        "Image identity must be reviewed before publishing garment labels",
                        409,
                    )
                if any(
                    session.get(GarmentAsset, (garment_id, asset_id)) is None
                    for asset_id in output.evidence_asset_ids
                ):
                    raise BusinessError(
                        "INVALID_MODEL_OUTPUT",
                        "Vision evidence must reference this garment's images",
                        502,
                    )
                fields = {
                    field: {
                        "value": value,
                        "source_kind": "observed"
                        if field in output.observed_fields
                        else "inferred"
                        if value is not None
                        else "unknown",
                        "verification_state": "unverified"
                        if value is not None
                        else "unknown",
                        "evidence": [
                            {
                                "asset_id": asset,
                                "model_version": output.model_version,
                                "prompt_version": output.prompt_version,
                            }
                            for asset in output.evidence_asset_ids
                        ],
                        "updated_at": int(time.time()),
                    }
                    for field, value in output.values.model_dump(
                        exclude_unset=True
                    ).items()
                }
                row = AnnotationVersion(
                    id=str(uuid4()),
                    garment_id=garment_id,
                    base_revision=base_revision,
                    fields=fields,
                    status="draft",
                    created_at=int(time.time()),
                )
                session.add(row)
                bump_catalog(session, principal.owner_id)
                return success(
                    {
                        "annotation_version_id": row.id,
                        "status": "draft",
                        "effective": False,
                    }
                )

            return idempotent(
                session,
                principal,
                "vision_draft",
                key,
                {
                    "garment_id": garment_id,
                    "base_revision": base_revision,
                    "output": {
                        **output.model_dump(),
                        "values": output.values.model_dump(exclude_unset=True),
                    },
                },
                work,
            )

    def suggest(self, principal, payload, key):
        principal.require("wardrobe.propose_change")
        with self.database.write() as session:
            recheck_grant(session, principal, required_scope="wardrobe.propose_change")

            def work():
                target = session.get(Garment, payload.garment_id)
                if (
                    target is None
                    or target.owner_id != principal.owner_id
                    or target.state != "active"
                ):
                    raise BusinessError("NOT_FOUND", "Garment not found", 404)
                if target.revision != payload.base_revision:
                    raise BusinessError(
                        "REVISION_CONFLICT",
                        "Garment changed; refresh the suggestion",
                        409,
                    )
                row = AnnotationVersion(
                    id=str(uuid4()),
                    garment_id=target.id,
                    base_revision=target.revision,
                    fields=caller_fields(payload.values),
                    status="draft",
                    created_at=int(time.time()),
                )
                session.add(row)
                bump_catalog(session, principal.owner_id)
                return success(
                    {
                        "annotation_version_id": row.id,
                        "garment_id": target.id,
                        "base_revision": target.revision,
                        "status": "draft",
                        "effective": False,
                    }
                )

            return idempotent(
                session,
                principal,
                "annotation_suggestion",
                key,
                payload.model_dump(exclude_unset=True),
                work,
            )

    def list_review(self, principal, query):
        principal.require("wardrobe.read")
        with self.database.sessions.begin() as session:
            state = self.catalog._state(session, principal.owner_id)
            binding = {
                "owner": principal.owner_id,
                "limit": query.limit,
                "type": "annotation_review",
                "revision": state.catalog_revision,
            }
            statement = (
                select(AnnotationVersion, Garment)
                .join(Garment)
                .where(
                    Garment.owner_id == principal.owner_id,
                    Garment.state == "active",
                    AnnotationVersion.status == "draft",
                )
                .order_by(AnnotationVersion.id)
            )
            if query.cursor:
                cursor = self.catalog._decode(query.cursor)
                if set(cursor) != {*binding, "last"} or any(
                    cursor.get(k) != v for k, v in binding.items()
                ):
                    raise BusinessError(
                        "STALE_CURSOR",
                        "Review list changed or cursor does not match",
                        409,
                    )
                statement = statement.where(AnnotationVersion.id > cursor["last"])
            rows = list(session.execute(statement.limit(query.limit + 1)))
            page = rows[: query.limit]
            return success(
                {
                    "items": [
                        {
                            "annotation_version_id": draft.id,
                            "garment_id": garment.id,
                            "base_revision": draft.base_revision,
                            "current_revision": garment.revision,
                            "fields": draft.fields,
                            "conflicted": draft.base_revision != garment.revision,
                            "missing_fields": [
                                field
                                for field in LabelValues.model_fields
                                if field not in draft.fields
                            ],
                        }
                        for draft, garment in page
                    ],
                    "next_cursor": self.catalog._encode(
                        {**binding, "last": page[-1][0].id}
                    )
                    if len(rows) > query.limit
                    else None,
                    "has_more": len(rows) > query.limit,
                }
            )
