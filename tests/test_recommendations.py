import asyncio
import hashlib
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from conftest import OWNER_A, TOKEN_A, TOKEN_B
from sqlalchemy import func, select
from test_catalog_writes import (
    assertion,
    decide,
    enable_write,
    human_setup,
    png,
    propose,
    upload,
)
from test_foundation import headers, rpc

from wardrobe.auth import token_hash
from wardrobe.errors import BusinessError
from wardrobe.models import ClientGrant, HistoryEvent, Recommendation
from wardrobe.recommendations import RecommendationService
from wardrobe.weather import Interval, Location, QWeather, WeatherSnapshot
from wardrobe.writes import digest

LOCATION = Location(
    name="Synthetic city", latitude=30.0, longitude=120.0, timezone="UTC"
)


class FixtureWeather:
    async def fetch(self, location, interval, now):
        return WeatherSnapshot(
            provider="synthetic-offline-fixture",
            source_kind="forecast",
            location=location,
            interval=interval,
            fetched_at=now,
            valid_until=now + 900,
            publication_freshness="unknown",
            temperature_min=24.0,
            temperature_max=26.0,
            feels_like_min=24.0,
            feels_like_max=26.0,
            precipitation_probability=0.0,
            precipitation_mm=0.0,
            wind_max_m_s=2.0,
            source_hash=digest({"fixture": True}),
        )


class FixtureEvaluator:
    async def evaluate(self, candidates, weather, occasion):
        await asyncio.sleep(0.02)
        return [
            {
                "candidate_id": c["candidate_id"],
                "suitable": True,
                "color": 0.8,
                "material": 0.8,
                "style": 0.8,
                "silhouette": 0.8,
                "evidence": {i["garment"]["id"]: ["primary_color"] for i in c["items"]},
            }
            for c in candidates
        ]


@pytest.fixture
def providers():
    return {"weather": FixtureWeather(), "evaluator": FixtureEvaluator()}


def seed_wardrobe(client, db):
    enable_write(db)
    from wardrobe.seasons import initialize

    initialize(db, OWNER_A, "autumn_winter")
    with db.write() as session:
        grant = session.get(ClientGrant, token_hash(TOKEN_A))
        grant.scopes = [*grant.scopes, "wardrobe.recommend", "wardrobe.feedback"]
    asset = upload(client, png())
    identifiers = []
    for role, amount in [("upper", 3), ("lower", 2), ("footwear", 2)]:
        for index in range(amount):
            value = client.post(
                "/v1/garments",
                headers={**headers(), "Idempotency-Key": f"seed-{role}-{index}"},
                json={
                    "name": f"Synthetic {role} {index}",
                    "category": {
                        "upper": "短袖T恤",
                        "lower": "长裤",
                        "footwear": "低帮运动鞋",
                    }[role],
                    "roles": [role],
                    "asset_ids": [asset],
                    "annotation_fields": {
                        "primary_color": "white",
                        "sleeve": "短袖" if role == "upper" else None,
                        "style": ["casual"],
                        "material": None,
                    },
                },
            )
            assert value.status_code == 200, value.text
            identifiers.append(value.json()["data"]["garment_id"])
    with db.write() as session:
        from wardrobe.models import Garment

        for identifier in identifiers:
            garment = session.get(Garment, identifier)
            garment.annotation_fields = {
                **garment.annotation_fields,
                "thickness": {
                    "value": "thin",
                    "verification_state": "user_verified",
                    "source_kind": "user_verified",
                },
            }
    return identifiers


def request_payload(**overrides):
    start = (datetime.now(UTC) + timedelta(hours=2)).replace(
        minute=0, second=0, microsecond=0
    )
    return {
        "location": LOCATION.model_dump(),
        "interval": {
            "start": start.isoformat(),
            "end": (start + timedelta(hours=3)).isoformat(),
        },
        **overrides,
    }


def submit(client, key, payload=None):
    response = client.post(
        "/v1/recommendations",
        headers={**headers(), "Idempotency-Key": key},
        json=payload or request_payload(),
    )
    assert response.status_code == 202, response.text
    return response.json()["data"]["recommendation_id"]


def completed(client, identifier):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/v1/recommendations/{identifier}", headers=headers())
        assert response.status_code == 200, response.text
        value = response.json()["data"]
        if value["state"] not in {"queued", "running"}:
            return value
        time.sleep(0.02)
    raise AssertionError("Fixture job did not complete")


def test_season_change_during_evaluation_never_publishes(live_service, monkeypatch):
    from wardrobe.models import WardrobePreference

    client, db = live_service
    seed_wardrobe(client, db)
    original = FixtureEvaluator.evaluate

    async def changed(evaluator, candidates, weather, occasion):
        with db.write() as session:
            preference = session.get(WardrobePreference, OWNER_A)
            preference.active_season = "spring_summer"
            preference.revision += 1
        return await original(evaluator, candidates, weather, occasion)

    monkeypatch.setattr(FixtureEvaluator, "evaluate", changed)
    identifier = submit(client, "season-during-model")
    result = completed(client, identifier)
    assert result["state"] == "failed" and result["error"]["code"] == "SEASON_CHANGED"
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(HistoryEvent)) == 0
    # Same key still resolves the original task after a change, no fresh provider job.
    saved = client.get(f"/v1/recommendations/{identifier}", headers=headers()).json()[
        "data"
    ]
    assert saved["wardrobe_context"]["active_season"] == "autumn_winter"


def test_closed_core_and_explicit_revision_conflict(live_service):
    from wardrobe.models import Garment, WardrobePreference

    client, db = live_service
    identifiers = seed_wardrobe(client, db)
    with db.write() as session:
        session.get(Garment, identifiers[0]).category = "短袖衬衫"
    identifier = submit(
        client, "stored-core", request_payload(core_ids=[identifiers[0]])
    )
    assert completed(client, identifier)["error"]["code"] == "CORE_ITEM_STORED"
    body = request_payload(expected_season_revision=1)
    with db.write() as session:
        session.get(WardrobePreference, OWNER_A).revision += 1
    response = client.post(
        "/v1/recommendations",
        headers={**headers(), "Idempotency-Key": "revision-conflict"},
        json=body,
    )
    assert response.status_code == 409


def test_weather_recommendations_low_repeat_feedback_and_mcp(live_service):
    client, db = live_service
    seed_wardrobe(client, db)
    payload = request_payload()
    first = submit(client, "rec-1", payload)
    assert submit(client, "rec-1", payload) == first
    result1 = completed(client, first)
    assert result1["state"] == "succeeded", result1
    assert len(result1["result"]["outfits"]) == 2
    second = submit(client, "rec-2")
    result2 = completed(client, second)
    signatures1 = {o["signature"] for o in result1["result"]["outfits"]}
    signatures2 = {o["signature"] for o in result2["result"]["outfits"]}
    assert not signatures1 & signatures2
    assert all(not o["exact_repeat_14d"] for o in result2["result"]["outfits"])
    assert result1["result"]["weather"]["published_at"] is None
    assert result1["result"]["weather"]["publication_freshness"] == "unknown"
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(HistoryEvent)) == 4
        assert set(session.scalars(select(HistoryEvent.kind))) == {"generated"}
    outfit = result1["result"]["outfits"][0]
    feedback = {
        "recommendation_id": first,
        "outfit_id": outfit["outfit_id"],
        "kind": "worn",
    }
    one = client.post(
        "/v1/feedback",
        headers={**headers(), "Idempotency-Key": "wear-1"},
        json=feedback,
    )
    assert one.status_code == 200, one.text
    assert (
        client.post(
            "/v1/feedback",
            headers={**headers(), "Idempotency-Key": "wear-1"},
            json=feedback,
        ).json()
        == one.json()
    )
    history = client.get("/v1/history", params={"limit": 2}, headers=headers()).json()[
        "data"
    ]
    assert history["has_more"] and history["next_cursor"]
    modern = rpc(
        client,
        "tools/call",
        {
            "name": "wardrobe.get_recommendation",
            "arguments": {"recommendation_id": first},
        },
    ).json()["result"]
    assert (
        not modern["isError"]
        and modern["structuredContent"]["data"]["state"] == "succeeded"
    )
    assert (
        client.get(f"/v1/recommendations/{first}", headers=headers(TOKEN_B)).status_code
        == 404
    )


def test_unknown_functions_core_and_deleted_items(live_service):
    client, db = live_service
    ids = seed_wardrobe(client, db)
    impossible = submit(
        client,
        "requires-waterproof",
        request_payload(required_functions={"footwear": ["waterproof"]}),
    )
    value = completed(client, impossible)
    assert value["state"] == "partial" and not value["result"]["outfits"]
    key = human_setup(db)
    remove = propose(client, ids[0], operation="delete", key="remove-upper")
    assert decide(client, assertion(key, remove)).status_code == 200
    missing_core = submit(client, "removed-core", request_payload(core_ids=[ids[0]]))
    assert not completed(client, missing_core)["result"]["outfits"]


def test_missing_location_and_strict_model_evidence(live_service, monkeypatch):
    client, db = live_service
    seed_wardrobe(client, db)
    assert (
        client.post(
            "/v1/recommendations",
            headers={**headers(), "Idempotency-Key": "no-location"},
            json={},
        ).status_code
        == 422
    )

    async def invented(self, candidates, weather, occasion):
        return [
            {
                "candidate_id": "invented-garment",
                "suitable": True,
                "color": 1.0,
                "material": 1.0,
                "style": 1.0,
                "silhouette": 1.0,
            }
        ]

    monkeypatch.setattr(FixtureEvaluator, "evaluate", invented)
    identifier = submit(client, "model-invented")
    result = completed(client, identifier)
    assert (
        result["state"] == "failed"
        and result["error"]["code"] == "INVALID_MODEL_OUTPUT"
    )
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(HistoryEvent)) == 0


def test_expired_job_and_revoked_permission_do_not_commit(live_service, monkeypatch):
    client, db = live_service
    seed_wardrobe(client, db)

    async def revoke_during_evaluation(self, candidates, weather, occasion):
        with db.write() as session:
            grant = session.get(ClientGrant, token_hash(TOKEN_A))
            grant.scopes = [
                scope for scope in grant.scopes if scope != "wardrobe.recommend"
            ]
        return await FixtureEvaluatorOriginal(candidates, weather, occasion)

    FixtureEvaluatorOriginal = FixtureEvaluator().evaluate
    monkeypatch.setattr(FixtureEvaluator, "evaluate", revoke_during_evaluation)
    identifier = submit(client, "revoke-during-job")
    result = completed(client, identifier)
    assert result["state"] == "failed" and result["error"]["code"] == "UNAUTHORIZED"
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(HistoryEvent)) == 0
    with db.write() as session:
        session.get(ClientGrant, token_hash(TOKEN_A)).scopes.append(
            "wardrobe.recommend"
        )
        row = Recommendation(
            id="expired-synthetic",
            owner_id=OWNER_A,
            client_id="client-a",
            grant_id=token_hash(TOKEN_A),
            payload={},
            state="queued",
            created_at=int(time.time()) - 130,
            deadline=int(time.time()) - 10,
            attempts=0,
        )
        session.add(row)
    result = client.get(
        "/v1/recommendations/expired-synthetic", headers=headers()
    ).json()["data"]
    assert (
        result["state"] == "failed"
        and result["error"]["code"] == "TASK_DEADLINE_EXCEEDED"
    )


def weather_raw(interval):
    start = datetime.fromisoformat(interval.start)
    return {
        "metadata": {"attributions": ["https://example.invalid/synthetic"]},
        "hours": [
            {
                "forecastTime": (start + timedelta(hours=i)).isoformat(),
                "temperature": {"value": 20, "unit": "°C"},
                "feelsLike": {"value": 21, "unit": "°C"},
                "wind": {"speed": {"value": 3, "unit": "m/s"}},
                "precipitation": {
                    "amount": {"value": 0.1, "unit": "mm"},
                    "probability": 0.2,
                },
            }
            for i in range(4)
        ],
    }


def test_weather_boundaries_publication_unknown_and_fixed_endpoint():
    start = (datetime.now(UTC) + timedelta(hours=24)).replace(
        minute=0, second=0, microsecond=0
    )
    interval = Interval(
        start=start.isoformat(), end=(start + timedelta(hours=3)).isoformat()
    )
    raw = weather_raw(interval)
    weather = QWeather.normalize(raw, LOCATION, interval, int(time.time()))
    assert weather.published_at is None and weather.publication_freshness == "unknown"
    assert weather.wind_max_m_s == 3 and weather.precipitation_probability == 0.2
    assert weather.temperature_median == 20 and weather.feels_like_median == 21
    assert weather.temperature_sample_count == weather.feels_like_sample_count == 4
    incomplete = {**raw, "hours": [raw["hours"][0], *raw["hours"][2:]]}
    with pytest.raises(BusinessError, match="continuously"):
        QWeather.normalize(incomplete, LOCATION, interval, int(time.time()))
    recorded = []

    def handler(request):
        recorded.append(request)
        return httpx.Response(200, json=raw)

    provider = QWeather(
        "synthetic.xy.qweatherapi.com",
        lambda: "synthetic-private-jwt",
        max_hours=72,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(provider.fetch(LOCATION, interval, int(time.time())))
    assert result.interval == interval
    assert recorded[0].url.path.startswith("/weather/v1/hourly/")
    assert (
        "hours" in recorded[0].url.params and "startTime" not in recorded[0].url.params
    )
    too_short = QWeather(
        "synthetic.xy.qweatherapi.com",
        lambda: "synthetic",
        max_hours=24,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(BusinessError, match="horizon"):
        asyncio.run(too_short.fetch(LOCATION, interval, int(time.time())))


def test_restart_running_job_does_not_blindly_resend_provider(store):
    db, settings, cursor_key = store
    from wardrobe.assets import AssetService
    from wardrobe.catalog import CatalogService
    from wardrobe.db import load_private_key

    service = RecommendationService(
        db,
        CatalogService(db.sessions, cursor_key),
        AssetService(
            db, settings.data_root, load_private_key(settings.data_root, "upload.key")
        ),
        settings.audience,
    )
    now = int(time.time())
    with db.write() as session:
        session.add(
            Recommendation(
                id="unknown-running",
                owner_id=OWNER_A,
                client_id="client-a",
                grant_id=token_hash(TOKEN_A),
                payload={},
                state="running",
                created_at=now - 100,
                deadline=now + 20,
                lease_id="old-lease",
                lease_until=now - 1,
                attempts=1,
            )
        )
    assert service.claim() is None
    with db.sessions.begin() as session:
        row = session.get(Recommendation, "unknown-running")
        assert row.state == "failed" and row.error["code"] == "UPSTREAM_RESULT_UNKNOWN"


def test_two_jobs_same_snapshot_replan_before_atomic_history(store):
    from wardrobe.assets import AssetService
    from wardrobe.auth import Authenticator
    from wardrobe.catalog import CatalogService
    from wardrobe.db import load_private_key
    from wardrobe.models import Asset, Garment, GarmentAsset
    from wardrobe.recommendations import RecommendationInput

    db, settings, cursor_key = store
    from wardrobe.seasons import initialize

    initialize(db, OWNER_A, "autumn_winter")
    assets = AssetService(
        db, settings.data_root, load_private_key(settings.data_root, "upload.key")
    )
    content = png()
    content_hash = hashlib.sha256(content).hexdigest()
    (assets.originals / content_hash).write_bytes(content)
    with db.write() as session:
        grant = session.get(ClientGrant, token_hash(TOKEN_A))
        grant.scopes = [*grant.scopes, "wardrobe.recommend"]
        session.add(
            Asset(
                id="concurrency-asset",
                sha256=content_hash,
                size=len(content),
                mime="image/png",
                width=4,
                height=4,
            )
        )
        for role, amount in [("upper", 3), ("lower", 2), ("footwear", 2)]:
            for i in range(amount):
                identifier = str(uuid4())
                session.add(
                    Garment(
                        id=identifier,
                        owner_id=OWNER_A,
                        name=f"Synthetic {role} {i}",
                        category={
                            "upper": "短袖T恤",
                            "lower": "长裤",
                            "footwear": "低帮运动鞋",
                        }[role],
                        roles=[role],
                        availability="unknown",
                        state="active",
                        revision=1,
                        annotation_fields={
                            "thickness": {
                                "value": "thin",
                                "verification_state": "user_verified",
                                "source_kind": "user_verified",
                            },
                            "sleeve": {"value": "短袖" if role == "upper" else None},
                            "primary_color": {
                                "value": "white",
                                "source_kind": "caller_supplied",
                                "verification_state": "unverified",
                            },
                        },
                        annotation_review_state="initial",
                        created_at=int(time.time()),
                    )
                )
                session.flush()
                session.add(
                    GarmentAsset(garment_id=identifier, asset_id="concurrency-asset")
                )

    class BarrierEvaluator(FixtureEvaluator):
        calls = 0

        async def evaluate(self, candidates, weather, occasion):
            self.calls += 1
            if self.calls <= 2:
                while self.calls < 2:
                    await asyncio.sleep(0.01)
            return await super().evaluate(candidates, weather, occasion)

    evaluator = BarrierEvaluator()
    service = RecommendationService(
        db,
        CatalogService(db.sessions, cursor_key),
        assets,
        settings.audience,
        FixtureWeather(),
        evaluator,
    )
    principal = Authenticator(db.sessions, settings.audience).authenticate(TOKEN_A)
    payload = RecommendationInput.model_validate(request_payload())
    first = service.submit(principal, payload, "concurrent-1")["data"][
        "recommendation_id"
    ]
    second = service.submit(principal, payload, "concurrent-2")["data"][
        "recommendation_id"
    ]
    claims = [service.claim(), service.claim()]

    async def run_both():
        async with asyncio.timeout(5):
            await asyncio.gather(*(service.process(*claim) for claim in claims))

    asyncio.run(run_both())
    values = [
        service.get(principal, identifier)["data"] for identifier in [first, second]
    ]
    assert all(value["state"] == "succeeded" for value in values), values
    signatures = [o["signature"] for v in values for o in v["result"]["outfits"]]
    assert len(signatures) == len(set(signatures)) == 4
    assert (
        evaluator.calls == 3
    )  # Losing commit reruns against the new history revision.
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(HistoryEvent)) == 4


def test_late_evaluator_result_is_never_published(live_service, monkeypatch):
    client, db = live_service
    seed_wardrobe(client, db)
    original = FixtureEvaluator().evaluate

    async def expire(self, candidates, weather, occasion):
        with db.write() as session:
            row = session.scalar(
                select(Recommendation).where(Recommendation.state == "running")
            )
            row.created_at = int(time.time()) - 130
            row.deadline = int(time.time()) - 1
        return await original(candidates, weather, occasion)

    monkeypatch.setattr(FixtureEvaluator, "evaluate", expire)
    identifier = submit(client, "expired-during-model")
    result = completed(client, identifier)
    assert (
        result["state"] == "failed"
        and result["error"]["code"] == "TASK_DEADLINE_EXCEEDED"
    )
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(HistoryEvent)) == 0
    mcp = rpc(
        client,
        "tools/call",
        {
            "name": "wardrobe.get_recommendation",
            "arguments": {"recommendation_id": identifier},
        },
    ).json()["result"]
    assert mcp["isError"]


def test_history_commit_failure_does_not_report_success(live_service):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    client, db = live_service
    seed_wardrobe(client, db)

    def fail_history(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ):
        if statement.startswith("INSERT INTO history_events"):
            raise OperationalError(
                "synthetic failing transaction", {}, RuntimeError("fixture")
            )

    event.listen(db.engine, "before_cursor_execute", fail_history)
    try:
        identifier = submit(client, "history-storage-failure")
        value = completed(client, identifier)
        assert value["state"] == "failed" and value["result"] is None
        with db.sessions.begin() as session:
            assert session.scalar(select(func.count()).select_from(HistoryEvent)) == 0
    finally:
        event.remove(db.engine, "before_cursor_execute", fail_history)


@pytest.mark.parametrize("no_thin_upper", [False, True])
def test_hot_weather_host_filter_is_enforced_before_model_and_history(
    live_service, monkeypatch, no_thin_upper
):
    from wardrobe.models import Garment

    client, db = live_service
    identifiers = seed_wardrobe(client, db)
    with db.write() as session:
        for index, identifier in enumerate(identifiers[:3]):
            garment = session.get(Garment, identifier)
            # Explicit synthetic user-confirmed labels, never inferred from photos.
            garment.category = "针织上衣" if index else "长袖衬衫"
            garment.annotation_fields = {
                **garment.annotation_fields,
                "sleeve": {"value": "长袖", "verification_state": "user_verified"},
                "thickness": {
                    "value": "medium" if no_thin_upper or index else "thin",
                    "verification_state": "user_verified",
                },
            }
    original_weather = FixtureWeather.fetch

    async def hot_weather(self, location, interval, now):
        weather = await original_weather(self, location, interval, now)
        return weather.model_copy(update={"temperature_max": 29.0})

    monkeypatch.setattr(FixtureWeather, "fetch", hot_weather)
    evaluated = []
    original_evaluate = FixtureEvaluator.evaluate

    async def inspected(self, candidates, weather, occasion):
        evaluated.append(candidates)
        assert not no_thin_upper
        assert all(
            i["garment"]["id"] == identifiers[0]
            for c in candidates
            for i in c["items"]
            if i["role"] == "upper"
        )
        return await original_evaluate(self, candidates, weather, occasion)

    monkeypatch.setattr(FixtureEvaluator, "evaluate", inspected)
    result = completed(client, submit(client, "hot-host-filter"))
    assert result["state"] == ("partial" if no_thin_upper else "succeeded"), result
    output = result["result"]
    assert output["search"]["temperature_check"]["pending_count"] == 0
    if no_thin_upper:
        assert evaluated == [] and output["outfits"] == []
        assert output["completeness"] == "partial" and output["missing"]
    else:
        assert len(evaluated) == 1 and len(output["outfits"]) == 2
        assert all(
            "气温 24–29°C；体感 24–26°C" in o["explanation"]["weather"]
            for o in output["outfits"]
        )
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(HistoryEvent)) == (
            0 if no_thin_upper else 2
        )


@pytest.mark.parametrize("mode", ["partial", "all", "key", "null", "value_missing"])
def test_qweather_only_feels_missing_uses_internal_unknown_context(mode):
    start = (datetime.now(UTC) + timedelta(hours=2)).replace(
        minute=0, second=0, microsecond=0
    )
    interval = Interval(
        start=start.isoformat(), end=(start + timedelta(hours=3)).isoformat()
    )
    raw = weather_raw(interval)
    for index, hour in enumerate(raw["hours"]):
        if mode == "key":
            del hour["feelsLike"]
        elif mode == "null":
            hour["feelsLike"] = None
        elif mode == "value_missing":
            del hour["feelsLike"]["value"]
            del hour["feelsLike"]["unit"]
        elif mode == "all" or index == 3:
            hour["feelsLike"]["value"] = None
    context = QWeather.normalize(raw, LOCATION, interval, int(time.time()))
    assert context.feels_like_missing
    assert context.feels_like_min is None and context.feels_like_max is None
    assert context.temperature_min == context.temperature_max == 20
    assert context.temperature_median == 20 and context.temperature_sample_count == 4
    assert context.feels_like_median is None and context.feels_like_sample_count == 0
    context.validate_for(LOCATION, interval, int(time.time()))


@pytest.mark.parametrize("field", ["temperature", "feelsLike"])
@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), -101, 81])
def test_qweather_invalid_value_is_failure_not_missing_fallback(field, bad):
    start = (datetime.now(UTC) + timedelta(hours=2)).replace(
        minute=0, second=0, microsecond=0
    )
    interval = Interval(
        start=start.isoformat(), end=(start + timedelta(hours=3)).isoformat()
    )
    raw = weather_raw(interval)
    raw["hours"][0]["feelsLike"]["value"] = None
    raw["hours"][1][field]["value"] = bad
    with pytest.raises(BusinessError) as error:
        QWeather.normalize(raw, LOCATION, interval, int(time.time()))
    assert error.value.code == "INVALID_WEATHER"


@pytest.mark.parametrize("field", ["temperature", "feelsLike"])
def test_qweather_invalid_unit_is_failure_alongside_missing_feels(field):
    start = (datetime.now(UTC) + timedelta(hours=2)).replace(
        minute=0, second=0, microsecond=0
    )
    interval = Interval(
        start=start.isoformat(), end=(start + timedelta(hours=3)).isoformat()
    )
    raw = weather_raw(interval)
    raw["hours"][0]["feelsLike"]["value"] = None
    raw["hours"][1][field]["unit"] = "K"
    with pytest.raises(BusinessError):
        QWeather.normalize(raw, LOCATION, interval, int(time.time()))
