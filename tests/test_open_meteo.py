import asyncio
import copy
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from wardrobe.errors import BusinessError
from wardrobe.open_meteo import OpenMeteo
from wardrobe.weather import Interval, Location

LOCATION = Location(
    name="Synthetic city", latitude=30.0, longitude=120.0, timezone="UTC"
)
START = datetime(2030, 1, 2, 1, tzinfo=UTC)
INTERVAL = Interval(
    start=START.isoformat(), end=(START + timedelta(hours=2)).isoformat()
)
NOW = int((START - timedelta(days=1)).timestamp())


def fixture():
    return {
        "latitude": 30.01,
        "longitude": 120.02,
        "utc_offset_seconds": 0,
        "hourly_units": {
            "time": "unixtime",
            "temperature_2m": "°C",
            "apparent_temperature": "°C",
            "precipitation_probability": "%",
            "precipitation": "mm",
            "wind_speed_10m": "m/s",
        },
        "hourly": {
            "time": [int((START + timedelta(hours=i)).timestamp()) for i in range(3)],
            "temperature_2m": [20, 21, 22],
            "apparent_temperature": [19, 20, 21],
            "precipitation_probability": [10, 20, 30],
            "precipitation": [9, 0.2, 0.3],
            "wind_speed_10m": [1, 2, 3],
        },
    }


def test_fixed_endpoint_units_utc_and_preceding_hour_rain():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=fixture())

    weather = asyncio.run(
        OpenMeteo(transport=httpx.MockTransport(handler)).fetch(LOCATION, INTERVAL, NOW)
    )
    assert str(seen[0].url).startswith("https://api.open-meteo.com/v1/forecast?")
    assert seen[0].url.params["wind_speed_unit"] == "ms"
    assert seen[0].url.params["timezone"] == "UTC"
    assert seen[0].url.params["timeformat"] == "unixtime"
    assert "forecast_days" not in seen[0].url.params
    assert weather.precipitation_mm == 0.5
    assert weather.precipitation_probability == 0.3
    assert weather.published_at is None and weather.publication_freshness == "unknown"
    assert weather.source_kind == "forecast" and weather.attributions


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x["hourly"]["time"].pop(),
        lambda x: x["hourly"]["time"].__setitem__(1, x["hourly"]["time"][0]),
        lambda x: x["hourly"]["temperature_2m"].__setitem__(1, None),
        lambda x: x["hourly"]["temperature_2m"].__setitem__(1, True),
        lambda x: x["hourly"]["precipitation_probability"].__setitem__(1, 110),
        lambda x: x["hourly_units"].__setitem__("wind_speed_10m", "km/h"),
        lambda x: x.__setitem__("latitude", 45.0),
        lambda x: x.__setitem__("utc_offset_seconds", 3600),
        lambda x: x["hourly"]["wind_speed_10m"].__setitem__(1, -1),
        lambda x: x["hourly"]["temperature_2m"].pop(),
    ],
)
def test_bad_coverage_location_units_and_values_fail_closed(mutation):
    raw = copy.deepcopy(fixture())
    mutation(raw)
    with pytest.raises(BusinessError):
        OpenMeteo.normalize(raw, LOCATION, INTERVAL, NOW)


@pytest.mark.parametrize("status", [301, 401, 429, 500])
def test_http_failure_has_no_fallback(status):
    with pytest.raises(BusinessError):
        asyncio.run(
            OpenMeteo(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(status, json=fixture())
                )
            ).fetch(LOCATION, INTERVAL, NOW)
        )


def test_past_or_far_future_does_not_call_network():
    def forbidden(_):
        pytest.fail("invalid interval reached network")

    provider = OpenMeteo(transport=httpx.MockTransport(forbidden))
    for now in [
        int((START + timedelta(days=2)).timestamp()),
        int((START - timedelta(days=30)).timestamp()),
    ]:
        with pytest.raises(BusinessError):
            asyncio.run(provider.fetch(LOCATION, INTERVAL, now))


def test_cache_does_not_extend_fetch_freshness():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=fixture())

    provider = OpenMeteo(transport=httpx.MockTransport(handler))

    async def exercise():
        first = await provider.fetch(LOCATION, INTERVAL, NOW)
        second = await provider.fetch(LOCATION, INTERVAL, NOW + 100)
        assert second == first and second.fetched_at == NOW
        assert second.temperature_median == 21
        assert second.feels_like_median == 20
        assert second.temperature_sample_count == second.feels_like_sample_count == 3
        third = await provider.fetch(LOCATION, INTERVAL, NOW + 901)
        assert third.fetched_at == NOW + 901

    asyncio.run(exercise())
    assert len(calls) == 2


@pytest.mark.parametrize("missing", [False, True])
def test_same_window_decimal_median_partial_feels_and_public_projection(missing):
    from decimal import Decimal

    from wardrobe.thermal_budget import thermal_context
    from wardrobe.weather import (
        WeatherSnapshot,
        weather_context,
        weather_output,
    )

    raw = fixture()
    raw["hourly"]["temperature_2m"] = [18.8, 22.3, 22.3]
    raw["hourly"]["apparent_temperature"] = [18.8, None if missing else 19.9, 23.2]
    value = OpenMeteo.normalize(raw, LOCATION, INTERVAL, NOW)
    value = weather_context(value)
    assert value.temperature_median == Decimal("22.3")
    assert value.feels_like_median == (None if missing else Decimal("19.9"))
    assert value.feels_like_sample_count == (0 if missing else 3)
    ctx = thermal_context(value)
    assert ctx.basis == ("temperature" if missing else "feels_like")
    assert ctx.reference == (Decimal("22.3") if missing else Decimal("19.9"))
    assert set(weather_output(value)) == set(WeatherSnapshot.model_fields)
    assert "temperature_median" not in weather_output(value)


def test_rounded_hour_does_not_turn_past_interval_into_forecast():
    interval = Interval(
        start="2030-01-02T01:01:00+00:00", end="2030-01-02T01:30:00+00:00"
    )

    def forbidden(_):
        pytest.fail("past interval must fail before network")

    now = int(datetime(2030, 1, 2, 1, 45, tzinfo=UTC).timestamp())
    with pytest.raises(BusinessError):
        asyncio.run(
            OpenMeteo(transport=httpx.MockTransport(forbidden)).fetch(
                LOCATION, interval, now
            )
        )


def test_production_weather_composition(store):
    from wardrobe.app import create_app
    from wardrobe.config import Settings

    db, settings, _ = store
    app = create_app(
        Settings(data_root=settings.data_root, weather_provider="open-meteo"),
        database=db,
    )
    assert isinstance(app.state.recommendations.weather, OpenMeteo)


@pytest.mark.parametrize("mode", ["partial", "all", "key", "null", "no_value_unit"])
def test_only_missing_feels_preserves_unknown_context_in_cache(mode):
    raw = fixture()
    if mode == "partial":
        raw["hourly"]["apparent_temperature"][1] = None
    elif mode == "all" or mode == "no_value_unit":
        raw["hourly"]["apparent_temperature"] = [None] * 3
        if mode == "no_value_unit":
            del raw["hourly_units"]["apparent_temperature"]
    elif mode == "key":
        del raw["hourly"]["apparent_temperature"]
        del raw["hourly_units"]["apparent_temperature"]
    else:
        raw["hourly"]["apparent_temperature"] = None
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=raw)

    provider = OpenMeteo(transport=httpx.MockTransport(handler))

    async def exercise():
        first = await provider.fetch(LOCATION, INTERVAL, NOW)
        cached = await provider.fetch(LOCATION, INTERVAL, NOW + 100)
        assert first.feels_like_min is None and first.feels_like_max is None
        assert cached.feels_like_missing and cached.missing_reason
        assert first.temperature_min == cached.temperature_min == 20
        assert cached.fetched_at == NOW and cached.valid_until == NOW + 900
        with pytest.raises(BusinessError):
            cached.validate_for(LOCATION, INTERVAL, NOW + 901)
        with pytest.raises(BusinessError):
            cached.validate_for(
                LOCATION.model_copy(update={"latitude": 31}), INTERVAL, NOW
            )
        third = await provider.fetch(LOCATION, INTERVAL, NOW + 901)
        assert third.feels_like_missing and third.fetched_at == NOW + 901

    asyncio.run(exercise())
    assert len(calls) == 2


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), -101, 81, "21"])
def test_invalid_provided_feels_is_failure_even_alongside_a_missing_hour(bad):
    raw = fixture()
    raw["hourly"]["apparent_temperature"] = [None, bad, 24]
    with pytest.raises(BusinessError) as error:
        OpenMeteo.normalize(raw, LOCATION, INTERVAL, NOW)
    assert error.value.code == "INVALID_WEATHER"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x["hourly"]["apparent_temperature"].pop(),
        lambda x: x["hourly_units"].__setitem__("apparent_temperature", "K"),
        lambda x: x["hourly_units"].pop("apparent_temperature"),
        lambda x: x["hourly"]["temperature_2m"].__setitem__(1, float("nan")),
        lambda x: x["hourly"]["temperature_2m"].__setitem__(1, float("inf")),
        lambda x: x["hourly"]["temperature_2m"].__setitem__(1, 71),
        lambda x: x["hourly"].pop("temperature_2m"),
    ],
)
def test_other_invalid_hourly_data_never_becomes_missing_feels_fallback(mutation):
    raw = fixture()
    raw["hourly"]["apparent_temperature"][0] = None
    mutation(raw)
    with pytest.raises(BusinessError):
        OpenMeteo.normalize(raw, LOCATION, INTERVAL, NOW)


def test_missing_feels_does_not_hide_wrong_unit_even_without_any_values():
    raw = fixture()
    raw["hourly"]["apparent_temperature"] = None
    raw["hourly_units"]["apparent_temperature"] = "K"
    with pytest.raises(BusinessError):
        OpenMeteo.normalize(raw, LOCATION, INTERVAL, NOW)
