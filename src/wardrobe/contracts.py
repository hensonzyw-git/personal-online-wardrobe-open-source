from dataclasses import dataclass
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .errors import BusinessError


@dataclass(frozen=True)
class Principal:
    owner_id: str
    client_id: str
    scopes: frozenset[str]
    channel: str
    audience: str
    grant_id: str | None = None

    def require(self, scope: str, *, channel: str = "agent") -> None:
        if self.channel != channel or scope not in self.scopes:
            raise BusinessError(
                "SCOPE_DENIED", "This channel lacks the required permission", 403
            )


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ListQuery(StrictModel):
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=4096)
    category: str | None = Field(default=None, min_length=1, max_length=100)


def success(data: dict, *, status: str = "succeeded") -> dict:
    return {
        "schema_version": "wardrobe/1.0",
        "request_id": str(uuid4()),
        "status": status,
        "data": data,
    }
