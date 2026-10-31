from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_foundation import headers
from test_recommendation_review_regressions import catalog, coverage, payload
from test_recommendations import seed_wardrobe

from wardrobe.labels import LabelValues
from wardrobe.recommendations import candidates_for


def thermal_catalog(thickness="thin", verified=True):
    garments = catalog()
    for garment in garments:
        garment["annotation_fields"]["thickness"] = {
            "value": thickness,
            "verification_state": "user_verified" if verified else "unverified",
        }
    return garments


@pytest.mark.parametrize("thickness", ["medium", "thick", "very_thick", None])
def test_hot_weather_cannot_accept_thick_or_unknown_clothing(thickness):
    result, search = candidates_for(
        thermal_catalog(thickness),
        payload(),
        SimpleNamespace(
            feels_like_min=29, feels_like_max=32, temperature_min=26, temperature_max=29
        ),
    )
    assert not result
    assert search["temperature_check"]["blocked_count"] > 0


def test_unknown_and_unverified_are_pending_not_a_neutral_weather_pass():
    _, search = candidates_for(
        thermal_catalog("thin", False),
        payload(),
        SimpleNamespace(feels_like_min=20, feels_like_max=23),
    )
    assert search["temperature_check"]["pending_count"] > 0
    assert len(search["temperature_check"]["pending_items"]) <= 10


def test_thin_confirmed_clothing_passes_heat_and_footwear_does_not_need_thickness():
    garments = thermal_catalog()
    for g in garments:
        if "footwear" in g["roles"]:
            g["annotation_fields"] = {}
    candidates, _ = candidates_for(
        garments, payload(), SimpleNamespace(feels_like_min=29, feels_like_max=32)
    )
    assert candidates and len(coverage(candidates, "footwear")) == 17


def test_real_temperature_can_block_even_if_feels_like_is_lower():
    candidates, _ = candidates_for(
        thermal_catalog("medium"),
        payload(),
        SimpleNamespace(
            feels_like_min=20, feels_like_max=26, temperature_min=24, temperature_max=29
        ),
    )
    assert not candidates


def test_cold_weather_requires_a_confirmed_warm_outer_layer():
    garments = thermal_catalog("thin")
    weather = SimpleNamespace(feels_like_min=5, feels_like_max=9)
    assert not candidates_for(garments, payload(), weather)[0]
    for g in garments:
        if "outerwear" in g["roles"]:
            g["annotation_fields"]["thickness"]["value"] = "very_thick"
        if "lower" in g["roles"]:
            g["annotation_fields"]["thickness"]["value"] = "thick"
    assert candidates_for(garments, payload(), weather)[0]


def test_four_thickness_levels_are_exact_and_do_not_set_warmth():
    for level in ["thin", "medium", "thick", "very_thick"]:
        value = LabelValues(thickness=level)
        assert value.thickness == level and value.warmth is None
    with pytest.raises(ValidationError):
        LabelValues(thickness="guess")


def test_relative_tomorrow_is_resolved_in_location_timezone_and_frozen(
    live_service, monkeypatch
):
    client, db = live_service
    seed_wardrobe(client, db)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 12, 31, 17, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("wardrobe.recommendations.datetime", Clock)
    body = payload(target_date="tomorrow").model_dump()
    key = {**headers(), "Idempotency-Key": "relative-date"}
    response = client.post("/v1/recommendations", headers=key, json=body)
    assert response.status_code == 202, response.text
    identifier = response.json()["data"]["recommendation_id"]
    from wardrobe.models import Recommendation

    with db.sessions() as session:
        job = session.get(Recommendation, identifier)
        assert job.payload["interval"]["start"] == "2027-01-01T09:00:00+00:00"
        assert job.payload["interval"]["end"] == "2027-01-01T23:00:00+00:00"

    class NextClock(Clock):
        @classmethod
        def now(cls, tz=None):
            return datetime(2027, 1, 2, 17, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("wardrobe.recommendations.datetime", NextClock)
    assert (
        client.post("/v1/recommendations", headers=key, json=body).json()
        == response.json()
    )


@pytest.mark.parametrize(
    "category,role",
    [
        ("短袖T恤", "upper"),
        ("短袖衬衫", "upper"),
        ("短袖连帽上衣", "upper"),
        ("短裤", "lower"),
    ],
)
def test_user_exempt_short_clothing_needs_no_label_and_is_not_filled_as_thin(
    category, role
):
    from wardrobe.recommendations import temperature_status

    garment = {"category": category, "annotation_fields": {}}
    assert (
        temperature_status(
            garment, role, SimpleNamespace(feels_like_min=29, feels_like_max=32)
        )
        == "eligible"
    )
    assert garment["annotation_fields"] == {}


def test_exemption_does_not_allow_known_thick_hot_clothing_or_shorts_in_cold():
    from wardrobe.recommendations import temperature_status

    g = {
        "category": "短袖连帽上衣",
        "annotation_fields": {
            "thickness": {"value": "very_thick", "verification_state": "user_verified"}
        },
    }
    assert (
        temperature_status(
            g, "upper", SimpleNamespace(feels_like_min=29, feels_like_max=32)
        )
        == "conflict"
    )
    assert (
        temperature_status(
            {"category": "短裤", "annotation_fields": {}},
            "lower",
            SimpleNamespace(feels_like_min=5, feels_like_max=9),
        )
        == "conflict"
    )


@pytest.mark.parametrize(
    "feels,temperature,required,missing",
    [
        (21.9, 25, True, False),
        (21.999, 25, True, False),
        (22.0, 18, False, False),
        (0.0, 25, True, False),
        (None, 21.9, True, True),
        (None, 22.0, False, True),
    ],
)
def test_outerwear_threshold_prefers_raw_feels_and_preserves_zero(
    feels, temperature, required, missing
):
    from wardrobe.recommendations import outerwear_requirement

    weather = SimpleNamespace(feels_like_min=feels, temperature_min=temperature)
    result = outerwear_requirement(catalog(), payload(), weather)
    assert result["outerwear_required"] is required
    assert result["feels_like_missing"] is missing
    assert result["feels_like_min"] == feels and result["temp_min"] == temperature


@pytest.mark.parametrize(
    "now,zone,expected_date,offset",
    [
        ("2027-12-31T23:30:00+00:00", "Europe/Berlin", "2028-01-02", "+01:00"),
        ("2028-02-28T02:00:00+00:00", "UTC", "2028-02-29", "+00:00"),
        ("2027-03-13T17:00:00+00:00", "America/New_York", "2027-03-14", "-04:00"),
        ("2027-11-06T17:00:00+00:00", "America/New_York", "2027-11-07", "-05:00"),
    ],
)
def test_default_window_year_leap_and_dst_boundaries(
    live_service, monkeypatch, now, zone, expected_date, offset
):
    from wardrobe.models import Recommendation

    client, db = live_service
    seed_wardrobe(client, db)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(now).astimezone(tz)

    monkeypatch.setattr("wardrobe.recommendations.datetime", Clock)
    body = payload(target_date="tomorrow").model_dump()
    body["location"]["timezone"] = zone
    response = client.post(
        "/v1/recommendations",
        headers={**headers(), "Idempotency-Key": "calendar"},
        json=body,
    )
    assert response.status_code == 202, response.text
    with db.sessions() as session:
        row = session.get(Recommendation, response.json()["data"]["recommendation_id"])
        assert row.payload["interval"] == {
            "start": expected_date + "T09:00:00" + offset,
            "end": expected_date + "T23:00:00" + offset,
        }


def test_hottest_22_is_first_choice_rule_not_single_item_conflict():
    from test_thermal_budget import garment, weather

    from wardrobe.recommendations import temperature_status
    from wardrobe.thermal_budget import thermal_context

    w = weather("14", "21")
    w.temperature_max = "22"
    ctx = thermal_context(w)
    assert ctx.hottest == 22 and ctx.hottest_blocks_heavy_first
    assert (
        temperature_status(garment("outerwear", "thick"), "outerwear", w, ctx)
        == "eligible"
    )
