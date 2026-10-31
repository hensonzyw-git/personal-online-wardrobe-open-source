"""Versioned values; evidence and factual verification are service-owned."""

import time
from typing import Literal

from pydantic import Field

from .contracts import StrictModel


class LabelValues(StrictModel):
    brand: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    subcategory: str | None = Field(default=None, max_length=100)
    primary_color: str | None = Field(default=None, max_length=60)
    secondary_colors: list[str] | None = Field(default=None, max_length=5)
    color_distribution: str | None = Field(default=None, max_length=300)
    lightness: Literal["light", "medium", "dark"] | None = None
    saturation: Literal["low", "medium", "high"] | None = None
    pattern: list[str] | None = Field(default=None, max_length=5)
    pattern_density: Literal["low", "medium", "high"] | None = None
    visual_focus: str | None = Field(default=None, max_length=300)
    material: str | None = Field(default=None, max_length=300)
    composition: str | None = Field(default=None, max_length=300)
    texture: str | None = Field(default=None, max_length=200)
    thickness: Literal["thin", "medium", "thick", "very_thick"] | None = None
    warmth: Literal["low", "medium", "high"] | None = None
    breathability: Literal["low", "medium", "high"] | None = None
    windproof: bool | None = None
    waterproof: bool | None = None
    silhouette: str | None = Field(default=None, max_length=100)
    length: str | None = Field(default=None, max_length=100)
    sleeve: str | None = Field(default=None, max_length=100)
    neckline: str | None = Field(default=None, max_length=100)
    closure: str | None = Field(default=None, max_length=100)
    fit: str | None = Field(default=None, max_length=100)
    shoe_shape: str | None = Field(default=None, max_length=100)
    style: list[str] | None = Field(default=None, max_length=5)
    formality: Literal["casual", "smart_casual", "formal"] | None = None
    occasions: list[str] | None = Field(default=None, max_length=8)
    layering: list[str] | None = Field(default=None, max_length=5)
    seasons: list[str] | None = Field(default=None, max_length=4)
    weather_notes: str | None = Field(default=None, max_length=300)
    size: str | None = Field(default=None, max_length=100)
    fit_feedback: str | None = Field(default=None, max_length=500)
    preference: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=1000)


def caller_fields(values: LabelValues):
    return {
        key: {
            "value": value,
            "source_kind": "caller_supplied",
            "verification_state": "unverified",
            "evidence": [],
            "updated_at": int(time.time()),
        }
        for key, value in values.model_dump(exclude_unset=True).items()
    }
