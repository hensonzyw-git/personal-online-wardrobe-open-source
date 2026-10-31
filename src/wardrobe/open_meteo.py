"""No-key hourly forecasts. UTC bounds and units are validated before use."""

import json
import math
from datetime import timedelta

import httpx

from .errors import BusinessError
from .thermal_budget import median
from .weather import WeatherContext, instant
from .writes import digest

VARIABLES = {
    "temperature_2m": "°C",
    "apparent_temperature": "°C",
    "precipitation_probability": "%",
    "precipitation": "mm",
    "wind_speed_10m": "m/s",
}


def bounds(interval):
    lower = instant(interval.start).replace(minute=0, second=0, microsecond=0)
    end = instant(interval.end)
    upper = end.replace(minute=0, second=0, microsecond=0)
    if upper < end:
        upper += timedelta(hours=1)
    return lower, upper


def failed(code="INVALID_WEATHER"):
    return BusinessError(
        code, "Hourly forecast failed validation; no stale fallback was used", 502
    )


class OpenMeteo:
    def __init__(self, *, transport=None):
        self.transport = transport
        self.cache = {}

    async def fetch(self, location, interval, now):
        lower, upper = bounds(interval)
        if (
            instant(interval.end).timestamp() <= now
            or upper.timestamp() > now + 16 * 86400
        ):
            raise BusinessError(
                "WEATHER_OUT_OF_RANGE", "Target is outside the forecast range", 422
            )
        binding = digest(
            {"location": location.model_dump(), "interval": interval.model_dump()}
        )
        cached = self.cache.get(binding)
        if cached is not None:
            try:
                cached.validate_for(location, interval, now)
                return cached
            except BusinessError:
                del self.cache[binding]
        try:
            async with (
                httpx.AsyncClient(
                    timeout=8,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                ) as client,
                client.stream(
                    "GET",
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": location.latitude,
                        "longitude": location.longitude,
                        "hourly": ",".join(VARIABLES),
                        "temperature_unit": "celsius",
                        "wind_speed_unit": "ms",
                        "precipitation_unit": "mm",
                        "timezone": "UTC",
                        "timeformat": "unixtime",
                        "start_hour": lower.strftime("%Y-%m-%dT%H:%M"),
                        "end_hour": upper.strftime("%Y-%m-%dT%H:%M"),
                    },
                ) as response,
            ):
                if response.status_code != 200:
                    raise failed("WEATHER_PROVIDER_FAILED")
                raw = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise failed()

            def unique(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj:
                        raise ValueError()
                    obj[key] = value
                return obj

            value = self.normalize(
                json.loads(raw, object_pairs_hook=unique), location, interval, now
            )
            value.validate_for(location, interval, now)
            if len(self.cache) >= 64:
                self.cache.pop(next(iter(self.cache)))
            self.cache[binding] = value
            return value
        except httpx.HTTPError:
            raise failed("WEATHER_PROVIDER_FAILED") from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise failed() from None

    @staticmethod
    def normalize(raw, location, interval, now):
        try:

            def number(value):
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError()
                return value

            if not isinstance(raw, dict) or raw.get("utc_offset_seconds") != 0:
                raise ValueError()
            lat, lon = number(raw["latitude"]), number(raw["longitude"])
            if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError()
            delta_lon = min(
                abs(lon - location.longitude), 360 - abs(lon - location.longitude)
            )
            distance = 111.2 * math.hypot(
                lat - location.latitude,
                delta_lon * math.cos(math.radians(location.latitude)),
            )
            if distance > 50:
                raise ValueError()
            units = raw["hourly_units"]
            if units.get("time") != "unixtime" or any(
                units.get(k) != v
                for k, v in VARIABLES.items()
                if k != "apparent_temperature"
            ):
                raise ValueError()
            hourly = raw["hourly"]
            times = hourly["time"]
            if (
                not isinstance(times, list)
                or not times
                or any(type(t) is not int or t % 3600 for t in times)
                or times != sorted(set(times))
            ):
                raise ValueError()
            apparent = hourly.get("apparent_temperature")
            if apparent is None:
                apparent = [None] * len(times)
            if not isinstance(apparent, list) or len(apparent) != len(times):
                raise ValueError()
            if units.get("apparent_temperature") not in (None, "°C") or (
                any(value is not None for value in apparent)
                and units.get("apparent_temperature") != "°C"
            ):
                raise ValueError()
            for value in apparent:
                if value is not None and not -100 <= number(value) <= 80:
                    raise ValueError()
            if any(
                not isinstance(hourly[k], list) or len(hourly[k]) != len(times)
                for k in VARIABLES
                if k != "apparent_temperature"
            ):
                raise ValueError()
            lower, upper = bounds(interval)
            required = list(
                range(int(lower.timestamp()), int(upper.timestamp()) + 1, 3600)
            )
            indices = {t: i for i, t in enumerate(times)}
            if any(t not in indices for t in required):
                raise failed("WEATHER_COVERAGE_INCOMPLETE")
            values = {
                k: [number(hourly[k][indices[t]]) for t in required]
                for k in VARIABLES
                if k != "apparent_temperature"
            }
            if any(
                not 0 <= p <= 100 for p in values["precipitation_probability"]
            ) or any(p < 0 for p in values["precipitation"] + values["wind_speed_10m"]):
                raise ValueError()
            # Validate every selected temperature; aggregation must not hide an invalid hour.
            if any(not -100 <= v <= 70 for v in values["temperature_2m"]):
                raise ValueError()
            feels = [apparent[indices[t]] for t in required]
            missing = any(value is None for value in feels)
            return WeatherContext(
                provider="Open-Meteo/forecast",
                source_kind="forecast",
                location=location,
                interval=interval,
                fetched_at=now,
                published_at=None,
                valid_until=now + 900,
                publication_freshness="unknown",
                temperature_min=min(values["temperature_2m"]),
                temperature_max=max(values["temperature_2m"]),
                feels_like_min=None if missing else min(feels),
                feels_like_max=None if missing else max(feels),
                feels_like_missing=missing,
                temperature_median=median(values["temperature_2m"]),
                temperature_sample_count=len(required),
                feels_like_median=None if missing else median(feels),
                feels_like_sample_count=0 if missing else len(required),
                missing_reason="hourly_feels_like_incomplete" if missing else None,
                # Open-Meteo rain/probability describe the preceding hour.
                precipitation_probability=max(values["precipitation_probability"][1:])
                / 100,
                precipitation_mm=sum(values["precipitation"][1:]),
                wind_max_m_s=max(values["wind_speed_10m"]),
                source_hash=digest(raw),
                attributions=[
                    "Weather data by Open-Meteo.com (CC BY 4.0)",
                    "https://open-meteo.com/",
                ],
            )
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            raise failed() from None
