import base64
import hashlib
import hmac
import json
from uuid import UUID

from sqlalchemy import select

from .catalog_import import evidence_summary
from .contracts import ListQuery, Principal, success
from .errors import BusinessError
from .image_previews import image_preview_response
from .labels import LabelValues
from .models import Asset, Garment, GarmentAsset, ServiceState


class CatalogService:
    def __init__(self, session_factory, cursor_key: bytes, originals=None):
        self.sessions = session_factory
        self.cursor_key = cursor_key
        self.originals = originals

    def _encode(self, value: dict) -> str:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        signed = hmac.digest(self.cursor_key, raw, hashlib.sha256)
        return base64.urlsafe_b64encode(signed + raw).decode().rstrip("=")

    def _decode(self, cursor: str) -> dict:
        try:
            raw = base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
            )
            signature, value = raw[:32], raw[32:]
            if not hmac.compare_digest(
                signature, hmac.digest(self.cursor_key, value, hashlib.sha256)
            ):
                raise TypeError()
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise TypeError()
            return parsed
        except (ValueError, TypeError, UnicodeError):
            raise BusinessError(
                "INVALID_CURSOR", "Cursor is invalid for this query", 422
            ) from None

    @staticmethod
    def _view(row: Garment, session, originals=None) -> dict:
        assets = list(
            session.scalars(
                select(Asset)
                .join(GarmentAsset)
                .where(GarmentAsset.garment_id == row.id)
                .order_by(Asset.id)
            )
        )
        from .models import GarmentIntakeOrigin, IntakeSourceObject

        sources = {}
        for asset in assets:
            source = session.scalar(
                select(IntakeSourceObject)
                .join(
                    GarmentIntakeOrigin,
                    GarmentIntakeOrigin.task_id == IntakeSourceObject.task_id,
                )
                .where(
                    GarmentIntakeOrigin.garment_id == row.id,
                    IntakeSourceObject.asset_id == asset.id,
                    IntakeSourceObject.owner_id == row.owner_id,
                    IntakeSourceObject.state == "submitted",
                )
            )
            if source:
                mode = source.report.get("asset_validation_mode", "client_decoded")
                sources[asset.id] = {
                    "source_reference": {
                        "source_object_id": source.id,
                        "content_url": f"/v1/garments/{row.id}/sources/{source.id}/content",
                    },
                    "validation": {
                        "mode": mode,
                        "dimensions_source": "server_decoded"
                        if mode == "server_decoded"
                        else "client_reported",
                        "view_validation": "server_decoded",
                    },
                }
        missing = [
            key
            for key, value in [("category", row.category), ("roles", row.roles)]
            if not value
        ]
        if (
            not assets
            or originals is not None
            and any(not (originals / asset.sha256).is_file() for asset in assets)
        ):
            missing.append("image")
        return {
            "id": row.id,
            "name": row.name,
            "category": row.category,
            "roles": row.roles,
            "availability": row.availability,
            "revision": row.revision,
            "state": row.state,
            "created_at": row.created_at,
            "deleted_at": row.deleted_at,
            "restore_until": row.restore_until,
            "identity_fields": row.identity_fields,
            "annotation_fields": {
                key: row.annotation_fields.get(
                    key,
                    {
                        "value": None,
                        "source_kind": "unknown",
                        "verification_state": "unknown",
                        "evidence": [],
                    },
                )
                for key in LabelValues.model_fields
            },
            "annotation_schema_version": "wardrobe-labels/1",
            "annotation_review_state": row.annotation_review_state,
            "product_reference": row.product_reference,
            "product_evidence_summary": evidence_summary(row, session),
            "recommendation_eligible": not missing
            and row.availability != "unavailable"
            and row.state == "active",
            "missing_requirements": missing,
            "image_references": [
                {
                    "asset_id": a.id,
                    **sources.get(a.id, {}),
                    "mime": a.mime,
                    "width": a.width,
                    "height": a.height,
                    "content_url": f"/v1/assets/{a.id}/content",
                }
                for a in assets
            ],
            "image_access_status": "available" if "image" not in missing else "missing",
        }

    @staticmethod
    def _state(session, owner_id: str) -> ServiceState:
        state = session.get(ServiceState, owner_id)
        if state is None:
            raise BusinessError(
                "SERVICE_NOT_INITIALIZED", "Owner catalog is not initialized", 503
            )
        return state

    @image_preview_response
    def list_garments(self, principal: Principal, query: ListQuery) -> dict:
        principal.require("wardrobe.read")
        with self.sessions.begin() as session:
            state = self._state(session, principal.owner_id)
            binding = {
                "v": 1,
                "owner": principal.owner_id,
                "category": query.category,
                "limit": query.limit,
            }
            last = None
            if query.cursor:
                cursor = self._decode(query.cursor)
                if set(cursor) != {*binding, "revision", "last"} or any(
                    cursor.get(k) != v for k, v in binding.items()
                ):
                    raise BusinessError(
                        "INVALID_CURSOR", "Cursor is invalid for this query", 422
                    )
                if cursor["revision"] != state.catalog_revision:
                    raise BusinessError(
                        "CURSOR_STALE",
                        "Catalog changed; restart from the first page",
                        409,
                    )
                last = cursor["last"]
            stmt = select(Garment).where(
                Garment.owner_id == principal.owner_id, Garment.state == "active"
            )
            if query.category is not None:
                stmt = stmt.where(Garment.category == query.category)
            if last is not None:
                stmt = stmt.where(Garment.id > last)
            rows = list(
                session.scalars(stmt.order_by(Garment.id).limit(query.limit + 1))
            )
            has_more = len(rows) > query.limit
            items = rows[: query.limit]
            next_cursor = (
                self._encode(
                    {
                        **binding,
                        "revision": state.catalog_revision,
                        "last": items[-1].id,
                    }
                )
                if has_more
                else None
            )
            return success(
                {
                    "items": [self._view(x, session, self.originals) for x in items],
                    "next_cursor": next_cursor,
                    "has_more": has_more,
                    "catalog_revision": state.catalog_revision,
                }
            )

    @image_preview_response
    def get_garment(self, principal: Principal, garment_id: str) -> dict:
        principal.require("wardrobe.read")
        try:
            identifier = str(UUID(garment_id))
        except (ValueError, TypeError, AttributeError):
            raise BusinessError(
                "INVALID_ARGUMENT", "A garment UUID is required", 422
            ) from None
        with self.sessions.begin() as session:
            state = self._state(session, principal.owner_id)
            row = session.scalar(
                select(Garment).where(
                    Garment.id == identifier,
                    Garment.owner_id == principal.owner_id,
                    Garment.state == "active",
                )
            )
            if row is None:
                raise BusinessError("NOT_FOUND", "Garment not found", 404)
            return success(
                {
                    "garment": self._view(row, session, self.originals),
                    "catalog_revision": state.catalog_revision,
                }
            )
