"""Authority for intake drafts and create decisions; no model has create authority."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from uuid import uuid4

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from pydantic import ValidationError
from sqlalchemy import func, select

from .auth import _unique_json, recheck_grant
from .contracts import success
from .errors import BusinessError
from .image_previews import PreviewStore, private_directory
from .intake_contracts import IntakeAssertion, IntakeFields
from .models import (
    Asset,
    Garment,
    GarmentAsset,
    GarmentIntake,
    GarmentIntakeOrigin,
    IntakeCommitAttempt,
    IntakeDecision,
    IntakeGCJob,
    IntakeImage,
    IntakeObservation,
    IntakeProductEnrichment,
    IntakeSourceObject,
    IntakeUpload,
    OwnerAsset,
    TrustedIssuer,
)
from .writes import audit, bump_catalog, canonical, digest, idempotent

TTL = 7 * 86400
TERMINAL = {"submitted", "closed", "expired"}


def fail(code, message="Intake request cannot be completed", status=409):
    raise BusinessError(code, message, status)


def _decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _json(value):
    return json.loads(
        value,
        object_pairs_hook=_unique_json,
        parse_constant=lambda _: fail("INVALID_CONFIRMATION", status=403),
    )


class IntakeService:
    def __init__(self, database, root, upload_key, permit=None, *, enabled=True):
        self.database, self.root, self.key = database, Path(root), upload_key
        self.enabled = enabled
        self.files = self.root / "intake" / "objects"
        self.temporary = self.root / "intake" / "tmp"
        private_directory(self.root / "intake")
        for path in (self.files, self.temporary):
            private_directory(path)
        self.permit = permit

    def _write(self):
        return self.database.write()

    def _auth(self, session, p, scope="wardrobe.intake"):
        p.require(
            scope,
            channel="human_adapter" if scope == "human.intake_create" else "agent",
        )
        recheck_grant(session, p, required_scope=scope)

    def _task(self, session, p, identifier, mutable=False):
        row = session.get(GarmentIntake, identifier)
        if not row or row.owner_id != p.owner_id or row.client_id != p.client_id:
            fail("INTAKE_NOT_FOUND", "Intake not found", 404)
        if mutable:
            if not self.enabled:
                fail("INTAKE_DISABLED", "New intake writes are disabled", 503)
            if row.state in TERMINAL:
                fail("INTAKE_CLOSED" if row.state != "expired" else "INTAKE_EXPIRED")
            if row.expires_at <= int(time.time()):
                fail("INTAKE_EXPIRED")
            if row.state == "commit_pending":
                fail("COMMIT_OUTCOME_UNKNOWN")
        return row

    def _cas(self, row, revision):
        if row.revision != revision:
            fail("REVISION_CONFLICT")

    def _images(self, s, row):
        result = []
        for im in s.scalars(
            select(IntakeImage)
            .where(IntakeImage.task_id == row.id)
            .order_by(IntakeImage.ordinal)
        ):
            src = s.get(IntakeSourceObject, im.source_object_id)
            result.append(
                {
                    "image_id": im.image_id,
                    "ordinal": im.ordinal,
                    "role": im.role,
                    "source_kind": im.source_kind,
                    "source_object_id": src.id,
                    "asset_id": src.asset_id,
                    "source_sha256": src.sha256,
                    "view_sha256": src.view_sha256,
                    "validation": {
                        "decode_validation": "client_reported",
                        "view_validation": "server_decoded",
                    },
                    "preview_url": f"/v1/intakes/{row.id}/images/{im.image_id}/preview",
                }
            )
        return result

    def _duplicates(self, s, row):
        hashes = {x["source_sha256"] for x in self._images(s, row)}
        if not hashes:
            return []
        return sorted(
            set(
                s.scalars(
                    select(Garment.id)
                    .join(GarmentAsset)
                    .join(Asset)
                    .where(
                        Garment.owner_id == row.owner_id,
                        Garment.state == "active",
                        Asset.sha256.in_(hashes),
                    )
                )
            )
        )

    def _view(self, s, row):
        images = self._images(s, row)
        return {
            "task_id": row.id,
            "origin_scope": row.origin_scope,
            "state": row.state,
            "revision": row.revision,
            "image_revision": row.image_revision,
            "fields": row.payload,
            "field_sources": row.field_sources,
            "images": images,
            "input_fingerprint": digest(images),
            "expires_at": row.expires_at,
            "duplicates": self._duplicates(s, row),
            "receipt": row.receipt,
            **(row.frozen or {}),
        }

    def _edit(self, row, user=False):
        row.revision += 1
        row.frozen = None
        row.state = "review" if row.image_revision else "collecting"
        if user:
            row.last_user_edit_at = int(time.time())
            row.expires_at = row.last_user_edit_at + TTL

    def create(self, p, payload, key):
        if not self.enabled:
            fail("INTAKE_DISABLED", "New intake writes are disabled", 503)
        with self._write() as s:
            self._auth(s, p)

            def work():
                existing = s.scalar(
                    select(GarmentIntake).where(
                        GarmentIntake.owner_id == p.owner_id,
                        GarmentIntake.client_id == p.client_id,
                        GarmentIntake.origin_scope == payload.origin_scope,
                    )
                )
                if existing is not None:
                    return success(self._view(s, existing))
                now = int(time.time())
                row = GarmentIntake(
                    id=str(uuid4()),
                    owner_id=p.owner_id,
                    client_id=p.client_id,
                    origin_scope=payload.origin_scope,
                    state="collecting",
                    revision=1,
                    image_revision=0,
                    payload={},
                    field_sources={},
                    created_at=now,
                    last_user_edit_at=now,
                    expires_at=now + TTL,
                )
                s.add(row)
                s.flush()
                return success(self._view(s, row))

            return idempotent(s, p, "intake_create", key, payload.model_dump(), work)

    def get(self, p, identifier):
        with self.database.sessions.begin() as s:
            self._auth(s, p)
            return success(self._view(s, self._task(s, p, identifier)))

    def patch(self, p, identifier, payload):
        with self._write() as s:
            self._auth(s, p)
            row = self._task(s, p, identifier, True)
            self._cas(row, payload.expected_revision)
            fields = payload.fields.model_dump(exclude_unset=True)
            row.payload = {**row.payload, **fields}
            row.field_sources = {
                **row.field_sources,
                **{
                    k: {
                        "source_kind": "caller_supplied",
                        "verification_state": "unverified",
                        "evidence_hash": digest(
                            {"field": k, "value": v, "task_id": row.id}
                        ),
                    }
                    for k, v in fields.items()
                },
            }
            self._edit(row, True)
            return success(self._view(s, row))

    def prepare(self, p, identifier, payload, key):
        with self._write() as s:
            self._auth(s, p, "wardrobe.intake_upload")
            row = self._task(s, p, identifier, True)

            def work():
                self._cas(row, payload.expected_revision)
                count = s.scalar(
                    select(func.count())
                    .select_from(IntakeUpload)
                    .where(
                        IntakeUpload.task_id == row.id,
                        IntakeUpload.state == "pending",
                        IntakeUpload.expires_at > int(time.time()),
                    )
                )
                if count >= 2:
                    fail("STORAGE_QUOTA_EXCEEDED", "At most two uploads may be pending")
                used = s.scalar(
                    select(func.coalesce(func.sum(IntakeUpload.size), 0))
                    .join(GarmentIntake)
                    .where(
                        GarmentIntake.state.not_in(TERMINAL),
                        IntakeUpload.expires_at > int(time.time()),
                    )
                )
                task_size = s.scalar(
                    select(func.coalesce(func.sum(IntakeUpload.size), 0)).where(
                        IntakeUpload.task_id == row.id, IntakeUpload.kind == "source"
                    )
                )
                disk_bytes = sum(
                    path.stat().st_size
                    for path in self.files.iterdir()
                    if path.is_file() and not path.is_symlink()
                )
                if max(used, disk_bytes) + payload.size > 512 * 1024**2 or (
                    payload.kind == "source"
                    and task_size + payload.size > 100 * 1024**2
                ):
                    fail("STORAGE_QUOTA_EXCEEDED")
                upload = IntakeUpload(
                    id=str(uuid4()),
                    task_id=row.id,
                    image_id=payload.image_id,
                    kind=payload.kind,
                    sha256=payload.sha256,
                    size=payload.size,
                    metadata_json=payload.model_dump(),
                    state="pending",
                    expires_at=int(time.time()) + 900,
                )
                s.add(upload)
                s.flush()
                return success(self._upload_view(upload))

            result = idempotent(
                s,
                p,
                "intake_upload",
                key,
                payload.model_dump() | {"task_id": identifier},
                work,
            )
        return {
            **result,
            "data": {
                **result["data"],
                "upload_token": self._token(result["data"]["upload_id"]),
            },
        }

    def _token(self, uid):
        return hmac.new(
            self.key, ("intake-upload/1:" + uid).encode(), hashlib.sha256
        ).hexdigest()

    def _upload_view(self, u):
        return {
            "upload_id": u.id,
            "state": u.state,
            "sha256": u.sha256,
            "size": u.size,
            "expires_at": u.expires_at,
            "upload_url": f"/v1/intakes/{u.task_id}/uploads/{u.id}/content",
            "required_header": "X-Wardrobe-Upload-Token",
        }

    def _upload(self, s, task_id, uid):
        u = s.get(IntakeUpload, uid)
        if not u or u.task_id != task_id:
            fail("INTAKE_NOT_FOUND", status=404)
        if u.expires_at <= int(time.time()):
            fail("UPLOAD_EXPIRED")
        return u

    def upload_status(self, p, identifier, uid):
        with self.database.sessions.begin() as s:
            self._auth(s, p, "wardrobe.intake_upload")
            self._task(s, p, identifier)
            return success(self._upload_view(self._upload(s, identifier, uid)))

    def check_upload(self, p, identifier, uid, token):
        with self.database.sessions.begin() as s:
            self._auth(s, p, "wardrobe.intake_upload")
            self._task(s, p, identifier, True)
            if not hmac.compare_digest(token, self._token(uid)):
                fail("INVALID_UPLOAD_GRANT", status=403)
            return self._upload(s, identifier, uid).size

    def accept_file(self, p, identifier, uid, token, path):
        self.check_upload(p, identifier, uid, token)
        with path.open("rb") as file:
            sha = hashlib.file_digest(file, "sha256").hexdigest()
        head = (
            path.read_bytes()[:64]
            if path.stat().st_size < 64
            else path.open("rb").read(64)
        )
        mime = (
            "image/jpeg"
            if head.startswith(b"\xff\xd8\xff")
            else "image/png"
            if head.startswith(b"\x89PNG\r\n\x1a\n")
            else None
        )
        if (
            mime is None
            and len(head) >= 16
            and head[4:8] == b"ftyp"
            and any(x in head[8:64] for x in (b"heic", b"heix", b"mif1"))
        ):
            mime = "image/heif" if b"mif1" == head[8:12] else "image/heic"
        with self._write() as s:
            self._auth(s, p, "wardrobe.intake_upload")
            self._task(s, p, identifier, True)
            u = self._upload(s, identifier, uid)
            if sha != u.sha256 or path.stat().st_size != u.size:
                fail("UPLOAD_INCOMPLETE")
            if mime != u.metadata_json["mime"]:
                fail("SOURCE_FORMAT_UNSUPPORTED", status=422)
            from .intake_sources import validate_source_header

            validate_source_header(
                path, mime, u.metadata_json["width"], u.metadata_json["height"]
            )
            if u.kind == "view":
                from PIL import Image

                try:
                    with Image.open(path, formats=["JPEG"]) as image:
                        if (
                            image.size
                            != (u.metadata_json["width"], u.metadata_json["height"])
                            or max(image.size) > 2560
                            or image.width * image.height > 12000000
                        ):
                            fail("VIEW_INVALID", status=422)
                except (OSError, ValueError):
                    fail("VIEW_INVALID", status=422)
            target = self.files / sha
            if target.exists():
                if (
                    target.is_symlink()
                    or hashlib.sha256(target.read_bytes()).hexdigest() != sha
                ):
                    fail("UPLOAD_INCOMPLETE")
            else:
                path.chmod(0o600)
                os.replace(path, target)
            if u.kind == "view":
                PreviewStore(self.root).get(sha, source_path=target)
            u.state = "ready"
            return success(self._upload_view(u))

    def complete(self, p, identifier, payload):
        with self._write() as s:
            self._auth(s, p, "wardrobe.intake_upload")
            row = self._task(s, p, identifier, True)
            a = self._upload(s, identifier, payload.source_upload_id)
            b = self._upload(s, identifier, payload.view_upload_id)
            if (
                a.kind != "source"
                or b.kind != "view"
                or a.image_id != b.image_id
                or a.state != "ready"
                or b.state != "ready"
            ):
                fail("UPLOAD_INCOMPLETE")
            old = s.get(IntakeImage, (row.id, a.image_id))
            if old:
                src = s.get(IntakeSourceObject, old.source_object_id)
                if src.sha256 == a.sha256 and src.view_sha256 == b.sha256:
                    return success(self._view(s, row))
                fail("REVISION_CONFLICT")
            self._cas(row, payload.expected_revision)
            if b.metadata_json.get("source_sha256") != a.sha256:
                fail("VIEW_INVALID", "View must bind its original source hash", 422)
            for k in ("role", "ordinal", "source_kind"):
                if a.metadata_json[k] != b.metadata_json[k]:
                    fail("VIEW_INVALID", status=422)
            if (
                s.scalar(
                    select(func.count())
                    .select_from(IntakeImage)
                    .where(IntakeImage.task_id == row.id)
                )
                >= 5
            ):
                fail("STORAGE_QUOTA_EXCEEDED")
            if s.scalar(
                select(IntakeImage).where(
                    IntakeImage.task_id == row.id,
                    IntakeImage.ordinal == a.metadata_json["ordinal"],
                )
            ):
                fail("REVISION_CONFLICT")
            asset = s.scalar(select(Asset).where(Asset.sha256 == a.sha256))
            previous_source = (
                s.scalar(
                    select(IntakeSourceObject).where(
                        IntakeSourceObject.asset_id == asset.id
                    )
                )
                if asset
                else None
            )
            server_decoded = asset is not None and (
                previous_source is None
                or previous_source.report.get("asset_validation_mode")
                == "server_decoded"
            )
            if asset is None:
                asset = Asset(
                    id=str(uuid4()),
                    sha256=a.sha256,
                    size=a.size,
                    mime=a.metadata_json["mime"],
                    width=a.metadata_json["width"],
                    height=a.metadata_json["height"],
                )
                s.add(asset)
                s.flush()
            src = IntakeSourceObject(
                id=str(uuid4()),
                task_id=row.id,
                owner_id=p.owner_id,
                asset_id=asset.id,
                sha256=a.sha256,
                view_sha256=b.sha256,
                report={
                    "recipe": payload.recipe,
                    "asset_validation_mode": "server_decoded"
                    if server_decoded
                    else "client_decoded",
                    "decode_validation": "client_reported",
                    "dimensions_source": "client_reported",
                    "metadata": a.metadata_json,
                },
                state="draft",
            )
            s.add(src)
            s.flush()
            s.add(
                IntakeImage(
                    task_id=row.id,
                    image_id=a.image_id,
                    ordinal=a.metadata_json["ordinal"],
                    role=a.metadata_json["role"],
                    source_kind=a.metadata_json["source_kind"],
                    source_object_id=src.id,
                )
            )
            s.flush()
            row.image_revision += 1
            self._edit(row, True)
            return success(self._view(s, row))

    def observe(self, p, identifier, payload):
        with self._write() as s:
            self._auth(s, p, "wardrobe.intake_observe")
            row = self._task(s, p, identifier, True)
            images = self._images(s, row)
            if (
                payload.image_revision != row.image_revision
                or payload.input_fingerprint != digest(images)
            ):
                fail("REVISION_CONFLICT")
            candidate = payload.model_dump(exclude={"expected_revision"})
            for previous in s.scalars(
                select(IntakeObservation).where(
                    IntakeObservation.task_id == row.id,
                    IntakeObservation.input_fingerprint == payload.input_fingerprint,
                    IntakeObservation.producer == payload.producer,
                )
            ):
                if {
                    k: v
                    for k, v in previous.payload.items()
                    if k != "expected_revision"
                } == candidate:
                    return success(self._view(s, row))
            self._cas(row, payload.expected_revision)
            allowed = (
                {"brand", "article_number", "size", "composition"}
                if payload.producer == "apple_vision"
                else {
                    "category",
                    "roles",
                    "sleeve",
                    "primary_color",
                    "secondary_colors",
                    "color_distribution",
                }
            )
            if set(payload.fields) - allowed:
                fail("INVALID_ARGUMENT", "Observer may not supply those facts", 422)
            values = IntakeFields.model_validate(payload.fields).model_dump(
                exclude_unset=True
            )
            evidence = payload.ocr
            if payload.producer == "apple_vision":
                if payload.schema_version != "wardrobe-label-ocr/1" or not evidence:
                    fail("OCR_UNCERTAIN", status=422)
                matched = next(
                    (
                        x
                        for x in images
                        if x["role"] == "label"
                        and x["image_id"] == evidence.get("image_id")
                        and x["source_sha256"] == evidence.get("source_hash")
                        and x["view_sha256"] == evidence.get("view_hash")
                    ),
                    None,
                )
                if not matched or len(canonical(evidence)) > 65536:
                    fail("OCR_UNCERTAIN", status=422)
                from .intake_sources import validate_ocr_geometry

                validate_ocr_geometry(evidence)
                raw = evidence.get("raw_text", "")
                if not isinstance(raw, str) or len(raw.encode()) > 16384:
                    fail("OCR_UNCERTAIN", status=422)
                # Only explicit keyed text can become structured OCR candidates.
                import re

                keys = {
                    "brand": r"(?:品牌|brand)",
                    "article_number": r"(?:货号|款号|style\s*(?:no\.?|number)|article\s*(?:no\.?|number))",
                    "size": r"(?:尺码|size)",
                    "composition": r"(?:成分|composition)",
                }
                for key, value in values.items():
                    if value is not None and not re.search(
                        r"^[ \t]*"
                        + keys[key]
                        + r"[ \t]*[:：][ \t]*"
                        + re.escape(value)
                        + r"[ \t]*$",
                        raw,
                        re.IGNORECASE | re.MULTILINE,
                    ):
                        fail(
                            "OCR_UNCERTAIN",
                            "OCR candidate must follow an explicit label key",
                            422,
                        )
            elif payload.schema_version != "wardrobe-intake-vision/1":
                fail("INVALID_ARGUMENT", status=422)
            srcs = dict(row.field_sources)
            fields = dict(row.payload)
            for key, val in values.items():
                source = {
                    "source_kind": "label_ocr"
                    if payload.producer == "apple_vision"
                    else "visual_observation",
                    "verification_state": "unverified",
                    "source_image_id": evidence.get("image_id")
                    if payload.producer == "apple_vision"
                    else None,
                    "evidence_hash": digest(
                        {
                            "input": payload.input_fingerprint,
                            "field": key,
                            "value": val,
                            "ocr": evidence,
                        }
                    ),
                }
                if srcs.get(key, {}).get("source_kind") == "caller_supplied":
                    srcs[key] = {
                        **srcs[key],
                        "conflicting_candidate": {"value": val, **source},
                    }
                else:
                    fields[key] = val
                    srcs[key] = source
            row.payload = fields
            row.field_sources = srcs
            s.add(
                IntakeObservation(
                    id=str(uuid4()),
                    task_id=row.id,
                    input_fingerprint=payload.input_fingerprint,
                    producer=payload.producer,
                    payload=payload.model_dump(),
                )
            )
            self._edit(row)
            return success(self._view(s, row))

    def freeze(self, p, identifier, payload):
        with self._write() as s:
            self._auth(s, p)
            row = self._task(s, p, identifier, True)
            self._cas(row, payload.expected_revision)
            images = self._images(s, row)
            if not any(x["role"] == "garment" for x in images) or not row.payload.get(
                "name"
            ):
                fail(
                    "UPLOAD_INCOMPLETE",
                    "A named garment and a garment image are required",
                )
            row.revision += 1
            row.state = "frozen"
            frozen = {
                "payload_digest": digest(
                    {
                        "fields": row.payload,
                        "field_sources": row.field_sources,
                        "images": images,
                    }
                ),
                "duplicate_set_digest": digest(self._duplicates(s, row)),
                "nonce": secrets.token_urlsafe(24),
                "freeze_expires_at": min(row.expires_at, int(time.time()) + 900),
                "field_digests": {
                    k: {
                        "value_digest": digest(v),
                        "evidence_hash": row.field_sources[k]["evidence_hash"],
                    }
                    for k, v in row.payload.items()
                },
            }
            row.frozen = frozen
            return success(self._view(s, row))

    def _assertion(self, s, p, identifier, signed):
        try:
            head, body, sig = signed.split(".")
            header = _json(_decode(head))
            claims = IntakeAssertion.model_validate(_json(_decode(body)))
            if (
                set(header) != {"alg", "typ", "kid"}
                or header["alg"] != "EdDSA"
                or header["typ"] != "wardrobe-intake-human/1"
            ):
                raise ValueError()
            issuer = s.get(TrustedIssuer, (claims.iss, header["kid"]))
            now = int(time.time())
            if (
                issuer is None
                or issuer.revoked_at is not None
                or issuer.expires_at <= now
            ):
                raise ValueError()
            if (
                issuer.audience != claims.aud
                or "intake_create" not in issuer.purposes
                or {"sub": claims.sub, "owner_id": p.owner_id, "client_id": p.client_id}
                not in issuer.bindings
            ):
                raise ValueError()
            if (
                claims.owner_id != p.owner_id
                or claims.client_id != p.client_id
                or claims.task_id != identifier
            ):
                raise ValueError()
            public = load_pem_public_key(issuer.public_key.encode())
            if not isinstance(public, Ed25519PublicKey):
                raise TypeError()
            public.verify(_decode(sig), (head + "." + body).encode())
            return claims
        except (
            ValueError,
            KeyError,
            TypeError,
            UnicodeError,
            ValidationError,
            InvalidSignature,
        ):
            fail("INVALID_CONFIRMATION", status=403)

    def _frozen_check(self, s, row, c):
        now = int(time.time())
        f = row.frozen or {}
        if (
            c.exp <= now
            or c.iat > now + 30
            or c.exp - c.iat > 120
            or c.iat > c.exp
            or c.exp > f.get("freeze_expires_at", 0)
        ):
            fail("INVALID_CONFIRMATION", status=403)
        if (
            row.revision != c.revision
            or f.get("nonce") != c.nonce
            or f.get("payload_digest") != c.payload_digest
            or f.get("duplicate_set_digest") != c.duplicate_set_digest
        ):
            fail("INVALID_CONFIRMATION", status=403)
        duplicates = self._duplicates(s, row)
        if digest(duplicates) != c.duplicate_set_digest:
            fail("DUPLICATE_SET_CHANGED")
        if sorted(set(c.duplicate_overrides)) != duplicates:
            fail("DUPLICATE_SET_CHANGED")
        paths = set()
        for att in c.attested_fields:
            field = att.field_path.removeprefix("fields.")
            if field in paths:
                fail("INVALID_CONFIRMATION", status=403)
            paths.add(field)
            source = row.field_sources.get(field, {})
            if (
                source.get("source_kind") not in {"caller_supplied", "label_ocr"}
                or f.get("field_digests", {}).get(field)
                != {
                    "value_digest": att.value_digest,
                    "evidence_hash": att.evidence_hash,
                }
                or att.input_revision != row.image_revision
            ):
                fail("INVALID_CONFIRMATION", status=403)

    def decide(self, p, identifier, signed):
        fingerprint = digest(signed)
        with self._write() as s:
            self._auth(s, p, "human.intake_create")
            row = self._task(s, p, identifier)
            # A byte-identical already committed request is a readback, even
            # after its issuer/nonce lifetime. It still requires a live grant
            # bound to the same task owner/business client.
            try:
                parsed = IntakeAssertion.model_validate(
                    _json(_decode(signed.split(".")[1]))
                )
            except (IndexError, ValueError, TypeError, ValidationError, UnicodeError):
                fail("INVALID_CONFIRMATION", status=403)
            saved = s.get(IntakeDecision, (parsed.iss, parsed.jti))
            if (
                saved
                and saved.task_id == identifier
                and saved.assertion_digest == fingerprint
            ):
                return success(saved.receipt)
            c = self._assertion(s, p, identifier, signed)
            if not self.enabled and c.decision == "approve":
                fail("INTAKE_DISABLED", "New intake writes are disabled", 503)
            previous = s.get(IntakeDecision, (c.iss, c.jti))
            if previous:
                if (
                    previous.assertion_digest != fingerprint
                    or previous.task_id != identifier
                ):
                    fail("INVALID_CONFIRMATION", status=403)
                return success(previous.receipt)
            attempt = s.get(IntakeCommitAttempt, c.jti)
            if attempt:
                if (
                    attempt.task_id != identifier
                    or attempt.signed_request_hash != fingerprint
                ):
                    fail("INVALID_CONFIRMATION", status=403)
                if attempt.state == "committed":
                    return success(attempt.receipt)
                if attempt.state == "expired_aborted":
                    fail("INVALID_CONFIRMATION", status=403)
            if row.state not in {"frozen", "commit_pending"} or row.expires_at <= int(
                time.time()
            ):
                fail("INTAKE_CLOSED")
            self._frozen_check(s, row, c)
            if c.decision != "approve":
                row.state = "closed"
                row.revision += 1
                row.frozen = None
                receipt = {
                    "receipt_id": str(uuid4()),
                    "task_id": row.id,
                    "state": "closed",
                    "decision": c.decision,
                }
                row.receipt = receipt
                s.add(
                    IntakeDecision(
                        issuer=c.iss,
                        jti=c.jti,
                        task_id=row.id,
                        assertion_digest=fingerprint,
                        receipt=receipt,
                        created_at=int(time.time()),
                    )
                )
                return success(receipt)
            if not attempt:
                attempt = IntakeCommitAttempt(
                    jti=c.jti,
                    task_id=identifier,
                    signed_request_hash=fingerprint,
                    revision=row.revision,
                    state="prepared",
                    permit={"expires_at": c.exp},
                )
                s.add(attempt)
            row.state = "commit_pending"
            request = {
                "task_id": identifier,
                "jti": c.jti,
                "revision": row.revision,
                "payload_digest": c.payload_digest,
                "origin_scope": row.origin_scope,
            }
        # No database write transaction or lock survives the fixed-origin callback.
        permit = (self.permit or self._remote_permit)(request)
        now = int(time.time())
        if (
            any(permit.get(k) != v for k, v in request.items())
            or type(permit.get("generation")) is not int
            or not now < permit.get("expires_at", 0) <= now + 120
        ):
            fail("COMMIT_OUTCOME_UNKNOWN")
        with self._write() as s:
            self._auth(s, p, "human.intake_create")
            row = self._task(s, p, identifier)
            attempt = s.get(IntakeCommitAttempt, c.jti)
            if attempt.state == "committed":
                return success(attempt.receipt)
            if (
                attempt.state == "expired_aborted"
                or row.state != "commit_pending"
                or row.expires_at <= int(time.time())
            ):
                fail("INTAKE_CLOSED")
            self._assertion(s, p, identifier, signed)
            self._frozen_check(s, row, c)
            if permit["expires_at"] <= int(time.time()):
                fail("COMMIT_OUTCOME_UNKNOWN")
            attempt.permit = permit
            attempt.state = "permit_granted"
            return success(self._commit(s, p, row, c, attempt, fingerprint))

    def _commit(self, s, p, row, c, attempt, fingerprint):
        attested = {a.field_path.removeprefix("fields.") for a in c.attested_fields}
        receipt = self._persist_garment(s, p, row, attested)
        attempt.state = "committed"
        attempt.receipt = receipt
        s.add(
            IntakeDecision(
                issuer=c.iss,
                jti=c.jti,
                task_id=row.id,
                assertion_digest=fingerprint,
                receipt=receipt,
                created_at=int(time.time()),
            )
        )
        return receipt

    def _persist_garment(self, s, p, row, attested, *, assurance=None):
        """Shared persistence only; callers must validate their approval protocol.

        The adapter validates its signature, permit and IntakeDecision before
        entering this function in the same write transaction.
        """
        gid = str(uuid4())
        now = int(time.time())
        fields = {}
        for k, v in row.payload.items():
            if k in {"name", "category", "roles", "article_number"}:
                continue
            source = row.field_sources[k]
            fields["seasons" if k == "season" else k] = {
                "value": [v] if k == "season" else v,
                "source_kind": "user_supplied"
                if source["source_kind"] == "caller_supplied"
                else source["source_kind"],
                "verification_state": "user_verified"
                if k in attested
                else "unverified",
                "evidence": [source["evidence_hash"]],
                "updated_at": now,
                **(
                    {"verification_basis": assurance}
                    if k in attested and assurance
                    else {}
                ),
            }
        identity = {}
        if row.payload.get("article_number"):
            identity = {
                "schema_version": "wardrobe-identity/1",
                "article_number": {
                    "value": row.payload["article_number"],
                    **row.field_sources["article_number"],
                    "verification_state": "user_verified"
                    if "article_number" in attested
                    else "unverified",
                    **(
                        {"verification_basis": assurance}
                        if "article_number" in attested and assurance
                        else {}
                    ),
                },
            }
        garment = Garment(
            id=gid,
            owner_id=p.owner_id,
            name=row.payload["name"],
            category=row.payload.get("category"),
            roles=row.payload.get("roles") or [],
            availability="unknown",
            state="active",
            revision=1,
            annotation_fields=fields,
            identity_fields=identity,
            annotation_review_state="initial",
            created_at=now,
        )
        s.add(garment)
        s.flush()
        images = self._images(s, row)
        linked = set()
        for image in images:
            source = s.get(IntakeSourceObject, image["source_object_id"])
            source.state = "submitted"
            original = self.root / "assets" / "original" / source.sha256
            private_directory(self.root / "assets")
            private_directory(original.parent)
            if not original.exists():
                # Hard-link immutable CAS bytes: no lossy rewrite, and no view registered as Asset.
                os.link(self.files / source.sha256, original)
            if image["role"] == "garment" and source.asset_id not in linked:
                linked.add(source.asset_id)
                if not s.get(OwnerAsset, (p.owner_id, source.asset_id)):
                    s.add(OwnerAsset(owner_id=p.owner_id, asset_id=source.asset_id))
                s.add(GarmentAsset(garment_id=gid, asset_id=source.asset_id))
        receipt = {
            "receipt_id": str(uuid4()),
            "task_id": row.id,
            "garment_id": gid,
            "state": "submitted",
            "field_sources": fields,
            "identity_fields": identity,
            **({"approval_assurance": assurance} if assurance else {}),
        }
        row.state = "submitted"
        row.revision += 1
        row.published_garment_id = gid
        row.receipt = receipt
        s.add(
            GarmentIntakeOrigin(
                garment_id=gid, task_id=row.id, sources=images, receipt=receipt
            )
        )
        audit(s, p, "intake_create", gid, receipt["receipt_id"])
        bump_catalog(s, p.owner_id)
        return receipt

    def _remote_permit(self, body):
        key = os.environ.get("WARDROBE_INTAKE_PERMIT_KEY", "")
        try:
            if len(key) != 64:
                raise ValueError()
            key = bytes.fromhex(key)
            if len(key) != 32:
                raise ValueError()
        except ValueError:
            fail(
                "COMMIT_OUTCOME_UNKNOWN", "Trusted commit permit is not configured", 503
            )
        path = "/internal/wardrobe/intake-permits"
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(24)
        raw = canonical(body)
        signed = "\n".join(
            ["POST", path, timestamp, nonce, hashlib.sha256(raw).hexdigest()]
        ).encode()
        signature = hmac.new(key, signed, hashlib.sha256).hexdigest()
        try:
            with httpx.Client(
                timeout=10, follow_redirects=False, trust_env=False
            ) as client:
                response = client.post(
                    "https://agent.example.com" + path,
                    content=raw,
                    headers={
                        "Content-Type": "application/json",
                        "X-Wardrobe-Timestamp": timestamp,
                        "X-Wardrobe-Nonce": nonce,
                        "X-Wardrobe-Signature": signature,
                    },
                )
                response.raise_for_status()
                value = response.json()
                permit = value["permit"]
                expected = hmac.new(key, canonical(permit), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected, value["signature"]):
                    raise ValueError()
                return permit
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            fail("COMMIT_OUTCOME_UNKNOWN", "Commit permit outcome is unknown", 503)

    def close(self, p, identifier):
        with self._write() as s:
            self._auth(s, p)
            row = self._task(s, p, identifier)
            if row.state == "submitted":
                return success(row.receipt)
            if row.state in {"closed", "expired"}:
                return success(self._view(s, row))
            row.state = "closed"
            row.revision += 1
            row.frozen = None
            for a in s.scalars(
                select(IntakeCommitAttempt).where(
                    IntakeCommitAttempt.task_id == row.id,
                    IntakeCommitAttempt.state != "committed",
                )
            ):
                a.state = "expired_aborted"
            for src in s.scalars(
                select(IntakeSourceObject).where(IntakeSourceObject.task_id == row.id)
            ):
                if s.get(IntakeGCJob, src.id) is None:
                    s.add(
                        IntakeGCJob(
                            source_id=src.id,
                            grace_until=int(time.time()) + 86400,
                            state="pending",
                            retries=0,
                        )
                    )
            return success(self._view(s, row))

    def attempt(self, p, identifier, jti, resolve=None):
        with self._write() as s:
            self._auth(
                s,
                p,
                "human.intake_create"
                if p.channel == "human_adapter"
                else "wardrobe.intake",
            )
            row = self._task(s, p, identifier)
            a = s.get(IntakeCommitAttempt, jti)
            if not a or a.task_id != identifier:
                fail("INTAKE_NOT_FOUND", status=404)
            if resolve is not None:
                if p.channel != "human_adapter":
                    fail("SCOPE_DENIED", status=403)
                deadline = (a.permit or {}).get(
                    "expires_at",
                    (row.frozen or {}).get("freeze_expires_at", row.expires_at),
                )
                if a.state != "committed" and (
                    resolve.origin_deleted or deadline <= int(time.time())
                ):
                    a.state = "expired_aborted"
                    row.state = "closed" if resolve.origin_deleted else "review"
                    row.revision += 1
                    row.frozen = None
            return success(
                {
                    "jti": a.jti,
                    "task_id": identifier,
                    "state": a.state,
                    "permit": a.permit,
                    "receipt": a.receipt,
                }
            )

    def preview(self, p, identifier, image_id):
        with self.database.sessions.begin() as s:
            self._auth(s, p)
            row = self._task(s, p, identifier)
            if row.state in {"closed", "expired"} or row.expires_at <= int(time.time()):
                fail("INTAKE_CLOSED")
            image = s.get(IntakeImage, (identifier, image_id))
            if image is None:
                fail("INTAKE_NOT_FOUND", status=404)
            src = s.get(IntakeSourceObject, image.source_object_id)
            sha = src.view_sha256
        path = PreviewStore(self.root).get(sha, source_path=self.files / sha)
        with self.database.sessions.begin() as s:
            self._auth(s, p)
            row = self._task(s, p, identifier)
            if row.state in {"closed", "expired"}:
                fail("INTAKE_CLOSED")
        return path, "image/jpeg"

    def receipt(self, p, receipt_id):
        with self.database.sessions.begin() as s:
            self._auth(s, p)
            for row in s.scalars(
                select(GarmentIntake).where(
                    GarmentIntake.owner_id == p.owner_id,
                    GarmentIntake.client_id == p.client_id,
                    GarmentIntake.state == "submitted",
                )
            ):
                if row.receipt.get("receipt_id") == receipt_id:
                    return success(row.receipt)
            fail("INTAKE_NOT_FOUND", status=404)

    def source_content(self, p, gid, source_id):
        with self.database.sessions.begin() as s:
            self._auth(s, p, "wardrobe.read")
            g = s.get(Garment, gid)
            src = s.get(IntakeSourceObject, source_id)
            origin = s.get(GarmentIntakeOrigin, gid)
            if (
                not g
                or g.owner_id != p.owner_id
                or g.state != "active"
                or not src
                or not origin
                or src.task_id != origin.task_id
            ):
                fail("INTAKE_NOT_FOUND", status=404)
            asset = s.get(Asset, src.asset_id)
            path = self.root / "assets" / "original" / src.sha256
            if path.is_symlink() or not path.is_file():
                fail("ASSET_UNAVAILABLE", "Source is temporarily unavailable", 503)
            return path, asset.mime

    def draft_source(self, p, identifier, image_id, kind="source"):
        with self.database.sessions.begin() as s:
            self._auth(s, p, "wardrobe.intake_upload")
            row = self._task(s, p, identifier)
            if row.state in {"closed", "expired"} or row.expires_at <= int(time.time()):
                fail("INTAKE_CLOSED")
            image = s.get(IntakeImage, (identifier, image_id))
            if not image:
                fail("INTAKE_NOT_FOUND", status=404)
            src = s.get(IntakeSourceObject, image.source_object_id)
            asset = s.get(Asset, src.asset_id)
            # Analysis is the validated view, never a label or the opaque source.
            if kind == "analysis" and image.role != "garment":
                fail("SCOPE_DENIED", status=403)
            if kind == "analysis":
                return PreviewStore(self.root).get(
                    src.view_sha256, source_path=self.files / src.view_sha256
                ), "image/jpeg"
            path = self.files / src.sha256
            if path.is_symlink() or not path.is_file():
                fail("ASSET_UNAVAILABLE", "Source is temporarily unavailable", 503)
            return path, asset.mime

    def by_origin(self, p, origin):
        with self.database.sessions.begin() as s:
            self._auth(s, p)
            row = s.scalar(
                select(GarmentIntake).where(
                    GarmentIntake.owner_id == p.owner_id,
                    GarmentIntake.client_id == p.client_id,
                    GarmentIntake.origin_scope == origin,
                )
            )
            if row is None:
                fail("INTAKE_NOT_FOUND", status=404)
            return success(self._view(s, row))

    def remove_image(self, p, identifier, image_id, payload):
        with self._write() as s:
            self._auth(s, p, "wardrobe.intake_upload")
            row = self._task(s, p, identifier, True)
            self._cas(row, payload.expected_revision)
            image = s.get(IntakeImage, (identifier, image_id))
            if image is None:
                fail("INTAKE_NOT_FOUND", status=404)
            # Removing evidence cannot leave an apparently current candidate.
            fields, sources = dict(row.payload), dict(row.field_sources)
            for field, source in list(sources.items()):
                invalid = (
                    source.get("source_kind") in {"label_ocr", "product_info_ocr"}
                    and image.role in {"label", "product_info"}
                    and source.get("source_image_id", image_id) == image_id
                ) or (
                    source.get("source_kind") == "visual_observation"
                    and image.role == "garment"
                )
                if invalid:
                    fields.pop(field, None)
                    sources.pop(field, None)
                elif "conflicting_candidate" in source:
                    sources[field] = {
                        k: v for k, v in source.items() if k != "conflicting_candidate"
                    }
            row.payload, row.field_sources = fields, sources
            sid = image.source_object_id
            s.delete(image)
            s.flush()
            if s.get(IntakeGCJob, sid) is None:
                s.add(
                    IntakeGCJob(
                        source_id=sid,
                        grace_until=int(time.time()) + 86400,
                        state="pending",
                        retries=0,
                    )
                )
            row.image_revision += 1
            self._edit(row, True)
            return success(self._view(s, row))

    def collect(self, now=None):
        """Explicit local GC: isolated intake CAS only, never historical originals.

        Every active association is rechecked inside BEGIN IMMEDIATE. Quarantine
        rename and unlink are serialized with new intake source associations.
        A crash after rename is resumed from the fixed hash quarantine path.
        """
        now = int(time.time()) if now is None else now
        quarantine = self.root / "intake" / "quarantine"
        private_directory(quarantine)
        report = {
            "expired": 0,
            "collected": 0,
            "retained": 0,
            "removed_objects": 0,
            "retained_objects": 0,
        }
        with self._write() as s:
            for row in s.scalars(
                select(GarmentIntake).where(
                    GarmentIntake.state.not_in(TERMINAL),
                    GarmentIntake.expires_at <= now,
                )
            ):
                if row.state == "commit_pending":
                    continue  # reconcile authorized attempts before expiry
                enrichment = s.get(IntakeProductEnrichment, row.id)
                if enrichment is not None:
                    enrichment.updated_at = now
                row.state = "expired"
                row.revision += 1
                row.frozen = None
                report["expired"] += 1
            for src in s.scalars(
                select(IntakeSourceObject)
                .join(GarmentIntake)
                .where(
                    GarmentIntake.state.in_(["closed", "expired"]),
                    IntakeSourceObject.state == "draft",
                )
            ):
                if s.get(IntakeGCJob, src.id) is None:
                    s.add(
                        IntakeGCJob(
                            source_id=src.id,
                            grace_until=now + 86400,
                            state="pending",
                            retries=0,
                        )
                    )
            s.flush()
            from .intake_retention import finish_collection, protected_hashes

            protected = protected_hashes(s, now)
            for job in s.scalars(
                select(IntakeGCJob).where(
                    IntakeGCJob.state == "pending", IntakeGCJob.grace_until <= now
                )
            ):
                src = s.get(IntakeSourceObject, job.source_id)
                if src.state == "submitted":
                    job.state = "retained"
                    report["retained"] += 1
                    continue
                row = s.get(GarmentIntake, src.task_id)
                image = s.scalar(
                    select(IntakeImage).where(IntakeImage.source_object_id == src.id)
                )
                if image is not None and row.state not in {"closed", "expired"}:
                    report["retained"] += 1
                    continue
                for sha in {src.sha256, src.view_sha256}:
                    # Existing/or submitted original CAS is never a GC target.
                    # Preserve its linked intake copy as well rather than infer ownership.
                    original = self.root / "assets" / "original" / sha
                    refs = list(
                        s.scalars(
                            select(IntakeSourceObject).where(
                                IntakeSourceObject.id != src.id,
                                (IntakeSourceObject.sha256 == sha)
                                | (IntakeSourceObject.view_sha256 == sha),
                                IntakeSourceObject.state != "collected",
                            )
                        )
                    )
                    active = False
                    for other in refs:
                        task = s.get(GarmentIntake, other.task_id)
                        if other.state == "submitted" or task.state not in {
                            "closed",
                            "expired",
                        }:
                            active = True
                            break
                    upload = s.scalar(
                        select(IntakeUpload).where(
                            IntakeUpload.sha256 == sha, IntakeUpload.expires_at > now
                        )
                    )
                    if sha in protected or original.exists() or active or upload:
                        report["retained_objects"] += 1
                        continue
                    source = self.files / sha
                    target = quarantine / sha
                    if source.is_symlink() or target.is_symlink():
                        fail("STORAGE_QUOTA_EXCEEDED", "Unsafe intake storage", 503)
                    if source.exists():
                        os.replace(source, target)
                    target.unlink(missing_ok=True)
                    report["removed_objects"] += 1
                src.state = "collected"
                job.state = "done"
                report["collected"] += 1
            # Expired partial uploads have no source row: collect only intake files
            # with no source, view or unexpired upload holder, under the same lock.
            for path in self.files.iterdir():
                sha = path.name
                if (
                    not __import__("re").fullmatch("[0-9a-f]{64}", sha)
                    or path.is_symlink()
                ):
                    continue
                if sha in protected:
                    continue
                if s.scalar(
                    select(IntakeSourceObject.id).where(
                        (
                            (IntakeSourceObject.sha256 == sha)
                            | (IntakeSourceObject.view_sha256 == sha)
                        ),
                        IntakeSourceObject.state != "collected",
                    )
                ):
                    continue
                if s.scalar(
                    select(IntakeUpload.id).where(
                        IntakeUpload.sha256 == sha,
                        IntakeUpload.expires_at > now - 86400,
                    )
                ):
                    continue
                if (self.root / "assets" / "original" / sha).exists():
                    continue
                target = quarantine / sha
                os.replace(path, target)
                target.unlink(missing_ok=True)
            finish_collection(s, self.files, now)
        return report

    def upload_by_key(self, p, identifier, key):
        from .models import Idempotency

        with self.database.sessions.begin() as s:
            self._auth(s, p, "wardrobe.intake_upload")
            self._task(s, p, identifier)
            saved = s.get(Idempotency, (p.owner_id, p.client_id, "intake_upload", key))
            if saved is None:
                fail("INTAKE_NOT_FOUND", status=404)
            uid = saved.response["data"]["upload_id"]
            u = self._upload(s, identifier, uid)
            return success({**self._upload_view(u), "upload_token": self._token(uid)})
