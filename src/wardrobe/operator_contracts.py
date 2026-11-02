"""Narrow human operator contract, independent of public ListQuery and MCP."""

from typing import Literal

from pydantic import Field, model_validator

from .assets import ROLES
from .contracts import StrictModel
from .labels import LabelValues


class OperatorPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    roles: list[ROLES] | None = Field(default=None, max_length=8)
    availability: Literal["available", "unavailable", "unknown"] | None = None
    annotation_fields: LabelValues | None = None

    @model_validator(mode="after")
    def specific_patch(self):
        if not self.model_fields_set:
            raise ValueError("An explicit patch is required")
        for key in self.model_fields_set - {"category"}:
            if getattr(self, key) is None:
                raise ValueError("Required values cannot be null")
        for key in ("name", "category"):
            value = getattr(self, key)
            if value is not None and not value.strip():
                raise ValueError("Blank text is not allowed")
        if (
            self.annotation_fields is not None
            and not self.annotation_fields.model_fields_set
        ):
            raise ValueError("An explicit annotation key is required")
        return self


class OperatorRevision(StrictModel):
    base_revision: int = Field(ge=1)


class OperatorUpdate(OperatorRevision):
    patch: OperatorPatch


class OperatorListQuery(StrictModel):
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=4096)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    state: Literal["active", "deleted"] = "active"
    sort: Literal["created_at"] = "created_at"
    order: Literal["asc", "desc"] = "desc"


class OperatorCategoryQuery(StrictModel):
    limit: int = Field(default=100, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=4096)
    state: Literal["active", "deleted"] = "active"


class OperatorDetailQuery(StrictModel):
    state: Literal["active", "deleted"] = "active"
