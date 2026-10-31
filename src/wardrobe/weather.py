"""Weather boundaries are checked independently of the provider's HTTP success."""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import Field, field_validator, model_validator

from .contracts import StrictModel
from .errors import BusinessError
from .thermal_budget import decimal, median
from .writes import digest


class Location(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def iana_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("An IANA timezone is required") from None
        return value


class Interval(StrictModel):
    start: str
    end: str

    @model_validator(mode="after")
    def aware_interval(self):
        start, end = instant(self.start), instant(self.end)
        if end <= start or end - start > timedelta(hours=48):
            raise ValueError("Target interval must be positive and at most 48 hours")
        return self


def instant(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("Offset-aware timestamp required")
    return moment.astimezone(UTC)


class WeatherSnapshot(StrictModel):
    provider: str = Field(min_length=1, max_length=100)
    source_kind: Literal["forecast", "user_input"]
    location: Location
    interval: Interval
    fetched_at: int
    published_at: int | None = None
    valid_until: int
    publication_freshness: Literal["fresh", "unknown"]
    temperature_min: float = Field(ge=-100, le=70)
    temperature_max: float = Field(ge=-100, le=70)
    feels_like_min: float = Field(ge=-100, le=80)
    feels_like_max: float = Field(ge=-100, le=80)
    precipitation_probability: float = Field(ge=0, le=1)
    precipitation_mm: float = Field(ge=0)
    wind_max_m_s: float = Field(ge=0, le=150)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    attributions: list[str] = Field(default_factory=list)

    def validate_for(self, location, interval, now):
        if self.location != location or self.interval != interval:
            raise BusinessError(
                "WEATHER_BINDING_MISMATCH",
                "Weather does not match the target location and interval",
                422,
            )
        if self.source_kind == "forecast" and instant(interval.end).timestamp() <= now:
            raise BusinessError(
                "WEATHER_OUT_OF_RANGE",
                "Forward forecast cannot cover an entirely past interval",
                422,
            )
        if (
            self.valid_until <= now
            or self.fetched_at > now + 30
            or now - self.fetched_at > 900
        ):
            raise BusinessError(
                "WEATHER_EXPIRED", "Weather fetch validity has expired", 409
            )
        if self.published_at is not None and (
            self.published_at > now + 30 or now - self.published_at > 10800
        ):
            raise BusinessError(
                "WEATHER_EXPIRED",
                "Forecast publication is outside its validity window",
                409,
            )
        if self.temperature_min > self.temperature_max or (
            self.feels_like_min is not None
            and self.feels_like_max is not None
            and self.feels_like_min > self.feels_like_max
        ):
            raise BusinessError(
                "INVALID_WEATHER", "Weather ranges are inconsistent", 422
            )


class WeatherContext(WeatherSnapshot):
    """Internal provider context; the public caller snapshot stays strict."""

    feels_like_min: float | None = Field(default=None, ge=-100, le=80)
    feels_like_max: float | None = Field(default=None, ge=-100, le=80)
    feels_like_missing: bool = False
    missing_reason: str | None = None
    temperature_median: Decimal | None = None
    feels_like_median: Decimal | None = None
    temperature_sample_count: int = Field(default=0, ge=0, strict=True)
    feels_like_sample_count: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def complete_or_missing(self):
        missing = self.feels_like_min is None or self.feels_like_max is None
        if missing != self.feels_like_missing or (
            missing
            and (self.feels_like_min is not None or self.feels_like_max is not None)
        ):
            raise ValueError("Incomplete feels-like range must be entirely unknown")
        for key in (
            "temperature_min",
            "temperature_max",
            "feels_like_min",
            "feels_like_max",
            "precipitation_probability",
            "precipitation_mm",
            "wind_max_m_s",
        ):
            value = getattr(self, key)
            if value is not None and not math.isfinite(value):
                raise ValueError("Finite weather values required")
        if self.temperature_min > self.temperature_max or (
            not missing and self.feels_like_min > self.feels_like_max
        ):
            raise ValueError("Weather ranges are inconsistent")
        for prefix in ("temperature", "feels_like"):
            middle = getattr(self, prefix + "_median")
            count = getattr(self, prefix + "_sample_count")
            if (middle is None) != (count == 0):
                raise ValueError("Median and sample count must agree")
            if middle is not None and (
                not middle.is_finite()
                or prefix == "feels_like"
                and missing
                or not decimal(getattr(self, prefix + "_min"))
                <= middle
                <= decimal(getattr(self, prefix + "_max"))
            ):
                raise ValueError("Median outside its observed range")
        return self


def weather_context(weather):
    """Revalidate even injected/model_copy values at the service boundary."""
    return WeatherContext.model_validate(weather.model_dump())


def weather_output(weather):
    """Explicit public projection; never invent a feels-like observation."""
    data = {key: getattr(weather, key) for key in WeatherSnapshot.model_fields}
    data["location"] = weather.location.model_dump()
    data["interval"] = weather.interval.model_dump()
    if weather.feels_like_missing:
        data["feels_like_min"] = data["feels_like_max"] = None
    return data


def evaluator_weather(weather):
    return {
        **weather_output(weather),
        "feels_like_missing": weather.feels_like_missing,
        "outerwear_comparison_basis": "temperature_min"
        if weather.feels_like_missing
        else "feels_like_min",
    }


def missing_feels_note(weather):
    return f"体感缺失，本次比较气温最低值 {weather.temperature_min:g}°C 判断外套需求。"


def provider_number(value, lower, upper):
    """QWeather permits numeric strings, but never bool/nonfinite/out-of-range."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError("Invalid provider number")
    number = float(value)
    if not math.isfinite(number) or not lower <= number <= upper:
        raise ValueError("Invalid provider number")
    return number


class DisabledWeather:
    async def fetch(self, location, interval, now):
        raise BusinessError(
            "WEATHER_NOT_CONFIGURED",
            "Configure an authorized weather provider or supply explicit user weather",
            503,
        )


class QWeather:
    """Fixed modern endpoint; operator supplies account host and JWT supplier.

    Constructor is not exposed as an API/tool. No redirects or automatic retries.
    See https://dev.qweather.com/en/docs/api/weather/weather-hourly-forecast/
    """

    def __init__(self, host, token_supplier, *, max_hours, transport=None):
        if (
            not host.endswith(".qweatherapi.com")
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in host)
            or not 1 <= max_hours <= 240
        ):
            raise ValueError(
                "A registered QWeather account host and account horizon are required"
            )
        self.host, self.token_supplier, self.max_hours, self.transport = (
            host,
            token_supplier,
            max_hours,
            transport,
        )

    async def fetch(self, location, interval, now):
        end = instant(interval.end)
        if end.timestamp() <= now:
            raise BusinessError(
                "WEATHER_OUT_OF_RANGE",
                "Forward forecast cannot cover an entirely past interval",
                422,
            )
        hours = math.ceil((end.timestamp() - now) / 3600) + 1
        if hours > self.max_hours:
            raise BusinessError(
                "WEATHER_OUT_OF_RANGE",
                "Target exceeds the configured account forecast horizon",
                422,
            )
        try:
            async with httpx.AsyncClient(
                timeout=10,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                response = await client.get(
                    f"https://{self.host}/weather/v1/hourly/{location.latitude}/{location.longitude}",
                    params={"hours": hours, "localTime": "false", "lang": "en"},
                    headers={"Authorization": "Bearer " + self.token_supplier()},
                )
                if (
                    response.status_code != 200
                    or len(response.content) > 2 * 1024 * 1024
                ):
                    raise BusinessError(
                        "WEATHER_PROVIDER_FAILED",
                        "Weather provider did not return a bounded success response",
                        502,
                    )
                return self.normalize(response.json(), location, interval, now)
        except httpx.HTTPError:
            raise BusinessError(
                "WEATHER_PROVIDER_FAILED",
                "Weather provider request failed; no stale fallback was used",
                502,
            ) from None

    @staticmethod
    def normalize(raw, location, interval, now):
        try:
            lower = instant(interval.start).replace(minute=0, second=0, microsecond=0)
            finish = instant(interval.end)
            upper = finish.replace(minute=0, second=0, microsecond=0)
            if upper < finish:
                upper += timedelta(hours=1)
            points = {instant(hour["forecastTime"]): hour for hour in raw["hours"]}
            required = [
                lower + timedelta(hours=index)
                for index in range(int((upper - lower).total_seconds() // 3600) + 1)
            ]
            if any(moment not in points for moment in required):
                raise BusinessError(
                    "WEATHER_COVERAGE_INCOMPLETE",
                    "Hourly forecast does not continuously cover the target boundaries",
                    422,
                )
            selected = [points[moment] for moment in required]
            temperatures, feels, winds, probabilities, amounts = [], [], [], [], []
            for hour in selected:
                if (
                    hour["temperature"]["unit"] != "°C"
                    or hour["wind"]["speed"]["unit"] != "m/s"
                    or hour["precipitation"]["amount"]["unit"] != "mm"
                ):
                    raise ValueError("Unexpected provider units")
                temperatures.append(
                    provider_number(hour["temperature"]["value"], -100, 70)
                )
                winds.append(provider_number(hour["wind"]["speed"]["value"], 0, 150))
                probabilities.append(
                    provider_number(hour["precipitation"]["probability"], 0, 1)
                )
                amounts.append(
                    provider_number(
                        hour["precipitation"]["amount"]["value"], 0, math.inf
                    )
                )
                observation = hour.get("feelsLike")
                value = observation.get("value") if observation is not None else None
                if observation is not None and (
                    observation.get("unit") not in (None, "°C")
                    or value is not None
                    and observation.get("unit") != "°C"
                ):
                    raise ValueError("Unexpected feels-like unit")
                feels.append(
                    None if value is None else provider_number(value, -100, 80)
                )
            missing = any(value is None for value in feels)
            return WeatherContext(
                provider="QWeather/weather-v1",
                source_kind="forecast",
                location=location,
                interval=interval,
                fetched_at=now,
                published_at=None,
                valid_until=now + 900,
                publication_freshness="unknown",
                temperature_min=min(temperatures),
                temperature_max=max(temperatures),
                feels_like_min=None if missing else min(feels),
                feels_like_max=None if missing else max(feels),
                feels_like_missing=missing,
                temperature_median=median(temperatures),
                temperature_sample_count=len(temperatures),
                feels_like_median=None if missing else median(feels),
                feels_like_sample_count=0 if missing else len(feels),
                missing_reason="hourly_feels_like_incomplete" if missing else None,
                precipitation_probability=max(probabilities),
                precipitation_mm=sum(amounts[:-1]),
                wind_max_m_s=max(winds),
                source_hash=digest(raw),
                attributions=raw["metadata"]["attributions"],
            )
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            raise BusinessError(
                "INVALID_WEATHER",
                "Weather provider schema or units do not match the adapter",
                502,
            ) from None
