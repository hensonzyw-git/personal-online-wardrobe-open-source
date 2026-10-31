import base64
import json
import secrets
import time
from typing import Literal
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from pydantic import Field, ValidationError, field_validator
from sqlalchemy import delete, select

from .auth import recheck_grant
from .contracts import StrictModel, success
from .errors import BusinessError
from .labels import LabelValues, caller_fields
from .models import (
    AnnotationVersion,
    Asset,
    ChangeProposal,
    Garment,
    GarmentAsset,
    HistoryAmendment,
    HistoryEvent,
    HumanDecision,
    OwnerAsset,
    ProductDraft,
    ServiceState,
    TrustedIssuer,
)
from .writes import audit, bump_catalog, digest, idempotent


class GarmentPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    roles: (
        list[
            Literal[
                "upper",
                "lower",
                "footwear",
                "outerwear",
                "base_layer",
                "accessory",
                "one_piece",
            ]
        ]
        | None
    ) = Field(default=None, max_length=8)
    availability: Literal["available", "unavailable", "unknown"] | None = None
    annotation_fields: LabelValues | None = None
    asset_ids: list[str] | None = Field(default=None, max_length=20)
    annotation_version_id: str | None = None
    verified_fields: list[str] | None = Field(default=None, min_length=1, max_length=36)
    history_kind: (
        Literal["generated", "shown", "adopted", "worn", "rejected"] | None
    ) = None
    occurred_at: int | None = None
    note: str | None = Field(default=None, max_length=500)


class Change(StrictModel):
    target_type: Literal["garment", "history", "product_draft"] = "garment"
    target_id: str
    base_revision: int = Field(ge=1)
    operation: Literal[
        "update",
        "delete",
        "restore",
        "publish_annotation",
        "verify_fields",
        "correct_history",
        "revoke_history",
        "publish_product",
    ]
    patch: GarmentPatch = Field(default_factory=GarmentPatch)

    @field_validator("target_id")
    @classmethod
    def canonical_uuid(cls, value):
        return str(UUID(value))


class ProposalInput(StrictModel):
    changes: list[Change] = Field(min_length=1, max_length=50)


class Assertion(StrictModel):
    version: Literal["wardrobe-human/1"]
    iss: str = Field(min_length=1, max_length=100)
    sub: str = Field(min_length=1, max_length=200)
    owner_id: str
    client_id: str
    actor_subject: str = Field(min_length=1, max_length=200)
    aud: str
    purpose: Literal["decision"]
    proposal_id: str
    digest: str
    revisions: dict[str, int]
    nonce: str
    jti: str = Field(min_length=1, max_length=100)
    decision: Literal["approve", "reject"]
    iat: int
    exp: int


class PreviewAssertion(StrictModel):
    version: Literal["wardrobe-review/1"]
    iss: str = Field(min_length=1, max_length=100)
    sub: str = Field(min_length=1, max_length=200)
    owner_id: str
    client_id: str
    actor_subject: str = Field(min_length=1, max_length=200)
    aud: str
    purpose: Literal["review_preview"]
    proposal_id: str
    digest: str
    nonce: str
    garment_id: str
    target_revision: int = Field(ge=1)
    asset_id: str
    iat: int
    exp: int


def _strict_json(raw: bytes):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate field")
            value[key] = item
        return value

    return json.loads(raw, object_pairs_hook=unique)


def _unbase64(raw: str) -> bytes:
    return base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)


class ChangeService:
    def __init__(self, database, asset_service, audience: str):
        self.database, self.assets, self.audience = (
            database,
            asset_service,
            audience + ":human-decisions",
        )

    @staticmethod
    def _target(session, owner_id, identifier, target_type="garment"):
        try:
            identifier = str(UUID(identifier))
        except ValueError:
            raise BusinessError(
                "INVALID_ARGUMENT", "A target UUID is required", 422
            ) from None
        model = {
            "garment": Garment,
            "history": HistoryEvent,
            "product_draft": ProductDraft,
        }[target_type]
        row = session.scalar(
            select(model).where(model.owner_id == owner_id, model.id == identifier)
        )
        if row is None:
            raise BusinessError("NOT_FOUND", "Target not found", 404)
        return row

    def _validate(self, session, owner_id, change):
        target = self._target(
            session, owner_id, change["target_id"], change["target_type"]
        )
        if target.revision != change["base_revision"]:
            raise BusinessError(
                "REVISION_CONFLICT", "Target changed; review a fresh proposal", 409
            )
        patch = change["patch"]
        if change["target_type"] == "product_draft":
            from .products import validate_publication

            if change["operation"] != "publish_product" or patch:
                raise BusinessError(
                    "INVALID_ARGUMENT",
                    "Product publication requires an immutable draft without a patch",
                    422,
                )
            if "before" in change and change["before"].get("draft_digest") != digest(
                target.payload
            ):
                raise BusinessError(
                    "REVISION_CONFLICT",
                    "Product evidence changed; review a fresh proposal",
                    409,
                )
            validate_publication(session, owner_id, target, self.assets)
            return target
        if change["target_type"] == "history":
            if not target.active or change["operation"] not in {
                "correct_history",
                "revoke_history",
            }:
                raise BusinessError(
                    "INVALID_ARGUMENT",
                    "History change requires an active history target and explicit correction/revocation",
                    422,
                )
            if (
                change["operation"] == "revoke_history"
                and patch
                or change["operation"] == "correct_history"
                and (
                    not patch
                    or not set(patch) <= {"history_kind", "occurred_at", "note"}
                )
            ):
                raise BusinessError(
                    "INVALID_ARGUMENT",
                    "History patch does not match its operation",
                    422,
                )
            if (
                "history_kind" in patch
                and patch["history_kind"] is None
                or "occurred_at" in patch
                and (
                    patch["occurred_at"] is None
                    or patch["occurred_at"] > int(time.time()) + 30
                    or patch["occurred_at"] < 0
                )
            ):
                raise BusinessError(
                    "INVALID_ARGUMENT",
                    "History kind and occurrence must be explicit and valid",
                    422,
                )
            return target
        if change["operation"] == "publish_annotation":
            if set(patch) != {"annotation_version_id"} or target.state != "active":
                raise BusinessError(
                    "INVALID_ARGUMENT",
                    "Publication requires one draft version on an active garment",
                    422,
                )
            draft = session.get(AnnotationVersion, patch["annotation_version_id"])
            if (
                draft is None
                or draft.garment_id != target.id
                or draft.base_revision != target.revision
                or draft.status != "draft"
            ):
                raise BusinessError(
                    "REVISION_CONFLICT",
                    "Annotation draft is missing, changed or already published",
                    409,
                )
            if any(
                target.annotation_fields.get(field, {}).get("verification_state")
                == "user_verified"
                and target.annotation_fields[field].get("value") != value.get("value")
                for field, value in draft.fields.items()
            ):
                raise BusinessError(
                    "VERIFIED_FIELD_CONFLICT",
                    "Draft conflicts with a verified field; review an explicit field change",
                    409,
                )
            return target
        if change["operation"] == "verify_fields":
            fields = patch.get("verified_fields")
            if (
                set(patch) != {"verified_fields"}
                or not fields
                or target.state != "active"
                or any(
                    field not in target.annotation_fields
                    or target.annotation_fields[field].get("value") is None
                    for field in fields
                )
            ):
                raise BusinessError(
                    "INVALID_ARGUMENT",
                    "Verify specific existing non-null fields after human factual inspection",
                    422,
                )
            return target
        if change["operation"] not in {"update", "delete", "restore"} or set(patch) & {
            "annotation_version_id",
            "verified_fields",
            "history_kind",
            "occurred_at",
            "note",
        }:
            raise BusinessError(
                "INVALID_ARGUMENT", "Garment patch does not match its operation", 422
            )
        if change["operation"] == "restore":
            if (
                target.state != "deleted"
                or not target.restore_until
                or target.restore_until <= int(time.time())
            ):
                raise BusinessError(
                    "RESTORE_UNAVAILABLE", "Recovery window is closed", 409
                )
            images = list(
                session.scalars(
                    select(Asset)
                    .join(GarmentAsset)
                    .where(GarmentAsset.garment_id == target.id)
                )
            )
            if not images:
                raise BusinessError(
                    "RESTORE_UNAVAILABLE", "Recovery image is missing", 409
                )
            for asset in images:
                if not (self.assets.originals / asset.sha256).is_file():
                    raise BusinessError(
                        "RESTORE_UNAVAILABLE", "Recovery image is missing", 409
                    )
        elif target.state != "active":
            raise BusinessError("TARGET_REMOVED", "Target is no longer active", 409)
        patch = change["patch"]
        if change["operation"] != "update" and patch:
            raise BusinessError(
                "INVALID_ARGUMENT", "Delete and restore cannot carry a patch", 422
            )
        if change["operation"] == "update" and not patch:
            raise BusinessError(
                "INVALID_ARGUMENT", "Update requires a specific change", 422
            )
        if (
            "name" in patch
            and patch["name"] is None
            or "roles" in patch
            and patch["roles"] is None
            or "availability" in patch
            and patch["availability"] is None
            or "annotation_fields" in patch
            and patch["annotation_fields"] is None
        ):
            raise BusinessError(
                "INVALID_ARGUMENT", "Required fields cannot be null", 422
            )
        if "asset_ids" in patch and (
            patch["asset_ids"] is None
            or any(
                session.get(OwnerAsset, (owner_id, asset_id)) is None
                for asset_id in patch["asset_ids"]
            )
        ):
            raise BusinessError(
                "ASSET_NOT_OWNED", "Patch assets must belong to this owner", 422
            )
        return target

    @staticmethod
    def _view(row):
        return {
            "proposal_id": row.id,
            "owner_id": row.owner_id,
            "changes": row.changes,
            "digest": row.digest,
            "nonce": row.nonce,
            "expires_at": row.expires_at,
            "state": row.state,
            "receipt": row.receipt,
        }

    def propose(self, principal, payload: ProposalInput, key: str):
        principal.require("wardrobe.propose_change")
        changes = [
            c.model_dump(exclude={"patch"})
            | {"patch": c.patch.model_dump(exclude_unset=True)}
            for c in payload.changes
        ]
        if len({c["target_id"] for c in changes}) != len(changes):
            raise BusinessError(
                "INVALID_ARGUMENT", "Each batch target must appear once", 422
            )
        with self.database.write() as session:
            recheck_grant(session, principal, required_scope="wardrobe.propose_change")

            def work():
                snapshots = []
                for change in changes:
                    target = self._validate(session, principal.owner_id, change)
                    if isinstance(target, ProductDraft):
                        from .products import existing_garment

                        garment = existing_garment(
                            session, principal.owner_id, target.payload
                        )
                        snapshots.append(
                            {
                                "id": target.id,
                                "revision": target.revision,
                                "draft_digest": digest(target.payload),
                                "product_draft": target.payload,
                                "existing_garment": {
                                    "id": garment.id,
                                    "revision": garment.revision,
                                    "name": garment.name,
                                    "category": garment.category,
                                    "roles": garment.roles,
                                    "availability": garment.availability,
                                    "annotation_fields": garment.annotation_fields,
                                    "product_reference": garment.product_reference,
                                    "asset_ids": list(
                                        session.scalars(
                                            select(GarmentAsset.asset_id).where(
                                                GarmentAsset.garment_id == garment.id
                                            )
                                        )
                                    ),
                                }
                                if garment
                                else None,
                            }
                        )
                        continue
                    snapshots.append(
                        {
                            "id": target.id,
                            "name": target.name
                            if isinstance(target, Garment)
                            else target.kind,
                            "category": target.category
                            if isinstance(target, Garment)
                            else None,
                            "roles": target.roles
                            if isinstance(target, Garment)
                            else [],
                            "availability": target.availability
                            if isinstance(target, Garment)
                            else None,
                            "annotation_fields": target.annotation_fields
                            if isinstance(target, Garment)
                            else {},
                            "history": {
                                "kind": target.kind,
                                "occurred_at": target.occurred_at,
                                "payload": target.payload,
                            }
                            if isinstance(target, HistoryEvent)
                            else None,
                            "state": target.state
                            if isinstance(target, Garment)
                            else "active",
                            "revision": target.revision,
                            "asset_ids": list(
                                session.scalars(
                                    select(GarmentAsset.asset_id).where(
                                        GarmentAsset.garment_id == target.id
                                    )
                                )
                            ),
                            "annotation_draft": session.get(
                                AnnotationVersion,
                                change["patch"]["annotation_version_id"],
                            ).fields
                            if change["operation"] == "publish_annotation"
                            else None,
                        }
                    )
                identifier, nonce, expires = (
                    str(uuid4()),
                    secrets.token_urlsafe(24),
                    int(time.time()) + 900,
                )
                fingerprint = digest(
                    {
                        "version": "wardrobe-proposal/1",
                        "id": identifier,
                        "owner": principal.owner_id,
                        "changes": changes,
                        "before": snapshots,
                        "nonce": nonce,
                        "expires_at": expires,
                    }
                )
                row = ChangeProposal(
                    id=identifier,
                    owner_id=principal.owner_id,
                    created_by_client=principal.client_id,
                    changes=[
                        {**c, "before": before} for c, before in zip(changes, snapshots)
                    ],
                    digest=fingerprint,
                    nonce=nonce,
                    expires_at=expires,
                    state="pending",
                    receipt=None,
                )
                session.add(row)
                return success(self._view(row))

            return idempotent(
                session, principal, "propose_change", key, {"changes": changes}, work
            )

    def get(self, principal, proposal_id):
        if principal.channel == "agent":
            principal.require("wardrobe.read")
        else:
            principal.require("human.decision", channel="human_adapter")
        with self.database.sessions.begin() as session:
            row = session.get(ChangeProposal, proposal_id)
            if row is None or row.owner_id != principal.owner_id:
                raise BusinessError("NOT_FOUND", "Proposal not found", 404)
            view = self._view(row)
            if row.state == "pending":
                if row.expires_at <= int(time.time()):
                    view["state"] = "expired"
                elif any(
                    self._target(
                        session, row.owner_id, c["target_id"], c["target_type"]
                    ).revision
                    != c["base_revision"]
                    for c in row.changes
                ):
                    view["state"] = "conflicted"
                else:
                    for change in row.changes:
                        if change["target_type"] == "product_draft":
                            try:
                                self._validate(session, row.owner_id, change)
                            except BusinessError:
                                view["state"] = "conflicted"
                                break
            return success(view)

    def preview(self, principal, signed, proposal_id, asset_id=None):
        principal.require("human.preview", channel="human_adapter")
        try:
            if not isinstance(signed, str) or len(signed) > 32768:
                raise ValueError()
            head, body, signature = signed.split(".")
            header = _strict_json(_unbase64(head))
            if (
                not isinstance(header, dict)
                or set(header) != {"alg", "typ", "kid"}
                or header["alg"] != "EdDSA"
                or header["typ"] != "wardrobe-human-v1"
                or not isinstance(header["kid"], str)
            ):
                raise ValueError()
            assertion = PreviewAssertion.model_validate(_strict_json(_unbase64(body)))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise BusinessError(
                "INVALID_PREVIEW_GRANT",
                "Review assertion does not match its contract",
                403,
            ) from None
        with self.database.sessions.begin() as session:
            recheck_grant(session, principal, required_scope="human.preview")
            issuer = session.get(TrustedIssuer, (assertion.iss, header["kid"]))
            now = int(time.time())
            binding = {
                "sub": assertion.sub,
                "owner_id": assertion.owner_id,
                "client_id": assertion.client_id,
            }
            if (
                issuer is None
                or issuer.revoked_at is not None
                or issuer.expires_at <= now
                or binding not in issuer.bindings
                or assertion.owner_id != principal.owner_id
                or assertion.client_id != principal.client_id
                or assertion.aud != self.audience
                or issuer.audience != self.audience
                or assertion.purpose not in issuer.purposes
                or assertion.iat > now + 30
                or assertion.exp <= now
                or not 0 < assertion.exp - assertion.iat <= 120
            ):
                raise BusinessError(
                    "INVALID_PREVIEW_GRANT",
                    "Review identity, issuer, purpose or expiry is invalid",
                    403,
                )
            try:
                key = load_pem_public_key(issuer.public_key.encode())
                if not isinstance(key, Ed25519PublicKey):
                    raise TypeError()
                key.verify(_unbase64(signature), (head + "." + body).encode("ascii"))
            except (InvalidSignature, TypeError, ValueError, UnicodeError):
                raise BusinessError(
                    "INVALID_PREVIEW_GRANT", "Review signature is invalid", 403
                ) from None
            row = session.get(ChangeProposal, proposal_id)
            if (
                row is None
                or row.owner_id != principal.owner_id
                or row.id != assertion.proposal_id
                or row.state != "pending"
                or row.expires_at <= now
                or assertion.exp > row.expires_at
                or row.digest != assertion.digest
                or row.nonce != assertion.nonce
            ):
                raise BusinessError(
                    "PREVIEW_UNAVAILABLE",
                    "Review proposal is unavailable or changed",
                    409,
                )
            for change in row.changes:
                self._validate(session, principal.owner_id, change)
            change = next(
                (
                    c
                    for c in row.changes
                    if c["target_type"] == "garment"
                    and c["target_id"] == assertion.garment_id
                    and c["operation"] == "restore"
                    and c["base_revision"] == assertion.target_revision
                ),
                None,
            )
            if (
                change is None
                or asset_id is not None
                and asset_id != assertion.asset_id
                or session.get(GarmentAsset, (assertion.garment_id, assertion.asset_id))
                is None
            ):
                raise BusinessError(
                    "PREVIEW_UNAVAILABLE",
                    "Review does not bind this removed image",
                    403,
                )
            asset = session.get(Asset, assertion.asset_id)
            path = self.assets.originals / asset.sha256
            if not path.is_file():
                raise BusinessError(
                    "ASSET_UNAVAILABLE", "Review image is unavailable", 503
                )
            return path, asset.mime, assertion.asset_id, assertion.exp

    def cancel(self, principal, proposal_id):
        principal.require("wardrobe.propose_change")
        with self.database.write() as session:
            recheck_grant(session, principal, required_scope="wardrobe.propose_change")
            row = session.get(ChangeProposal, proposal_id)
            if (
                row is None
                or row.owner_id != principal.owner_id
                or row.created_by_client != principal.client_id
            ):
                raise BusinessError("NOT_FOUND", "Proposal not found", 404)
            if row.state == "cancelled":
                return success(self._view(row))
            if row.state != "pending" or row.expires_at <= int(time.time()):
                raise BusinessError(
                    "PROPOSAL_UNAVAILABLE", "Proposal cannot be cancelled", 409
                )
            row.state = "cancelled"
            audit(session, principal, "cancel_proposal", row.id, str(uuid4()))
            return success(self._view(row))

    def decide(self, principal, signed: str):
        principal.require("human.decision", channel="human_adapter")
        try:
            if not isinstance(signed, str) or len(signed) > 32768:
                raise ValueError()
            head, body, signature = signed.split(".")
            header = _strict_json(_unbase64(head))
            if (
                not isinstance(header, dict)
                or set(header) != {"alg", "typ", "kid"}
                or header["alg"] != "EdDSA"
                or header["typ"] != "wardrobe-human-v1"
                or not isinstance(header["kid"], str)
                or not 1 <= len(header["kid"]) <= 100
            ):
                raise ValueError()
            assertion = Assertion.model_validate(_strict_json(_unbase64(body)))
        except (ValueError, TypeError, ValidationError, UnicodeError, RecursionError):
            raise BusinessError(
                "INVALID_CONFIRMATION", "Confirmation does not match the contract", 403
            ) from None
        with self.database.write() as session:
            recheck_grant(session, principal, required_scope="human.decision")
            issuer = session.get(TrustedIssuer, (assertion.iss, header["kid"]))
            now = int(time.time())
            if (
                issuer is None
                or issuer.revoked_at is not None
                or issuer.expires_at <= now
            ):
                raise BusinessError(
                    "UNTRUSTED_ISSUER", "Confirmation issuer is unavailable", 403
                )
            binding = {
                "sub": assertion.sub,
                "owner_id": assertion.owner_id,
                "client_id": assertion.client_id,
            }
            if (
                binding not in issuer.bindings
                or assertion.owner_id != principal.owner_id
                or assertion.client_id != principal.client_id
                or assertion.aud != self.audience
                or issuer.audience != self.audience
                or assertion.purpose not in issuer.purposes
            ):
                raise BusinessError(
                    "CONFIRMATION_BINDING_DENIED",
                    "Confirmation identity or purpose is not authorized",
                    403,
                )
            try:
                key = load_pem_public_key(issuer.public_key.encode())
                if not isinstance(key, Ed25519PublicKey):
                    raise TypeError()
                key.verify(_unbase64(signature), (head + "." + body).encode("ascii"))
            except (InvalidSignature, ValueError, TypeError, UnicodeError):
                raise BusinessError(
                    "INVALID_CONFIRMATION", "Confirmation signature is invalid", 403
                ) from None
            row = session.get(ChangeProposal, assertion.proposal_id)
            if row is None or row.owner_id != principal.owner_id:
                raise BusinessError("NOT_FOUND", "Proposal not found", 404)
            expected = {c["target_id"]: c["base_revision"] for c in row.changes}
            if (
                assertion.digest != row.digest
                or assertion.nonce != row.nonce
                or assertion.revisions != expected
                or assertion.exp > row.expires_at
            ):
                raise BusinessError(
                    "CONFIRMATION_MISMATCH",
                    "Confirmation does not bind this proposal",
                    403,
                )
            fingerprint = digest(signed)
            previous = session.get(HumanDecision, (assertion.iss, assertion.jti))
            if previous:
                if (
                    previous.assertion_digest != fingerprint
                    or previous.proposal_id != row.id
                ):
                    raise BusinessError(
                        "CONFIRMATION_REPLAY",
                        "Confirmation ID was used for a different decision",
                        409,
                    )
                return success(previous.receipt)
            if (
                assertion.iat > now + 30
                or assertion.exp <= now
                or not 0 < assertion.exp - assertion.iat <= 120
            ):
                raise BusinessError(
                    "CONFIRMATION_EXPIRED",
                    "Confirmation is outside its validity window",
                    403,
                )
            if row.state != "pending" or row.expires_at <= now:
                raise BusinessError(
                    "PROPOSAL_UNAVAILABLE", "Proposal cannot be acted on", 409
                )
            targets = [
                self._validate(session, principal.owner_id, c) for c in row.changes
            ]
            receipt_id = str(uuid4())
            revisions = {}
            published_garments = {}
            if assertion.decision == "approve":
                for target, change in zip(targets, row.changes):
                    if isinstance(target, ProductDraft):
                        from .products import publish_product

                        garment = publish_product(
                            session, principal.owner_id, target, self.assets
                        )
                        published_garments[target.id] = {
                            "garment_id": garment.id,
                            "revision": garment.revision,
                        }
                    elif isinstance(target, HistoryEvent):
                        before = {
                            "kind": target.kind,
                            "occurred_at": target.occurred_at,
                            "payload": target.payload,
                            "active": target.active,
                            "revision": target.revision,
                        }
                        if change["operation"] == "revoke_history":
                            target.active = False
                        else:
                            patch = change["patch"]
                            if "history_kind" in patch:
                                target.kind = patch["history_kind"]
                            if "occurred_at" in patch:
                                target.occurred_at = patch["occurred_at"]
                            if "note" in patch:
                                target.payload = {
                                    **target.payload,
                                    "note": patch["note"],
                                }
                        session.add(
                            HistoryAmendment(
                                id=str(uuid4()),
                                target_id=target.id,
                                proposal_id=row.id,
                                before=before,
                                after={
                                    "kind": target.kind,
                                    "occurred_at": target.occurred_at,
                                    "payload": target.payload,
                                    "active": target.active,
                                    "revision": target.revision + 1,
                                },
                                created_at=now,
                            )
                        )
                    elif change["operation"] == "publish_annotation":
                        draft = session.get(
                            AnnotationVersion, change["patch"]["annotation_version_id"]
                        )
                        target.annotation_fields = {
                            **target.annotation_fields,
                            **{
                                key: value
                                for key, value in draft.fields.items()
                                if target.annotation_fields.get(key, {}).get(
                                    "verification_state"
                                )
                                != "user_verified"
                            },
                        }
                        target.annotation_review_state = "published"
                        draft.status = "published"
                    elif change["operation"] == "verify_fields":
                        target.annotation_fields = {
                            key: {
                                **value,
                                "source_kind": "user_verified",
                                "verification_state": "user_verified",
                                "verified_at": now,
                                "decision_id": receipt_id,
                            }
                            if key in change["patch"]["verified_fields"]
                            else value
                            for key, value in target.annotation_fields.items()
                        }
                    elif change["operation"] == "delete":
                        target.state, target.deleted_at, target.restore_until = (
                            "deleted",
                            now,
                            now + 30 * 86400,
                        )
                    elif change["operation"] == "restore":
                        target.state, target.deleted_at, target.restore_until = (
                            "active",
                            None,
                            None,
                        )
                    else:
                        for name, value in change["patch"].items():
                            if name == "asset_ids":
                                session.execute(
                                    delete(GarmentAsset).where(
                                        GarmentAsset.garment_id == target.id
                                    )
                                )
                                for asset_id in set(value):
                                    session.add(
                                        GarmentAsset(
                                            garment_id=target.id, asset_id=asset_id
                                        )
                                    )
                            elif name == "annotation_fields":
                                # Publication confirms use, not the factual truth of each field.
                                target.annotation_fields = {
                                    **target.annotation_fields,
                                    **caller_fields(LabelValues.model_validate(value)),
                                }
                                target.annotation_review_state = "published"
                            else:
                                setattr(target, name, value)
                    target.revision += 1
                    revisions[target.id] = target.revision
                if any(
                    isinstance(target, (Garment, ProductDraft)) for target in targets
                ):
                    bump_catalog(session, principal.owner_id)
                if any(isinstance(target, HistoryEvent) for target in targets):
                    session.get(ServiceState, principal.owner_id).history_revision += 1
            row.state = "executed" if assertion.decision == "approve" else "rejected"
            row.receipt = {
                "receipt_id": receipt_id,
                "proposal_id": row.id,
                "state": row.state,
                "revisions": revisions,
            }
            if published_garments:
                row.receipt = {**row.receipt, "published_garments": published_garments}
            session.add(
                HumanDecision(
                    issuer=assertion.iss,
                    jti=assertion.jti,
                    proposal_id=row.id,
                    assertion_digest=fingerprint,
                    receipt=row.receipt,
                    created_at=now,
                )
            )
            audit(session, principal, "human_decision", row.id, receipt_id)
            return success(row.receipt)
