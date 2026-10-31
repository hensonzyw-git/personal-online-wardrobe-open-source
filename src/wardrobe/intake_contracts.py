"""Strict wire contracts for the private, single-garment intake channel."""

from typing import Literal

from pydantic import Field, model_validator

from .contracts import StrictModel

SHA = r"^[0-9a-f]{64}$"


class IntakeCreate(StrictModel):
    origin_scope: str = Field(min_length=1, max_length=128)


class IntakeFields(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=100)
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
    color_distribution: str | None = Field(default=None, max_length=300)
    primary_color: str | None = Field(default=None, max_length=60)
    secondary_colors: list[str] | None = Field(default=None, max_length=5)
    thickness: Literal["thin", "medium", "thick", "very_thick"] | None = None
    season: Literal["spring_summer", "autumn_winter", "all_year"] | None = None
    sleeve: str | None = Field(default=None, max_length=100)
    brand: str | None = Field(default=None, max_length=100)
    article_number: str | None = Field(default=None, max_length=100)
    size: str | None = Field(default=None, max_length=100)
    composition: str | None = Field(default=None, max_length=300)


class IntakePatch(StrictModel):
    expected_revision: int = Field(ge=1)
    fields: IntakeFields


class IntakeRevision(StrictModel):
    expected_revision: int = Field(ge=1)


class IntakeUploadInput(IntakeRevision):
    image_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    ordinal: int = Field(ge=1, le=5)
    role: Literal["garment", "label"]
    source_kind: Literal["owned_photo", "product_image"] = "owned_photo"
    kind: Literal["source", "view"]
    sha256: str = Field(pattern=SHA)
    size: int = Field(gt=0, le=20 * 1024**2)
    mime: Literal["image/jpeg", "image/png", "image/heic", "image/heif"]
    width: int = Field(gt=0, le=40000000)
    height: int = Field(gt=0, le=40000000)
    source_sha256: str | None = Field(default=None, pattern=SHA)

    @model_validator(mode="after")
    def bounds(self):
        if self.width * self.height > 40000000:
            raise ValueError("Source exceeds pixel limit")
        if self.kind == "view" and (
            self.mime != "image/jpeg"
            or self.size > 8 * 1024**2
            or max(self.width, self.height) > 2560
            or self.width * self.height > 12000000
        ):
            raise ValueError("Invalid bounded view")
        return self


class IntakeComplete(IntakeRevision):
    source_upload_id: str = Field(max_length=36)
    view_upload_id: str = Field(max_length=36)
    recipe: Literal["pa-intake-view/1"] = "pa-intake-view/1"


class IntakeObserve(IntakeRevision):
    image_revision: int = Field(ge=1)
    input_fingerprint: str = Field(pattern=SHA)
    producer: Literal["apple_vision", "intake_visual"]
    schema_version: Literal["wardrobe-label-ocr/1", "wardrobe-intake-vision/1"]
    fields: dict = Field(default_factory=dict)
    ocr: dict | None = None


class IntakeDecisionInput(StrictModel):
    assertion: str = Field(min_length=1, max_length=32768)


class Attestation(StrictModel):
    field_path: str = Field(max_length=100)
    value_digest: str = Field(pattern=SHA)
    evidence_hash: str = Field(pattern=SHA)
    user_action_id: str = Field(min_length=1, max_length=128)
    input_revision: int = Field(ge=0)


class IntakeAssertion(StrictModel):
    iss: str = Field(min_length=1, max_length=100)
    sub: str = Field(min_length=1, max_length=100)
    owner_id: str = Field(max_length=36)
    client_id: str = Field(max_length=100)
    actor_subject: str = Field(min_length=1, max_length=128)
    aud: Literal["wardrobe:intake-create"]
    purpose: Literal["intake_create"]
    task_id: str = Field(max_length=36)
    revision: int = Field(ge=1)
    payload_digest: str = Field(pattern=SHA)
    duplicate_set_digest: str = Field(pattern=SHA)
    duplicate_overrides: list[str] = Field(default_factory=list, max_length=1000)
    attested_fields: list[Attestation] = Field(default_factory=list, max_length=30)
    nonce: str = Field(max_length=100)
    jti: str = Field(min_length=1, max_length=100)
    decision: Literal["approve", "reject", "cancel"]
    iat: int
    exp: int


class IntakeResolve(StrictModel):
    origin_deleted: bool = False
