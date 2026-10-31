import os
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data_root: Path = Path(".local/wardrobe")
    timezone: str = "UTC"
    agent_enabled: bool = Field(default=False, strict=True)
    intake_enabled: bool = Field(default=False, strict=True)
    audience: str = Field(default="open-wardrobe", min_length=1)
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")
    allowed_origins: tuple[str, ...] = ()
    model_provider: Literal["disabled", "deepseek"] = "disabled"
    model_credentials_file: Path | None = Field(default=None, repr=False)
    model_call_limit: (
        Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
        | Literal["unlimited"]
        | None
    ) = None
    model_ledger_expected_used: int | None = Field(default=None, ge=0, le=1_000_000)
    weather_provider: Literal["disabled", "open-meteo"] = "disabled"

    @field_validator("timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("An IANA timezone is required") from None
        return value

    @field_validator("allowed_hosts", "allowed_origins")
    @classmethod
    def no_wildcards(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or "*" in value for value in values):
            raise ValueError("allowlists must contain explicit values")
        return values

    @field_validator("allowed_hosts")
    @classmethod
    def hosts_required(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or any("/" in x or x != x.lower() for x in values):
            raise ValueError("explicit lowercase hostnames are required")
        return values

    @field_validator("allowed_origins")
    @classmethod
    def origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
            ):
                raise ValueError("origins must be explicit HTTP origins")
        return values

    @classmethod
    def from_env(cls) -> "Settings":
        values: dict = {}
        if "WARDROBE_AGENT_ENABLED" in os.environ:
            value = os.environ["WARDROBE_AGENT_ENABLED"]
            if value not in {"true", "false"}:
                raise ValueError("WARDROBE_AGENT_ENABLED requires true or false")
            values["agent_enabled"] = value == "true"
        if "WARDROBE_INTAKE_ENABLED" in os.environ:
            enabled = os.environ["WARDROBE_INTAKE_ENABLED"]
            if enabled not in {"true", "false"}:
                raise ValueError("WARDROBE_INTAKE_ENABLED requires true or false")
            values["intake_enabled"] = enabled == "true"
        for env, field in [
            ("WARDROBE_DATA_ROOT", "data_root"),
            ("WARDROBE_TIMEZONE", "timezone"),
            ("WARDROBE_AUDIENCE", "audience"),
            ("WARDROBE_MODEL_PROVIDER", "model_provider"),
            ("WARDROBE_WEATHER_PROVIDER", "weather_provider"),
            ("WARDROBE_MODEL_CREDENTIALS_FILE", "model_credentials_file"),
        ]:
            if env in os.environ:
                values[field] = os.environ[env]
        if "WARDROBE_MODEL_CALL_LIMIT" in os.environ:
            raw_limit = os.environ["WARDROBE_MODEL_CALL_LIMIT"]
            values["model_call_limit"] = (
                "unlimited" if raw_limit == "unlimited" else int(raw_limit)
            )
        if "WARDROBE_MODEL_LEDGER_EXPECTED_USED" in os.environ:
            values["model_ledger_expected_used"] = os.environ[
                "WARDROBE_MODEL_LEDGER_EXPECTED_USED"
            ]
        if "model_credentials_file" not in values and os.environ.get(
            "CREDENTIALS_DIRECTORY"
        ):
            values["model_credentials_file"] = (
                Path(os.environ["CREDENTIALS_DIRECTORY"]) / "model"
            )
        for env, field in [
            ("WARDROBE_ALLOWED_HOSTS", "allowed_hosts"),
            ("WARDROBE_ALLOWED_ORIGINS", "allowed_origins"),
        ]:
            if env in os.environ:
                values[field] = tuple(
                    x.strip() for x in os.environ[env].split(",") if x.strip()
                )
        return cls.model_validate(values)
