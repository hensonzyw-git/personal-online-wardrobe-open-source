"""Approved O1–O5 contract: synthetic data, real adapters, no external network."""

import asyncio
import copy
import json
import re
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from conftest import OWNER_A
from sqlalchemy import func, select
from test_foundation import headers, rpc
from test_outerwear_tshirt import add_outer
from test_recommendations import (
    LOCATION,
    FixtureEvaluator,
    FixtureWeather,
    completed,
    request_payload,
    seed_wardrobe,
    submit,
)

from wardrobe.models import Garment, HistoryEvent, Recommendation, ServiceState
from wardrobe.open_meteo import OpenMeteo
from wardrobe.outfit_provider import DeepSeekEvaluator, ModelCallQuota
from wardrobe.recommendations import RecommendationService
from wardrobe.weather import QWeather, instant
from wardrobe.writes import digest

NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    monkeypatch.setattr("wardrobe.recommendations.datetime", Clock)
    monkeypatch.setattr(time, "time", lambda: NOW.timestamp())


@pytest.fixture
def scenario():
    return SimpleNamespace(
        temperature_min=24.0,
        temperature_max=26.0,
        feels_min=24.0,
        feels_max=26.0,
        missing=None,
        omit_last=False,
        requests=[],
        evaluations=[],
        prompts=[],
    )


class RecordingEvaluator(FixtureEvaluator):
    def __init__(self, scenario):
        self.scenario = scenario

    async def evaluate(self, candidates, weather, occasion):
        self.scenario.evaluations.append(copy.deepcopy(weather))
        return await super().evaluate(candidates, weather, occasion)


def open_raw(scenario, start, end):
    times = list(range(int(start.timestamp()), int(end.timestamp()) + 1, 3600))
    amount = len(times)
    # Cold-window fixtures retain extrema while giving a legal two-thin budget.
    middle = (
        min(scenario.feels_max, 23) if scenario.feels_min < 22 else scenario.feels_max
    )
    values = [scenario.feels_max] + [middle] * (amount - 2) + [scenario.feels_min]
    if scenario.missing == "partial":
        values[-1] = None
    elif scenario.missing == "all":
        values = [None] * amount
    raw = {
        "latitude": LOCATION.latitude,
        "longitude": LOCATION.longitude,
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
            "time": times,
            "temperature_2m": [scenario.temperature_min]
            + [
                min(scenario.temperature_max, 23)
                if scenario.temperature_min < 22
                else scenario.temperature_max
            ]
            * (amount - 2)
            + [scenario.temperature_max],
            "apparent_temperature": values,
            "precipitation_probability": [0] * amount,
            "precipitation": [0] * amount,
            "wind_speed_10m": [2] * amount,
        },
    }
    if scenario.missing == "key":
        del raw["hourly"]["apparent_temperature"]
        del raw["hourly_units"]["apparent_temperature"]
    elif scenario.missing == "null":
        raw["hourly"]["apparent_temperature"] = None
    if scenario.omit_last:
        for value in raw["hourly"].values():
            value.pop()
    return raw


@pytest.fixture
def providers(request, scenario, tmp_path):
    mode = getattr(request, "param", "open")

    def weather_handler(req):
        scenario.requests.append(req)
        if mode.startswith("qweather"):
            points = [NOW.replace(minute=0) + timedelta(hours=i) for i in range(73)]
            raw = {"metadata": {"attributions": ["synthetic"]}, "hours": []}
            for index, moment in enumerate(points):
                hour = {
                    "forecastTime": moment.isoformat(),
                    "temperature": {
                        "value": scenario.temperature_min
                        if index % 15 == 0
                        else scenario.temperature_max
                        if index % 15 == 1
                        else min(scenario.temperature_max, 23)
                        if scenario.temperature_min < 22
                        else scenario.temperature_max,
                        "unit": "°C",
                    },
                    "feelsLike": {
                        "value": scenario.feels_min
                        if index % 15 == 0
                        else scenario.feels_max
                        if index % 15 == 1
                        else min(scenario.feels_max, 23)
                        if scenario.feels_min < 22
                        else scenario.feels_max,
                        "unit": "°C",
                    },
                    "wind": {"speed": {"value": 2, "unit": "m/s"}},
                    "precipitation": {
                        "amount": {"value": 0, "unit": "mm"},
                        "probability": 0,
                    },
                }
                if scenario.missing == "key":
                    del hour["feelsLike"]
                elif scenario.missing == "null":
                    hour["feelsLike"] = None
                elif (
                    scenario.missing == "all"
                    or scenario.missing == "partial"
                    and index % 2
                ):
                    hour["feelsLike"]["value"] = None
                raw["hours"].append(hour)
        else:
            start = datetime.fromisoformat(req.url.params["start_hour"]).replace(
                tzinfo=UTC
            )
            end = datetime.fromisoformat(req.url.params["end_hour"]).replace(tzinfo=UTC)
            raw = open_raw(scenario, start, end)
        return httpx.Response(200, json=raw)

    weather = (
        QWeather(
            "synthetic.xy.qweatherapi.com",
            lambda: "synthetic",
            max_hours=240,
            transport=httpx.MockTransport(weather_handler),
        )
        if mode.startswith("qweather")
        else OpenMeteo(transport=httpx.MockTransport(weather_handler))
    )
    evaluator = RecordingEvaluator(scenario)
    if mode.endswith("prompt"):

        def model_handler(req):
            projection = json.loads(json.loads(req.content)["messages"][1]["content"])
            scenario.prompts.append(projection)
            evaluations = [
                {
                    "candidate_id": c["candidate_id"],
                    "suitable": True,
                    "color": 0.9,
                    "material": 0.9,
                    "style": 0.9,
                    "silhouette": 0.9,
                    "evidence": {},
                }
                for c in projection["candidates"][:8]
            ]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"evaluations": evaluations}),
                            },
                        }
                    ]
                },
            )

        key = tmp_path / "synthetic-model.json"
        key.write_text(json.dumps({"api_key": "synthetic-key-only"}))
        key.chmod(0o600)
        evaluator = DeepSeekEvaluator(
            key,
            ModelCallQuota(tmp_path / "synthetic-calls.sqlite", 20, create=True),
            transport=httpx.MockTransport(model_handler),
        )
    return {"weather": weather, "evaluator": evaluator}


def default_body(**kwargs):
    return {"location": LOCATION.model_dump(), "target_date": "tomorrow", **kwargs}


def assert_readback(client, db, identifier, expected):
    http = client.get(f"/v1/recommendations/{identifier}", headers=headers()).json()[
        "data"
    ]["result"]
    mcp = rpc(
        client,
        "tools/call",
        {
            "name": "wardrobe.get_recommendation",
            "arguments": {"recommendation_id": identifier},
        },
    ).json()["result"]
    assert not mcp["isError"]
    assert mcp["structuredContent"]["data"]["result"] == http == expected
    with db.sessions() as session:
        from wardrobe.image_previews import present_images

        assert (
            present_images(session.get(Recommendation, identifier).result) == expected
        )


def assert_no_generated(db):
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(HistoryEvent)) == 0
        assert session.get(ServiceState, OWNER_A).history_revision == 0


def test_a1_a9_report_values_default_window_and_persisted_layering(
    live_service, scenario
):
    client, db = live_service
    seed_wardrobe(client, db)
    add_outer(client, db)
    scenario.temperature_min, scenario.temperature_max = 17.9, 22.1
    scenario.feels_min, scenario.feels_max = 18.8, 22.3
    identifier = submit(client, "a1", default_body())
    value = completed(client, identifier)
    assert value["state"] == "succeeded", value
    result = value["result"]
    assert result["interval"] == {
        "start": "2026-10-30T09:00:00+00:00",
        "end": "2026-10-30T23:00:00+00:00",
    }
    assert "Default local daytime: 09:00–23:00" in result["assumptions"]
    assert any("默认当地外出时段：09:00–23:00" in a for a in result["assumptions"])
    assert result["search"]["rule_version"] == "wardrobe-candidates/7"
    assert (
        result["search"]["temperature_check"]["rule_version"]
        == "wardrobe-temperature/3"
    )
    layering = result["search"]["layering_check"]
    assert layering["rule_version"] == "outerwear-layerable-inner/1"
    assert layering["thermal_budget"]["rule_version"] == "thermal-budget/2"
    assert layering["outerwear_required"] and layering["feels_like_min"] == 18.8
    for outfit in result["outfits"]:
        outer = next(i for i in outfit["items"] if i["role"] == "outerwear")
        upper = next(i for i in outfit["items"] if i["role"] == "upper")
        assert upper["garment"]["category"] == "短袖T恤"
        assert outer["garment"]["id"] != upper["garment"]["id"]
    assert result["weather"]["feels_like_max"] == 22.3
    assert scenario.evaluations[0]["feels_like_min"] == 18.8
    assert not scenario.evaluations[0]["feels_like_missing"]
    assert_readback(client, db, identifier, result)
    with db.sessions() as session:
        events = list(session.scalars(select(HistoryEvent)))
        assert len(events) == 2 and all(
            e.payload["target_interval"] == result["interval"] for e in events
        )


@pytest.mark.parametrize("omit_last", [False, True])
def test_a1b_only_2300_is_cold_or_coverage_fails(live_service, scenario, omit_last):
    client, db = live_service
    seed_wardrobe(client, db)
    add_outer(client, db)
    scenario.feels_min, scenario.feels_max = 21.9, 24
    scenario.omit_last = omit_last
    value = completed(client, submit(client, "a1b", default_body()))
    request = scenario.requests[0]
    assert request.url.params["start_hour"] == "2026-10-30T09:00"
    assert request.url.params["end_hour"] == "2026-10-30T23:00"
    if omit_last:
        assert (
            value["state"] == "failed"
            and value["error"]["code"] == "WEATHER_COVERAGE_INCOMPLETE"
        )
        assert_no_generated(db)
    else:
        assert value["state"] == "succeeded", value
        assert value["result"]["search"]["layering_check"]["outerwear_required"]
        assert value["result"]["weather"]["feels_like_min"] == 21.9


@pytest.mark.parametrize(
    "feels,temp,required",
    [(21.9, 25, True), (21.999, 25, True), (22.0, 18, False), (26, 26, False)],
)
def test_a2_a3_feels_priority_strict_threshold_and_warm_shortsleeve(
    live_service, scenario, feels, temp, required
):
    client, db = live_service
    seed_wardrobe(client, db)
    add_outer(client, db)
    scenario.feels_min, scenario.temperature_min = feels, temp
    value = completed(client, submit(client, "a2-a3", default_body()))
    assert value["state"] == "succeeded", value
    result = value["result"]
    assert result["search"]["layering_check"]["outerwear_required"] is required
    assert all(
        any(i["role"] == "outerwear" for i in o["items"]) is required
        for o in result["outfits"]
    )


def test_a4_explicit_warm_short_window_is_never_expanded(live_service, scenario):
    client, db = live_service
    seed_wardrobe(client, db)
    body = request_payload(
        interval={
            "start": "2026-10-30T14:00:00+08:00",
            "end": "2026-10-30T16:00:00+08:00",
        }
    )
    result = completed(client, submit(client, "a4", body))["result"]
    assert result["interval"] == body["interval"]
    assert not any("默认当地外出时段" in n for n in result["assumptions"])
    assert any("保暖参考温度" in n for n in result["assumptions"])
    budget = result["search"]["layering_check"]["thermal_budget"]
    assert budget["reference_temperature_c"] == 26
    assert budget["gap_c"] == 0 and budget["sample_count"] == 3
    assert scenario.requests[0].url.params["end_hour"] == "2026-10-30T08:00"
    assert not result["search"]["layering_check"]["outerwear_required"]


@pytest.mark.parametrize(
    "reason",
    [
        "no_outer",
        "excluded",
        "pending",
        "thermal",
        "no_tshirt",
        "both",
        "core_conflict",
    ],
)
def test_a5_a5b_a6_missing_and_exclude_ids_cover_preexclusion_eligible_set(
    live_service, scenario, reason
):
    client, db = live_service
    ids = seed_wardrobe(client, db)
    scenario.feels_min = 21.9
    outer = add_outer(client, db) if reason != "no_outer" and reason != "both" else None
    excluded = (
        [outer]
        if reason in {"excluded", "pending", "thermal"}
        else ids[:3]
        if reason == "no_tshirt"
        else []
    )
    if reason in {"pending", "thermal", "both", "core_conflict"}:
        with db.write() as session:
            if reason == "pending":
                g = session.get(Garment, outer)
                g.annotation_fields = {
                    **g.annotation_fields,
                    "thickness": {"value": None},
                }
            elif reason == "thermal":
                scenario.temperature_max = 29
                g = session.get(Garment, outer)
                g.annotation_fields = {
                    **g.annotation_fields,
                    "thickness": {
                        "value": "thick",
                        "verification_state": "user_verified",
                    },
                }
            elif reason == "both":
                for identifier in ids[:3]:
                    session.get(Garment, identifier).category = "长袖衬衫"
            else:
                g = session.get(Garment, ids[0])
                g.category = "连帽上衣"
                g.annotation_fields = {
                    **g.annotation_fields,
                    "sleeve": {"value": "长袖"},
                }
    body = default_body(
        exclude_ids=excluded,
        **({"core_ids": [ids[0]]} if reason == "core_conflict" else {}),
    )
    value = completed(client, submit(client, "a5-a6", body))
    assert value["state"] == "partial", value
    result = value["result"]
    assert result["completeness"] == "partial" and result["outfits"] == []
    assert scenario.evaluations == []
    layering = result["search"]["layering_check"]
    assert layering["outerwear_required"]
    assert layering["all_eligible_outerwear_excluded"] is (reason == "excluded")
    missing = " ".join(result["missing"])
    if reason in {"no_outer", "excluded", "pending", "thermal", "both"}:
        assert "外套" in missing and "outerwear" in missing
    if reason in {"no_outer", "excluded", "pending", "thermal"}:
        assert layering["eligible_tshirt_count"] > 0
    if reason == "excluded":
        assert "用户已排除" in missing
    else:
        assert "用户已排除" not in missing
    if reason in {"no_tshirt", "both"}:
        assert "可叠内搭" in missing
    if reason == "core_conflict":
        assert "核心单品" in missing and "无法同时满足" in missing
    assert_no_generated(db)


@pytest.mark.parametrize("providers", ["open", "qweather"], indirect=True)
@pytest.mark.parametrize("missing", ["partial", "all", "key", "null"])
@pytest.mark.parametrize("minimum", [21.9, 22.0])
def test_a6b_p1_real_adapter_to_evaluator_explanation_commit_http_mcp(
    live_service, providers, scenario, missing, minimum
):
    client, db = live_service
    seed_wardrobe(client, db)
    add_outer(client, db)
    scenario.temperature_min, scenario.missing = minimum, missing
    body = default_body()
    assert "user_weather" not in body
    identifier = submit(client, "a6b", body)
    value = completed(client, identifier)
    assert value["state"] == "succeeded", value
    result = value["result"]
    assert scenario.requests
    for weather in [result["weather"], scenario.evaluations[0]]:
        assert weather["feels_like_min"] is None and weather["feels_like_max"] is None
        assert (
            weather["temperature_min"] == minimum and weather["temperature_max"] == 26
        )
    assert scenario.evaluations[0]["feels_like_missing"]
    assert scenario.evaluations[0]["outerwear_comparison_basis"] == "temperature_min"
    layering = result["search"]["layering_check"]
    assert layering["feels_like_missing"] and layering["feels_like_min"] is None
    assert layering["temp_min"] == minimum and layering["outerwear_required"] is (
        minimum < 22
    )
    note = f"体感缺失，本次比较气温最低值 {minimum:g}°C"
    assert any(note in text for text in result["assumptions"])
    assert any(note in text for text in result["warnings"])
    for outfit in result["outfits"]:
        explanation = outfit["explanation"]["weather"]
        assert note in explanation
        assert not re.search(r"体感\s*-?\d+(?:\.\d+)?–", explanation)
    assert_readback(client, db, identifier, result)
    if isinstance(providers["weather"], OpenMeteo):
        provider = providers["weather"]
        original = next(iter(provider.cache.values())).model_copy(deep=True)
        cached = asyncio.run(
            provider.fetch(LOCATION, original.interval, int(time.time()) + 100)
        )
        assert len(scenario.requests) == 1
        assert cached.feels_like_missing and cached.feels_like_min is None
        assert (
            cached.fetched_at == original.fetched_at
            and cached.valid_until == original.valid_until
        )


@pytest.mark.parametrize("providers", ["open-prompt", "qweather-prompt"], indirect=True)
def test_p1_actual_model_prompt_uses_safe_weather_mapping(live_service, scenario):
    client, db = live_service
    seed_wardrobe(client, db)
    add_outer(client, db)
    scenario.temperature_min, scenario.missing = 21.9, "partial"
    value = completed(client, submit(client, "p1-prompt", default_body()))
    assert value["state"] == "succeeded", value
    weather = scenario.prompts[0]["weather"]
    assert weather["feels_like_missing"]
    assert weather["feels_like_min"] is None and weather["feels_like_max"] is None
    assert weather["temperature_min"] == 21.9
    assert weather["outerwear_comparison_basis"] == "temperature_min"


def test_a7_timezone_utc_boundary_and_same_key_after_midnight(
    live_service, scenario, monkeypatch
):
    client, db = live_service
    seed_wardrobe(client, db)
    body = default_body()
    identifier = submit(client, "a7", body)
    result = completed(client, identifier)["result"]
    assert instant(result["interval"]["start"]).hour == 9
    assert instant(result["interval"]["end"]).hour == 23
    later = NOW + timedelta(hours=10)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return later.astimezone(tz)

    monkeypatch.setattr("wardrobe.recommendations.datetime", Later)
    monkeypatch.setattr(time, "time", lambda: later.timestamp())
    assert submit(client, "a7", body) == identifier
    assert_readback(client, db, identifier, result)
    assert len(scenario.requests) == 1


def test_a8_old_result_generated_and_feedback_full_row_hash_unchanged(
    live_service, scenario
):
    client, db = live_service
    seed_wardrobe(client, db)
    identifier = submit(client, "old", default_body())
    old = completed(client, identifier)["result"]
    response = client.post(
        "/v1/feedback",
        headers={**headers(), "Idempotency-Key": "old-worn"},
        json={
            "recommendation_id": identifier,
            "outfit_id": old["outfits"][0]["outfit_id"],
            "kind": "worn",
        },
    )
    assert response.status_code == 200
    # Simulate a legacy opaque result: reads must preserve it byte for byte.
    with db.write() as session:
        row = session.get(Recommendation, identifier)
        row.result = {
            **row.result,
            "assumptions": ["Default local daytime: 09:00–18:00"],
        }

    def hashes():
        with db.sessions() as session:
            rows = [
                session.get(Recommendation, identifier),
                *session.scalars(
                    select(HistoryEvent).where(
                        HistoryEvent.recommendation_id == identifier
                    )
                ),
            ]
            return {
                row.id: digest(
                    {c.name: getattr(row, c.name) for c in row.__table__.columns}
                )
                for row in rows
            }

    before = hashes()
    scenario.feels_min = 21.9
    add_outer(client, db)
    assert (
        completed(client, submit(client, "new", default_body()))["state"] == "succeeded"
    )
    assert hashes() == before
    legacy = client.get(f"/v1/recommendations/{identifier}", headers=headers()).json()[
        "data"
    ]["result"]
    assert legacy["assumptions"] == ["Default local daytime: 09:00–18:00"]


@pytest.mark.parametrize("field", ["feels_like_min", "feels_like_max"])
@pytest.mark.parametrize("mutation", ["omitted", "null", "extra_missing_flag"])
def test_public_user_weather_stays_strict_http_and_mcp(live_service, field, mutation):
    client, db = live_service
    seed_wardrobe(client, db)
    body = request_payload()
    from wardrobe.weather import Interval

    weather = asyncio.run(
        FixtureWeather().fetch(LOCATION, Interval(**body["interval"]), int(time.time()))
    ).model_dump()
    weather["source_kind"] = "user_input"
    if mutation == "omitted":
        del weather[field]
    elif mutation == "null":
        weather[field] = None
    else:
        weather["feels_like_missing"] = True
    body["user_weather"] = weather
    response = client.post(
        "/v1/recommendations",
        headers={**headers(), "Idempotency-Key": "strict"},
        json=body,
    )
    assert response.status_code == 422
    mcp = rpc(
        client,
        "tools/call",
        {
            "name": "wardrobe.recommend_outfits",
            "arguments": {"payload": body, "key": "strict-mcp"},
        },
    ).json()
    assert "error" in mcp or mcp["result"].get("isError")
    assert_no_generated(db)


@pytest.mark.parametrize("stage", ["after_model", "commit"])
@pytest.mark.parametrize(
    "mutation",
    [
        "remove_outer",
        "remove_tshirt",
        "duplicate_id",
        "excluded",
        "drop_core",
        "fake_diagnostic",
    ],
)
def test_hard_guards_reject_high_score_tampering_with_whole_batch_atomicity(
    live_service, scenario, monkeypatch, stage, mutation
):
    client, db = live_service
    ids = seed_wardrobe(client, db)
    add_outer(client, db)
    scenario.feels_min = 21.9
    excluded = ids[1]
    from wardrobe.catalog import CatalogService

    with db.sessions() as session:
        spare = CatalogService._view(session.get(Garment, ids[2]), session)
        excluded_garment = CatalogService._view(session.get(Garment, excluded), session)
    body = default_body(core_ids=[ids[0]], exclude_ids=[excluded])

    def corrupt(outfits):
        assert len(outfits) == 2  # first remains valid, second poisons the batch
        outfit = outfits[-1]
        if mutation in {"remove_outer", "fake_diagnostic"}:
            outfit["items"] = [i for i in outfit["items"] if i["role"] != "outerwear"]
        elif mutation == "remove_tshirt":
            outfit["items"] = [i for i in outfit["items"] if i["role"] != "upper"]
        elif mutation == "drop_core":
            next(i for i in outfit["items"] if i["role"] == "upper")["garment"] = (
                copy.deepcopy(spare)
            )
        elif mutation == "duplicate_id":
            outfit["items"][-1]["garment"]["id"] = outfit["items"][0]["garment"]["id"]
        else:
            next(i for i in outfit["items"] if i["role"] == "upper")["garment"] = (
                copy.deepcopy(excluded_garment)
            )

    if stage == "commit":
        original = RecommendationService._commit

        def tampered(
            self,
            principal,
            identifier,
            lease,
            catalog_revision,
            history_revision,
            weather,
            result,
        ):
            corrupt(result["outfits"])
            if mutation == "fake_diagnostic":
                result["search"]["layering_check"]["outerwear_required"] = False
            return original(
                self,
                principal,
                identifier,
                lease,
                catalog_revision,
                history_revision,
                weather,
                result,
            )

        monkeypatch.setattr(RecommendationService, "_commit", tampered)
    else:
        from wardrobe import recommendations

        original_choose = recommendations.choose

        def tampered_choose(*args):
            outfits, count = original_choose(*args)
            corrupt(outfits)
            return outfits, count

        monkeypatch.setattr(recommendations, "choose", tampered_choose)
    value = completed(client, submit(client, "hard-guard", body))
    assert (
        value["state"] == "failed"
        and value["error"]["code"] == "OUTFIT_CONSTRAINT_VIOLATION"
    ), value
    assert value["result"] is None
    assert_no_generated(db)


def test_p1_missing_partial_still_exposes_null_and_fallback_without_evaluator(
    live_service, scenario
):
    client, db = live_service
    seed_wardrobe(client, db)
    scenario.temperature_min, scenario.missing = 21.9, "key"
    identifier = submit(client, "missing-partial", default_body())
    value = completed(client, identifier)
    assert value["state"] == "partial"
    result = value["result"]
    assert result["weather"]["feels_like_min"] is None
    assert any("体感缺失" in text for text in result["warnings"])
    assert any("体感缺失" in text for text in result["assumptions"])
    assert not scenario.evaluations
    assert_readback(client, db, identifier, result)
    assert_no_generated(db)


def test_a4_adapter_ignores_cold_night_outside_explicit_window(scenario):
    from wardrobe.weather import Interval

    start = datetime(2026, 10, 30, 1, tzinfo=UTC)
    end = start + timedelta(hours=14)
    scenario.feels_min = 18.8  # Only local 23:00 is cold in the full-day response.
    raw = open_raw(scenario, start, end)
    raw["hourly"]["apparent_temperature"] = [26] * 14 + [18.8]
    interval = Interval(
        start="2026-10-30T14:00:00+08:00", end="2026-10-30T16:00:00+08:00"
    )
    context = OpenMeteo.normalize(raw, LOCATION, interval, int(time.time()))
    assert context.feels_like_min == 26
    assert context.interval == interval
    assert context.feels_like_median == 26 and context.feels_like_sample_count == 3
