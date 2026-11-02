"""Personal operator reads and atomic single-garment writes. No MCP surface."""

import base64
import hashlib
import hmac
import json
import time
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select

from .auth import _unique_json, recheck_operator, require_operator
from .catalog import CatalogService
from .changes import validate_restore
from .contracts import success
from .errors import BusinessError
from .labels import caller_fields
from .models import Garment, GarmentProductEvidence, ProductSnapshot
from .operator_contracts import OperatorCategoryQuery, OperatorListQuery
from .writes import audit, bump_catalog, idempotent

RESTORE_SECONDS = 2592000


def canonical_id(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise BusinessError(
            "INVALID_ARGUMENT", "A canonical UUID is required", 422
        ) from None


def validate_key(key):
    if (
        not isinstance(key, str)
        or not 1 <= len(key) <= 128
        or any(not 33 <= ord(char) <= 126 for char in key)
    ):
        raise BusinessError(
            "IDEMPOTENCY_KEY_REQUIRED", "A bounded visible ASCII key is required", 422
        )


class OperatorService:
    def __init__(self, database, cursor_key, assets):
        self.database, self.assets = database, assets
        self.catalog = CatalogService(database.sessions, cursor_key, assets.originals)

    @staticmethod
    def _visible(principal, state, now):
        clauses = [Garment.owner_id == principal.owner_id, Garment.state == state]
        if state == "deleted":
            clauses.append(Garment.restore_until > now)
        return clauses

    @staticmethod
    def _binding(principal, query, kind):
        result = {
            "v": 2,
            "kind": kind,
            "owner": principal.owner_id,
            "client": principal.client_id,
            "channel": principal.channel,
            "state": query.state,
            "limit": query.limit,
        }
        if isinstance(query, OperatorListQuery):
            result.update(category=query.category, sort=query.sort, order=query.order)
        else:
            result.update(sort="category", order="asc")
        return result

    def _cursor(self, token, binding, revision):
        if token is None:
            return None
        # Keep the public v1 decoder untouched. v2 rejects duplicate keys,
        # non-finite numbers and overlong cursors before binding or staleness.
        try:
            if not isinstance(token, str) or len(token) > 4096:
                raise ValueError()
            raw = base64.b64decode(
                token + "=" * (-len(token) % 4), altchars=b"-_", validate=True
            )
            signature, document = raw[:32], raw[32:]
            if not hmac.compare_digest(
                signature,
                hmac.digest(self.catalog.cursor_key, document, hashlib.sha256),
            ):
                raise ValueError()
            value = json.loads(
                document,
                object_pairs_hook=_unique_json,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
            if not isinstance(value, dict):
                raise TypeError()
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise BusinessError(
                "INVALID_CURSOR", "Cursor is invalid for this query", 422
            ) from None
        valid = set(value) == {*binding, "catalog_revision", "last"} and all(
            type(value.get(k)) is type(v) and value.get(k) == v
            for k, v in binding.items()
        )
        valid = (
            valid
            and type(value.get("catalog_revision")) is int
            and 0 <= value["catalog_revision"] < 2**63
        )
        last = value.get("last")
        if binding["kind"] == "operator-garments":
            valid = (
                valid and isinstance(last, dict) and set(last) == {"created_at", "id"}
            )
            if valid:
                valid = (
                    type(last["created_at"]) is int
                    and -(2**63) <= last["created_at"] < 2**63
                )
                try:
                    canonical_id(last["id"])
                except BusinessError:
                    valid = False
        else:
            valid = valid and isinstance(last, str) and 1 <= len(last) <= 100
        if not valid:
            raise BusinessError(
                "INVALID_CURSOR", "Cursor is invalid for this query", 422
            )
        if value["catalog_revision"] != revision:
            raise BusinessError(
                "CURSOR_STALE", "Catalog changed; restart from the first page", 409
            )
        return last

    def _view(self, row, session):
        value = self.catalog._view(row, session, self.assets.originals)
        # Never expose import source paths or public/agent-only image endpoints.
        value["image_references"] = [
            {
                "asset_id": ref["asset_id"],
                "mime": ref["mime"],
                "width": ref["width"],
                "height": ref["height"],
                "content_url": f"/v1/operator/garments/{row.id}/assets/{ref['asset_id']}/content",
                "preview": {
                    "url": f"/v1/operator/garments/{row.id}/assets/{ref['asset_id']}/preview",
                    "mime": "image/jpeg",
                    "max_edge": 768,
                    "max_bytes": 150 * 1024,
                },
            }
            for ref in value["image_references"]
            if row.state == "active"
        ]
        if row.state == "deleted":
            value["image_access_status"] = "deleted"
        return value

    def list_garments(self, principal, query: OperatorListQuery):
        require_operator(principal)
        with self.database.sessions.begin() as session:
            revision = self.catalog._state(session, principal.owner_id).catalog_revision
            now = int(time.time())
            binding = self._binding(principal, query, "operator-garments")
            last = self._cursor(query.cursor, binding, revision)
            statement = select(Garment).where(
                *self._visible(principal, query.state, now)
            )
            if query.category is not None:
                statement = statement.where(Garment.category == query.category)
            ascending = query.order == "asc"
            if last is not None:
                time_key = (
                    Garment.created_at > last["created_at"]
                    if ascending
                    else Garment.created_at < last["created_at"]
                )
                id_key = (
                    Garment.id > last["id"] if ascending else Garment.id < last["id"]
                )
                statement = statement.where(
                    or_(
                        time_key, and_(Garment.created_at == last["created_at"], id_key)
                    )
                )
            ordering = (
                (Garment.created_at.asc(), Garment.id.asc())
                if ascending
                else (Garment.created_at.desc(), Garment.id.desc())
            )
            rows = list(
                session.scalars(statement.order_by(*ordering).limit(query.limit + 1))
            )
            items, more = rows[: query.limit], len(rows) > query.limit
            cursor = (
                self.catalog._encode(
                    {
                        **binding,
                        "catalog_revision": revision,
                        "last": {
                            "created_at": items[-1].created_at,
                            "id": items[-1].id,
                        },
                    }
                )
                if more
                else None
            )
            return success(
                {
                    "items": [self._view(row, session) for row in items],
                    "has_more": more,
                    "next_cursor": cursor,
                    "catalog_revision": revision,
                    "server_now": now,
                }
            )

    def categories(self, principal, query: OperatorCategoryQuery):
        require_operator(principal)
        with self.database.sessions.begin() as session:
            revision = self.catalog._state(session, principal.owner_id).catalog_revision
            now = int(time.time())
            binding = self._binding(principal, query, "operator-categories")
            last = self._cursor(query.cursor, binding, revision)
            category = Garment.category.collate("BINARY")
            statement = (
                select(category)
                .where(
                    *self._visible(principal, query.state, now),
                    Garment.category.is_not(None),
                )
                .distinct()
            )
            if last is not None:
                statement = statement.where(category > last)
            rows = list(
                session.scalars(
                    statement.order_by(category.asc()).limit(query.limit + 1)
                )
            )
            items, more = rows[: query.limit], len(rows) > query.limit
            return success(
                {
                    "items": items,
                    "has_more": more,
                    "next_cursor": self.catalog._encode(
                        {**binding, "catalog_revision": revision, "last": items[-1]}
                    )
                    if more
                    else None,
                    "catalog_revision": revision,
                    "server_now": now,
                }
            )

    def get_garment(self, principal, garment_id, state="active"):
        require_operator(principal)
        garment_id = canonical_id(garment_id)
        if state not in {"active", "deleted"}:
            raise BusinessError("INVALID_ARGUMENT", "Invalid garment view", 422)
        with self.database.sessions.begin() as session:
            revision = self.catalog._state(session, principal.owner_id).catalog_revision
            now = int(time.time())
            row = session.scalar(
                select(Garment).where(
                    Garment.id == garment_id, *self._visible(principal, state, now)
                )
            )
            if row is None:
                raise BusinessError("NOT_FOUND", "Garment not found", 404)
            value = self._view(row, session)
            value["product_evidence"] = self._evidence(session, row)
            return success(
                {"garment": value, "catalog_revision": revision, "server_now": now}
            )

    @staticmethod
    def _evidence(session, garment):
        """Allowlisted existing evidence; never raw API responses or source files."""
        row = session.get(GarmentProductEvidence, garment.id)
        if row is None:
            return None
        if row.owner_id != garment.owner_id:
            raise BusinessError(
                "EVIDENCE_UNAVAILABLE", "Product evidence is unavailable", 503
            )
        value = row.evidence
        if "schema_version" in value:
            raise BusinessError(
                "EVIDENCE_UNAVAILABLE", "Unsupported product evidence format", 503
            )
        # Reuse the same legacy summary parser as the public reader. Snapshot
        # payloads are deliberately not projected because they are raw responses.
        from .catalog_import import evidence_summary

        for identifier in row.snapshot_ids:
            snap = session.get(ProductSnapshot, identifier)
            if snap is None or snap.owner_id != garment.owner_id:
                raise BusinessError(
                    "EVIDENCE_UNAVAILABLE", "Product snapshot is unavailable", 503
                )
        return {
            "schema_version": "wardrobe-catalog-product/1",
            **evidence_summary(garment, session),
        }

    def _write(self, principal, garment_id, payload, key, operation):
        require_operator(principal)
        garment_id = canonical_id(garment_id)
        validate_key(key)
        action = f"operator_{operation}_garment"
        fingerprint = {
            "garment_id": garment_id,
            **payload.model_dump(exclude_unset=True),
        }
        with self.database.write() as session:
            recheck_operator(session, principal)
            state = self.catalog._state(session, principal.owner_id)
            now = int(time.time())

            def work():
                row = session.get(Garment, garment_id)
                if row is None or row.owner_id != principal.owner_id:
                    raise BusinessError("NOT_FOUND", "Garment not found", 404)
                if row.revision != payload.base_revision:
                    raise BusinessError(
                        "REVISION_CONFLICT",
                        "Garment changed; reload before confirming",
                        409,
                    )
                changed = []
                if operation == "restore":
                    validate_restore(session, row, self.assets.originals, now)
                    row.state, row.deleted_at, row.restore_until = "active", None, None
                    changed = ["state", "deleted_at", "restore_until"]
                else:
                    if row.state != "active":
                        raise BusinessError(
                            "TARGET_REMOVED", "Target is no longer active", 409
                        )
                    if operation == "delete":
                        row.state, row.deleted_at, row.restore_until = (
                            "deleted",
                            now,
                            now + RESTORE_SECONDS,
                        )
                        changed = ["state", "deleted_at", "restore_until"]
                    else:
                        patch = payload.patch.model_dump(exclude_unset=True)
                        for name in ("name", "category", "roles", "availability"):
                            if name in patch:
                                setattr(row, name, patch[name])
                                changed.append(name)
                        if "annotation_fields" in patch:
                            fields = caller_fields(payload.patch.annotation_fields)
                            row.annotation_fields = {**row.annotation_fields, **fields}
                            row.annotation_review_state = "published"
                            changed.extend(
                                f"annotation_fields.{field}" for field in fields
                            )
                row.revision += 1
                bump_catalog(session, principal.owner_id)
                receipt = str(uuid4())
                audit(session, principal, action, row.id, receipt)
                return success(
                    {
                        "receipt_id": receipt,
                        "operation": operation,
                        "garment_id": row.id,
                        "base_revision": payload.base_revision,
                        "revision": row.revision,
                        "catalog_revision": state.catalog_revision,
                        "state": row.state,
                        "created_at": row.created_at,
                        "deleted_at": row.deleted_at,
                        "restore_until": row.restore_until,
                        "changed_fields": changed,
                    }
                )

            result = idempotent(session, principal, action, key, fingerprint, work)
        return result

    def update(self, principal, garment_id, payload, key):
        return self._write(principal, garment_id, payload, key, "update")

    def delete(self, principal, garment_id, payload, key):
        return self._write(principal, garment_id, payload, key, "delete")

    def restore(self, principal, garment_id, payload, key):
        return self._write(principal, garment_id, payload, key, "restore")

    def image(self, principal, garment_id, asset_id, variant):
        require_operator(principal)
        garment_id, asset_id = canonical_id(garment_id), canonical_id(asset_id)
        if variant not in {"content", "preview"}:
            raise BusinessError("NOT_FOUND", "Image not found", 404)

        def authorize(session):
            recheck_operator(session, principal)

        loader = self.assets._preview if variant == "preview" else self.assets._content
        result = loader(principal, asset_id, garment_id=garment_id, authorize=authorize)
        # Check again immediately before handing the file to the response layer.
        self.assets._content(
            principal, asset_id, garment_id=garment_id, authorize=authorize
        )
        return result
