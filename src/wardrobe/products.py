"""Product evidence matching and immutable drafts; publication requires ChangeService."""

import re
import time
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select

from .auth import recheck_grant
from .contracts import StrictModel, success
from .errors import BusinessError
from .labels import LabelValues
from .models import Asset, Garment, GarmentAsset, Idempotency, OwnerAsset, ProductDraft
from .writes import audit, digest, idempotent


def normalize(value):
    return " ".join(value.split()).casefold()


class ProductLookup(StrictModel):
    article_number: str = Field(min_length=1, max_length=100)
    page: int = Field(default=1, ge=1, le=1000)
    limit: int = Field(default=20, ge=1, le=20)

    @field_validator("article_number")
    @classmethod
    def nonempty_article(cls, value):
        if not value.strip():
            raise ValueError("Article number cannot be blank")
        return value


class ScreenshotEvidence(StrictModel):
    asset_id: str
    kind: Literal["product_detail", "product_parameters"]
    article_number: str | None = Field(default=None, min_length=1, max_length=100)
    values: LabelValues = Field(default_factory=LabelValues)
    parameters: dict[str, str] = Field(default_factory=dict, max_length=30)

    @field_validator("asset_id")
    @classmethod
    def asset_uuid(cls, value):
        return str(UUID(value))

    @field_validator("parameters")
    @classmethod
    def bounded_parameters(cls, value):
        if any(not k or len(k) > 100 or len(v) > 500 for k, v in value.items()):
            raise ValueError("Bounded parameter names and values required")
        return value


class ProductDraftInput(StrictModel):
    article_number: str = Field(min_length=1, max_length=100)
    global_spu_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    owned_size: str = Field(min_length=1, max_length=100)
    selected_color: str | None = Field(default=None, min_length=1, max_length=100)
    color_note: str | None = Field(default=None, min_length=1, max_length=500)
    sale_attributes: dict[str, str] = Field(default_factory=dict, max_length=10)
    screenshots: list[ScreenshotEvidence] = Field(min_length=2, max_length=2)
    asset_ids: list[str] = Field(min_length=1, max_length=20)
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
    garment_id: str | None = None
    base_revision: int | None = Field(default=None, ge=1)

    @field_validator("asset_ids")
    @classmethod
    def asset_uuids(cls, values):
        return [str(UUID(x)) for x in values]

    @field_validator("garment_id")
    @classmethod
    def garment_uuid(cls, value):
        return str(UUID(value)) if value is not None else value

    @field_validator("sale_attributes")
    @classmethod
    def bounded_attributes(cls, values):
        if any(
            not k.strip() or len(k) > 100 or not v.strip() or len(v) > 100
            for k, v in values.items()
        ):
            raise ValueError("Bounded nonempty sales attributes required")
        return values

    @model_validator(mode="after")
    def consistent(self):
        if self.color_note is not None and not self.color_note.strip():
            raise ValueError("Color explanation cannot be blank")
        if bool(self.garment_id) != bool(self.base_revision):
            raise ValueError("Existing garment and revision must be supplied together")
        if {s.kind for s in self.screenshots} != {
            "product_detail",
            "product_parameters",
        } or len({s.asset_id for s in self.screenshots}) != 2:
            raise ValueError("Two distinct screenshot roles are required")
        if set(self.asset_ids) & {s.asset_id for s in self.screenshots}:
            raise ValueError(
                "Wardrobe images and evidence screenshots must be distinct"
            )
        if not any(s.article_number for s in self.screenshots):
            raise ValueError(
                "At least one screenshot must carry the extracted article number"
            )
        if any(
            s.article_number
            and normalize(s.article_number) != normalize(self.article_number)
            for s in self.screenshots
        ):
            raise ValueError("Screenshot article numbers must match the request")
        if not self.owned_size.strip() or not self.article_number.strip():
            raise ValueError("Article number and owned size cannot be blank")
        return self


class MatchError(BusinessError):
    def __init__(self, code, message, candidates):
        super().__init__(code, message, 409)
        self.candidates = candidates[:20]

    def response(self):
        result = super().response()
        result["error"]["candidates"] = self.candidates
        return result


def match_sku(product, payload):
    matches = []
    for sku in product["skus"]:
        attrs = {normalize(k): normalize(v) for k, v in sku["attributes"].items()}
        sizes = [v for k, v in attrs.items() if k in {"size", "尺码"}]
        colors = [v for k, v in attrs.items() if k in {"color", "colour", "颜色"}]
        if len(sizes) != 1 or sizes[0] != normalize(payload.owned_size):
            continue
        if payload.selected_color is not None and (
            len(colors) != 1 or colors[0] != normalize(payload.selected_color)
        ):
            continue
        if any(
            attrs.get(normalize(k)) != normalize(v)
            for k, v in payload.sale_attributes.items()
        ):
            continue
        matches.append(sku)
    if len(matches) != 1:
        raise MatchError(
            "SKU_NOT_FOUND" if not matches else "SKU_AMBIGUOUS",
            "Provide the exact owned size and sales attributes to select one SKU",
            matches or product["skus"],
        )
    selected = matches[0]
    # Reject additional selectors that contradict the explicit size/color inputs.
    for key, value in payload.sale_attributes.items():
        if normalize(key) in {"size", "尺码"} and normalize(value) != normalize(
            payload.owned_size
        ):
            raise BusinessError(
                "INVALID_ARGUMENT", "Sales size conflicts with owned size", 422
            )
    return selected


def field(value, source, evidence):
    return {
        "value": value,
        "source_kind": source,
        "verification_state": "unverified",
        "evidence": list(evidence),
        "updated_at": int(time.time()),
    }


def build_payload(product, selected, request):
    api_evidence = [
        {
            "provider": product["provider"],
            "region": product["region"],
            "global_spu_id": product["global_spu_id"],
        }
    ]
    fields = {
        "brand": field(product["brand"], "product_api", api_evidence),
        "model": field(product["article_number"], "product_api", api_evidence),
        "subcategory": field(product["category"], "product_api", api_evidence),
    }
    conflicts, blockers = [], []
    for screenshot in request.screenshots:
        for key, value in screenshot.values.model_dump(exclude_unset=True).items():
            if value is None:
                continue  # Unknown extraction is not a field deletion.
            incoming = field(
                value,
                "screenshot_extraction",
                [{"asset_id": screenshot.asset_id, "kind": screenshot.kind}],
            )
            if key in fields:
                if fields[key]["value"] != value:
                    conflicts.append({"field": key, "values": [fields[key], incoming]})
                    if key not in {
                        "brand",
                        "model",
                        "subcategory",
                        "primary_color",
                        "size",
                    }:
                        blockers.append("screenshot_conflict:" + key)
                else:
                    fields[key]["evidence"].extend(incoming["evidence"])
            else:
                fields[key] = incoming
    sales_colors = [
        v
        for k, v in selected["attributes"].items()
        if normalize(k) in {"color", "colour", "颜色"}
    ]
    api_color = sales_colors[0] if len(sales_colors) == 1 else None
    title_colors = re.findall(
        r"卡其色|米白色|米色|白色|黑色|黄色|蓝色|绿色|红色|灰色|紫色|棕色|褐色|橙色|粉色",
        product["title"],
    )
    screenshot_colors = [
        {"value": s.values.primary_color, "asset_id": s.asset_id}
        for s in request.screenshots
        if s.values.primary_color is not None
    ]
    observed_colors = title_colors + [x["value"] for x in screenshot_colors]
    if not api_color and len({normalize(x["value"]) for x in screenshot_colors}) > 1:
        blockers.append("screenshot_conflict:primary_color")
    if api_color and any(normalize(x) != normalize(api_color) for x in observed_colors):
        conflicts.append(
            {
                "field": "primary_color",
                "sales_color": api_color,
                "title_color_hints": title_colors,
                "screenshot_color": fields.get("primary_color"),
                "screenshot_color_claims": screenshot_colors,
                "resolution_note": request.color_note,
            }
        )
        if request.selected_color is None or not request.color_note:
            blockers.append("color_conflict")
    if api_color and "primary_color" not in fields:
        fields["primary_color"] = field(api_color, "product_api", api_evidence)
    screenshot_sizes = [
        {"value": s.values.size, "asset_id": s.asset_id}
        for s in request.screenshots
        if s.values.size is not None
    ]
    if any(
        normalize(s["value"]) != normalize(request.owned_size) for s in screenshot_sizes
    ):
        conflicts.append(
            {
                "field": "size",
                "screenshot_size_claims": screenshot_sizes,
                "owned_size": request.owned_size,
            }
        )
        blockers.append("size_conflict")
    fields["size"] = field(
        request.owned_size, "caller_supplied", [{"basis": "caller_reported_owned_size"}]
    )
    return {
        "name": product["title"],
        "category": product["category"],
        "roles": sorted(set(request.roles)),
        "asset_ids": sorted(set(request.asset_ids)),
        "annotation_fields": fields,
        "screenshots": [s.model_dump(exclude_unset=True) for s in request.screenshots],
        "product_snapshot": product,
        "selected_sku": selected,
        "product_reference": {
            "provider": product["provider"],
            "region": product["region"],
            "article_number": product["article_number"],
            "spu_ids": product["ids"],
            "sku_ids": selected["ids"],
            "sale_attributes": selected["attributes"],
        },
        "conflicts": conflicts,
        "blockers": sorted(set(blockers)),
        "color_note": request.color_note,
        "garment_id": request.garment_id,
        "base_revision": request.base_revision,
    }


def check_assets(session, owner_id, ids, asset_service):
    for identifier in set(ids):
        asset = session.get(Asset, identifier)
        if session.get(OwnerAsset, (owner_id, identifier)) is None or asset is None:
            raise BusinessError(
                "ASSET_NOT_OWNED",
                "Evidence and wardrobe images must belong to this owner",
                422,
            )
        if not (asset_service.originals / asset.sha256).is_file():
            raise BusinessError(
                "ASSET_UNAVAILABLE", "An associated image is unavailable", 409
            )
        associations = list(
            session.scalars(
                select(Garment)
                .join(GarmentAsset)
                .where(
                    GarmentAsset.asset_id == identifier, Garment.owner_id == owner_id
                )
            )
        )
        if associations and not any(g.state == "active" for g in associations):
            raise BusinessError(
                "TARGET_REMOVED", "Image is associated only with removed garments", 409
            )


def existing_garment(session, owner_id, payload):
    if not payload["garment_id"]:
        return None
    row = session.get(Garment, payload["garment_id"])
    if row is None or row.owner_id != owner_id or row.state != "active":
        raise BusinessError("NOT_FOUND", "Garment not found", 404)
    if row.revision != payload["base_revision"]:
        raise BusinessError(
            "REVISION_CONFLICT", "Garment changed; prepare a fresh product draft", 409
        )
    return row


def validate_publication(session, owner_id, draft, asset_service):
    if draft.state != "draft" or draft.expires_at <= int(time.time()):
        raise BusinessError(
            "PRODUCT_DRAFT_UNAVAILABLE", "Product draft is published or expired", 409
        )
    if draft.payload["blockers"]:
        raise BusinessError(
            "PRODUCT_DRAFT_NEEDS_REVIEW",
            "Resolve evidence conflicts in a new draft before publication",
            409,
        )
    check_assets(
        session,
        owner_id,
        draft.payload["asset_ids"]
        + [s["asset_id"] for s in draft.payload["screenshots"]],
        asset_service,
    )
    return existing_garment(session, owner_id, draft.payload)


def publish_product(session, owner_id, draft, asset_service):
    garment = validate_publication(session, owner_id, draft, asset_service)
    payload = draft.payload
    if garment is None:
        garment = Garment(
            id=str(uuid4()),
            owner_id=owner_id,
            name=payload["name"],
            category=payload["category"],
            roles=payload["roles"],
            availability="unknown",
            state="active",
            revision=1,
            annotation_fields={},
            annotation_review_state="published",
            created_at=int(time.time()),
        )
        session.add(garment)
        session.flush()
    else:
        garment.name, garment.category, garment.roles = (
            payload["name"],
            payload["category"],
            payload["roles"],
        )
        garment.revision += 1
    garment.annotation_fields = {
        **garment.annotation_fields,
        **{
            k: v
            for k, v in payload["annotation_fields"].items()
            if garment.annotation_fields.get(k, {}).get("verification_state")
            != "user_verified"
        },
    }
    garment.annotation_review_state = "published"
    garment.product_reference = {
        **payload["product_reference"],
        "draft_id": draft.id,
        "evidence_asset_ids": [s["asset_id"] for s in payload["screenshots"]],
    }
    for asset_id in payload["asset_ids"]:
        if session.get(GarmentAsset, (garment.id, asset_id)) is None:
            session.add(GarmentAsset(garment_id=garment.id, asset_id=asset_id))
    draft.state = "published"
    draft.published_garment_id = garment.id
    return garment


class DisabledProductProvider:
    def lookup(self, *args):
        raise BusinessError(
            "PROVIDER_NOT_CONFIGURED", "No product provider is configured", 503
        )

    def detail(self, *args):
        raise BusinessError(
            "PROVIDER_NOT_CONFIGURED", "No product provider is configured", 503
        )


class ProductService:
    def __init__(self, database, assets, changes, provider=None):
        self.database, self.assets, self.changes, self.provider = (
            database,
            assets,
            changes,
            provider if provider is not None else DisabledProductProvider(),
        )

    @staticmethod
    def _view(row):
        return {
            "draft_id": row.id,
            "revision": row.revision,
            "state": row.state
            if row.expires_at > int(time.time()) or row.state == "published"
            else "expired",
            "expires_at": row.expires_at,
            "effective": row.state == "published",
            "published_garment_id": row.published_garment_id,
            "image_references": [
                {
                    "asset_id": asset_id,
                    "kind": kind,
                    "content_url": f"/v1/product-drafts/{row.id}/assets/{asset_id}/content",
                }
                for asset_id, kind in [
                    (x, "wardrobe") for x in row.payload["asset_ids"]
                ]
                + [(s["asset_id"], s["kind"]) for s in row.payload["screenshots"]]
            ],
            "draft": row.payload,
        }

    def lookup(self, principal, payload):
        principal.require("wardrobe.read")
        with self.database.sessions.begin() as session:
            recheck_grant(session, principal, required_scope="wardrobe.read")
        result = self.provider.lookup(
            payload.article_number, payload.page, payload.limit
        )
        with self.database.sessions.begin() as session:
            recheck_grant(session, principal, required_scope="wardrobe.read")
        return success(result)

    def prepare(self, principal, payload, key):
        principal.require("wardrobe.annotate")
        if not 1 <= len(key) <= 128:
            raise BusinessError(
                "IDEMPOTENCY_KEY_REQUIRED", "A bounded idempotency key is required", 422
            )
        request = payload.model_dump(exclude_unset=True)
        with self.database.sessions.begin() as session:
            recheck_grant(session, principal, required_scope="wardrobe.annotate")
            saved = session.get(
                Idempotency,
                (principal.owner_id, principal.client_id, "product_draft", key),
            )
            if saved is not None:
                if saved.digest != digest(request):
                    raise BusinessError(
                        "IDEMPOTENCY_CONFLICT",
                        "This key was used for different arguments",
                        409,
                    )
                return saved.response
            check_assets(
                session,
                principal.owner_id,
                payload.asset_ids + [s.asset_id for s in payload.screenshots],
                self.assets,
            )
            existing_garment(
                session,
                principal.owner_id,
                {
                    "garment_id": payload.garment_id,
                    "base_revision": payload.base_revision,
                },
            )
        product = self.provider.detail(payload.global_spu_id)
        if normalize(product["article_number"]) != normalize(payload.article_number):
            raise BusinessError(
                "PRODUCT_MISMATCH",
                "Selected product article number does not exactly match the evidence",
                409,
            )
        selected = match_sku(product, payload)
        data = build_payload(product, selected, payload)
        with self.database.write() as session:
            recheck_grant(session, principal, required_scope="wardrobe.annotate")

            def work():
                check_assets(
                    session,
                    principal.owner_id,
                    payload.asset_ids + [s.asset_id for s in payload.screenshots],
                    self.assets,
                )
                target = existing_garment(session, principal.owner_id, data)
                data["preserved_verified_fields"] = (
                    {
                        k: v
                        for k, v in target.annotation_fields.items()
                        if v.get("verification_state") == "user_verified"
                    }
                    if target
                    else {}
                )
                for k, old in data["preserved_verified_fields"].items():
                    if k in data["annotation_fields"] and data["annotation_fields"][k][
                        "value"
                    ] != old.get("value"):
                        data["conflicts"].append(
                            {
                                "field": k,
                                "existing": old,
                                "suggestion": data["annotation_fields"][k],
                                "resolution": "preserve_existing_verified",
                            }
                        )
                now = int(time.time())
                row = ProductDraft(
                    id=str(uuid4()),
                    owner_id=principal.owner_id,
                    client_id=principal.client_id,
                    revision=1,
                    state="draft",
                    payload=data,
                    created_at=now,
                    expires_at=now + 86400,
                )
                session.add(row)
                audit(session, principal, "prepare_product_draft", row.id, str(uuid4()))
                return success(self._view(row))

            return idempotent(session, principal, "product_draft", key, request, work)

    def get(self, principal, draft_id):
        principal.require("wardrobe.read")
        try:
            identifier = str(UUID(draft_id))
        except (ValueError, TypeError, AttributeError):
            raise BusinessError(
                "INVALID_ARGUMENT", "A product draft UUID is required", 422
            ) from None
        with self.database.sessions.begin() as session:
            row = session.get(ProductDraft, identifier)
            if row is None or row.owner_id != principal.owner_id:
                raise BusinessError("NOT_FOUND", "Product draft not found", 404)
            return success(self._view(row))

    def content(self, principal, draft_id, asset_id):
        principal.require("wardrobe.read")
        with self.database.sessions.begin() as session:
            recheck_grant(session, principal, required_scope="wardrobe.read")
            row = session.get(ProductDraft, draft_id)
            if row is None or row.owner_id != principal.owner_id:
                raise BusinessError("NOT_FOUND", "Product draft not found", 404)
            screenshots = {s["asset_id"] for s in row.payload["screenshots"]}
            if asset_id not in screenshots | set(row.payload["asset_ids"]):
                raise BusinessError("NOT_FOUND", "Image is not part of this draft", 404)
            if row.state == "draft":
                if row.expires_at <= int(time.time()):
                    raise BusinessError(
                        "PRODUCT_DRAFT_UNAVAILABLE", "Draft preview has expired", 409
                    )
                existing_garment(session, principal.owner_id, row.payload)
            else:
                garment = session.get(Garment, row.published_garment_id)
                if (
                    garment is None
                    or garment.owner_id != principal.owner_id
                    or garment.state != "active"
                ):
                    raise BusinessError(
                        "NOT_FOUND", "Published garment is unavailable", 404
                    )
                if asset_id in screenshots:
                    if (
                        not garment.product_reference
                        or garment.product_reference.get("draft_id") != row.id
                    ):
                        raise BusinessError(
                            "NOT_FOUND", "Evidence is no longer current", 404
                        )
                elif session.get(GarmentAsset, (garment.id, asset_id)) is None:
                    raise BusinessError(
                        "NOT_FOUND", "Image is no longer associated", 404
                    )
            check_assets(session, principal.owner_id, [asset_id], self.assets)
            asset = session.get(Asset, asset_id)
            return self.assets.originals / asset.sha256, asset.mime

    def propose(self, principal, draft_id, key):
        from .changes import Change, ProposalInput

        principal.require("wardrobe.propose_change")
        try:
            identifier = str(UUID(draft_id))
        except (ValueError, TypeError, AttributeError):
            raise BusinessError(
                "INVALID_ARGUMENT", "A product draft UUID is required", 422
            ) from None
        with self.database.sessions.begin() as session:
            row = session.get(ProductDraft, identifier)
            if row is None or row.owner_id != principal.owner_id:
                raise BusinessError("NOT_FOUND", "Product draft not found", 404)
            # Draft contents are immutable at revision 1; publication increments it.
            # Retrying the original proposal key must retain the original arguments.
            revision = 1
        return self.changes.propose(
            principal,
            ProposalInput(
                changes=[
                    Change(
                        target_type="product_draft",
                        target_id=draft_id,
                        base_revision=revision,
                        operation="publish_product",
                    )
                ]
            ),
            key,
        )
