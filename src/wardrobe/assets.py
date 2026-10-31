import hashlib
import hmac
import os
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from pydantic import Field
from sqlalchemy import select

from .auth import recheck_grant
from .contracts import StrictModel, success
from .errors import BusinessError
from .image_previews import PreviewStore
from .labels import LabelValues, caller_fields
from .models import Asset, Garment, GarmentAsset, IntakeSourceObject, OwnerAsset, Upload
from .writes import audit, idempotent

MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 40_000_000


class UploadInput(StrictModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(gt=0, le=MAX_BYTES)


class AssetService:
    def __init__(self, database, root: Path, upload_key: bytes):
        self.database, self.root, self.key = database, root, upload_key
        self.originals = root / "assets" / "original"
        self.temporary = root / "tmp"
        for directory in (self.originals, self.temporary):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)

    def _token(self, upload_id: str) -> str:
        return hmac.new(
            self.key, ("wardrobe-upload/1:" + upload_id).encode(), hashlib.sha256
        ).hexdigest()

    def prepare(self, principal, payload: UploadInput, key: str):
        principal.require("wardrobe.create")
        with self.database.write() as session:
            recheck_grant(session, principal, required_scope="wardrobe.create")

            def work():
                row = Upload(
                    id=str(uuid4()),
                    owner_id=principal.owner_id,
                    client_id=principal.client_id,
                    sha256=payload.sha256,
                    size=payload.size,
                    expires_at=int(time.time()) + 900,
                    state="pending",
                )
                session.add(row)
                return success(
                    {
                        "upload_id": row.id,
                        "expires_at": row.expires_at,
                        "upload_url": f"/v1/asset-uploads/{row.id}/content",
                        "required_header": "X-Wardrobe-Upload-Token",
                    }
                )

            value = idempotent(
                session, principal, "prepare_upload", key, payload.model_dump(), work
            )
        # Token is derived for this purpose and never persisted in response/audit JSON.
        return {
            **value,
            "data": {
                **value["data"],
                "upload_token": self._token(value["data"]["upload_id"]),
            },
        }

    def _upload(self, session, principal, upload_id: str):
        row = session.get(Upload, upload_id)
        if (
            row is None
            or row.owner_id != principal.owner_id
            or row.client_id != principal.client_id
        ):
            raise BusinessError("NOT_FOUND", "Upload not found", 404)
        if row.expires_at <= int(time.time()):
            raise BusinessError("UPLOAD_EXPIRED", "Create a new upload intent", 409)
        return row

    def status(self, principal, upload_id):
        principal.require("wardrobe.create")
        with self.database.sessions.begin() as session:
            row = session.get(Upload, upload_id)
            if (
                row is None
                or row.owner_id != principal.owner_id
                or row.client_id != principal.client_id
            ):
                raise BusinessError("NOT_FOUND", "Upload not found", 404)
            return success(
                {
                    "upload_id": row.id,
                    "state": row.state,
                    "asset_id": row.asset_id,
                    "sha256": row.sha256,
                    "expires_at": row.expires_at,
                }
            )

    def check_upload(self, principal, upload_id: str, token: str):
        principal.require("wardrobe.create")
        if not hmac.compare_digest(token or "", self._token(upload_id)):
            raise BusinessError(
                "INVALID_UPLOAD_GRANT", "Upload authorization is invalid", 403
            )
        with self.database.sessions.begin() as session:
            row = self._upload(session, principal, upload_id)
            return row.size

    def accept_file(self, principal, upload_id: str, token: str, path: Path):
        self.check_upload(principal, upload_id, token)
        size = path.stat().st_size
        with path.open("rb") as source:
            actual_hash = hashlib.file_digest(source, "sha256").hexdigest()
        try:
            with Image.open(path, formats=["PNG", "JPEG"]) as image:
                width, height = image.size
                if width * height > MAX_PIXELS:
                    raise BusinessError(
                        "IMAGE_TOO_LARGE", "Decoded image exceeds the pixel limit", 422
                    )
                mime = "image/png" if image.format == "PNG" else "image/jpeg"
                image.verify()
            with Image.open(path, formats=["PNG", "JPEG"]) as image:
                image.load()
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombError,
        ):
            raise BusinessError(
                "INVALID_IMAGE", "Image cannot be fully decoded", 422
            ) from None
        with self.database.write() as session:
            recheck_grant(session, principal, required_scope="wardrobe.create")
            row = self._upload(session, principal, upload_id)
            if size != row.size or actual_hash != row.sha256:
                raise BusinessError(
                    "ASSET_CHECKSUM_MISMATCH",
                    "Image does not match the upload intent",
                    422,
                )
            target = self.originals / actual_hash
            if target.exists():
                with target.open("rb") as existing:
                    if (
                        hashlib.file_digest(existing, "sha256").hexdigest()
                        != actual_hash
                    ):
                        raise BusinessError(
                            "ASSET_CORRUPT", "Stored image integrity check failed", 503
                        )
            else:
                path.chmod(0o600)
                os.replace(path, target)
                descriptor = os.open(self.originals, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            asset = session.scalar(select(Asset).where(Asset.sha256 == actual_hash))
            if asset is None:
                asset = Asset(
                    id=str(uuid4()),
                    sha256=actual_hash,
                    size=size,
                    mime=mime,
                    width=width,
                    height=height,
                )
                session.add(asset)
                session.flush()
            if session.get(OwnerAsset, (principal.owner_id, asset.id)) is None:
                session.add(OwnerAsset(owner_id=principal.owner_id, asset_id=asset.id))
            row.state, row.asset_id = "ready", asset.id
            return success(
                {
                    "upload_id": row.id,
                    "asset_id": asset.id,
                    "sha256": asset.sha256,
                    "size": asset.size,
                    "state": "ready",
                }
            )

    def content(self, principal, asset_id: str):
        principal.require("wardrobe.read")
        with self.database.sessions.begin() as session:
            asset = session.scalar(
                select(Asset)
                .join(GarmentAsset, GarmentAsset.asset_id == Asset.id)
                .join(Garment, Garment.id == GarmentAsset.garment_id)
                .where(
                    Asset.id == asset_id,
                    Garment.owner_id == principal.owner_id,
                    Garment.state == "active",
                )
            )
            if asset is None:
                raise BusinessError("NOT_FOUND", "Image not available", 404)
            path = self.originals / asset.sha256
            if not path.is_file():
                raise BusinessError(
                    "ASSET_UNAVAILABLE", "Image is temporarily unavailable", 503
                )
            return path, asset.mime

    def preview(self, principal, asset_id: str):
        def authorized_source():
            principal.require("wardrobe.read")
            with self.database.sessions.begin() as session:
                recheck_grant(session, principal, required_scope="wardrobe.read")
                asset = session.scalar(
                    select(Asset)
                    .join(GarmentAsset, GarmentAsset.asset_id == Asset.id)
                    .join(Garment, Garment.id == GarmentAsset.garment_id)
                    .where(
                        Asset.id == asset_id,
                        Garment.owner_id == principal.owner_id,
                        Garment.state == "active",
                    )
                )
                if asset is None:
                    raise BusinessError("NOT_FOUND", "Image not available", 404)
                if not (self.originals / asset.sha256).is_file():
                    raise BusinessError(
                        "ASSET_UNAVAILABLE", "Image is temporarily unavailable", 503
                    )
                source = session.scalar(
                    select(IntakeSourceObject).where(
                        IntakeSourceObject.asset_id == asset.id,
                        IntakeSourceObject.owner_id == principal.owner_id,
                        IntakeSourceObject.state == "submitted",
                        IntakeSourceObject.report["asset_validation_mode"].as_string()
                        == "client_decoded",
                    )
                )
                return asset.sha256, source.view_sha256 if source else None

        sha256, view = authorized_source()
        path = (
            PreviewStore(self.root).get(
                view, source_path=self.root / "intake" / "objects" / view
            )
            if view
            else PreviewStore(self.root).get(sha256)
        )
        if authorized_source() != (sha256, view):
            raise BusinessError(
                "ASSET_UNAVAILABLE", "Image changed during generation", 503
            )
        return path, "image/jpeg"


ROLES = Literal[
    "upper", "lower", "footwear", "outerwear", "base_layer", "accessory", "one_piece"
]


class GarmentInput(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    roles: list[ROLES] = Field(default_factory=list, max_length=8)
    availability: Literal["available", "unavailable", "unknown"] = "unknown"
    asset_ids: list[str] = Field(min_length=1, max_length=20)
    annotation_fields: LabelValues = Field(default_factory=LabelValues)


def create_garment(database, principal, payload: GarmentInput, key: str):
    principal.require("wardrobe.create")
    with database.write() as session:
        recheck_grant(session, principal, required_scope="wardrobe.create")

        def work():
            if any(
                session.get(OwnerAsset, (principal.owner_id, asset_id)) is None
                for asset_id in set(payload.asset_ids)
            ):
                raise BusinessError(
                    "ASSET_NOT_OWNED",
                    "Only this owner's verified assets can be attached",
                    422,
                )
            fields = caller_fields(payload.annotation_fields)
            row = Garment(
                id=str(uuid4()),
                owner_id=principal.owner_id,
                name=payload.name,
                category=payload.category,
                roles=sorted(set(payload.roles)),
                availability=payload.availability,
                state="active",
                revision=1,
                annotation_fields=fields,
                annotation_review_state="initial",
                created_at=int(time.time()),
            )
            session.add(row)
            session.flush()
            for asset_id in set(payload.asset_ids):
                session.add(GarmentAsset(garment_id=row.id, asset_id=asset_id))
            from .writes import bump_catalog

            bump_catalog(session, principal.owner_id)
            receipt_id = str(uuid4())
            audit(session, principal, "create_garment", row.id, receipt_id)
            duplicates = (
                select(GarmentAsset.garment_id)
                .join(Garment, Garment.id == GarmentAsset.garment_id)
                .where(
                    Garment.owner_id == principal.owner_id,
                    GarmentAsset.asset_id.in_(payload.asset_ids),
                    GarmentAsset.garment_id != row.id,
                )
                .limit(1)
            )
            return success(
                {
                    "garment_id": row.id,
                    "revision": 1,
                    "receipt_id": receipt_id,
                    "possible_duplicate": bool(session.scalar(duplicates)),
                }
            )

        return idempotent(
            session,
            principal,
            "create_garment",
            key,
            {
                **payload.model_dump(),
                "annotation_fields": payload.annotation_fields.model_dump(
                    exclude_unset=True
                ),
            },
            work,
        )
