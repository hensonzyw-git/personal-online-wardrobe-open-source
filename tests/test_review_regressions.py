import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import TOKEN_A
from sqlalchemy import func, select
from test_catalog_writes import (
    assertion,
    create,
    decide,
    enable_write,
    human_setup,
    png,
    propose,
    upload,
)
from test_foundation import headers, rpc
from test_recommendations import (
    LOCATION,
    FixtureEvaluator,
    FixtureWeather,
    completed,
    request_payload,
    seed_wardrobe,
    submit,
)

from wardrobe.annotations import AnnotationService, VisionOutput
from wardrobe.auth import Authenticator
from wardrobe.catalog import CatalogService
from wardrobe.db import load_cursor_key
from wardrobe.errors import BusinessError
from wardrobe.models import AnnotationVersion, Recommendation
from wardrobe.weather import Interval


@pytest.fixture
def providers():
    return {"weather": FixtureWeather(), "evaluator": FixtureEvaluator()}


def vision_draft(db, garment, asset, values, *, revision=1, key="vision"):
    output = VisionOutput.model_validate(
        {
            "schema_version": "wardrobe-labels/1",
            "model_version": "synthetic-v1",
            "prompt_version": "synthetic-v1",
            "values": values,
            "evidence_asset_ids": [asset],
            "multi_subject": False,
        }
    )
    service = AnnotationService(
        db, CatalogService(db.sessions, load_cursor_key(db.path.parent))
    )
    principal = Authenticator(db.sessions, "open-wardrobe").authenticate(TOKEN_A)
    return service.save_vision_output(principal, garment, revision, output, key)


@pytest.mark.parametrize("explicit_clear", [False, True])
def test_vision_publication_preserves_omitted_fields(live_service, explicit_clear):
    client, db = live_service
    enable_write(db)
    key = human_setup(db)
    asset = upload(client, png())
    response = client.post(
        "/v1/garments",
        headers={**headers(), "Idempotency-Key": "create"},
        json={
            "name": "Synthetic",
            "category": "shirt",
            "roles": ["upper"],
            "asset_ids": [asset],
            "annotation_fields": {"primary_color": "blue", "style": ["casual"]},
        },
    )
    assert response.status_code == 200, response.text
    garment = response.json()["data"]["garment_id"]
    url = f"/v1/garments/{garment}"
    before = client.get(url, headers=headers()).json()["data"]["garment"][
        "annotation_fields"
    ]
    values = {"material": "cotton"}
    if explicit_clear:
        values["primary_color"] = None
    version = vision_draft(db, garment, asset, values)["data"]["annotation_version_id"]
    with db.sessions.begin() as session:
        assert set(session.get(AnnotationVersion, version).fields) == set(values)
    # Saving suggestions has no effect before the full signed approval path.
    assert (
        client.get(url, headers=headers()).json()["data"]["garment"][
            "annotation_fields"
        ]
        == before
    )
    proposal = propose(
        client,
        garment,
        operation="publish_annotation",
        patch={"annotation_version_id": version},
    )
    assert set(proposal["changes"][0]["before"]["annotation_draft"]) == set(values)
    assert decide(client, assertion(key, proposal)).status_code == 200
    labels = client.get(url, headers=headers()).json()["data"]["garment"][
        "annotation_fields"
    ]
    assert labels["style"] == before["style"]
    if explicit_clear:
        assert labels["primary_color"]["value"] is None
        assert labels["primary_color"]["source_kind"] == "unknown"
    else:
        assert labels["primary_color"] == before["primary_color"]
    assert labels["material"]["value"] == "cotton"
    assert labels["material"]["source_kind"] == "inferred"
    assert labels["material"]["verification_state"] == "unverified"


def test_omitted_verified_field_allows_publish_but_explicit_conflict_does_not(
    live_service,
):
    client, db = live_service
    enable_write(db)
    key = human_setup(db)
    asset = upload(client, png())
    garment = create(client, asset)["data"]["garment_id"]
    suggestion = client.post(
        "/v1/annotation-suggestions",
        headers={**headers(), "Idempotency-Key": "suggest"},
        json={
            "garment_id": garment,
            "base_revision": 1,
            "values": {"primary_color": "blue"},
        },
    ).json()["data"]["annotation_version_id"]
    first = propose(
        client,
        garment,
        operation="publish_annotation",
        patch={"annotation_version_id": suggestion},
        key="publish-first",
    )
    assert decide(client, assertion(key, first)).status_code == 200
    verification = propose(
        client,
        garment,
        operation="verify_fields",
        patch={"verified_fields": ["primary_color"]},
        revision=2,
        key="verify",
    )
    assert decide(client, assertion(key, verification)).status_code == 200
    before = client.get(f"/v1/garments/{garment}", headers=headers()).json()["data"][
        "garment"
    ]["annotation_fields"]["primary_color"]
    version = vision_draft(db, garment, asset, {"material": "cotton"}, revision=3)[
        "data"
    ]["annotation_version_id"]
    publication = propose(
        client,
        garment,
        operation="publish_annotation",
        patch={"annotation_version_id": version},
        revision=3,
        key="publish-vision",
    )
    assert decide(client, assertion(key, publication)).status_code == 200
    assert (
        client.get(f"/v1/garments/{garment}", headers=headers()).json()["data"][
            "garment"
        ]["annotation_fields"]["primary_color"]
        == before
    )
    clear = vision_draft(
        db, garment, asset, {"primary_color": None}, revision=4, key="clear"
    )["data"]["annotation_version_id"]
    conflict = client.post(
        "/v1/change-proposals",
        headers={**headers(), "Idempotency-Key": "conflicting-publication"},
        json={
            "changes": [
                {
                    "target_id": garment,
                    "base_revision": 4,
                    "operation": "publish_annotation",
                    "patch": {"annotation_version_id": clear},
                }
            ]
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "VERIFIED_FIELD_CONFLICT"


def test_vision_idempotency_distinguishes_omitted_and_explicit_null(live_service):
    client, db = live_service
    enable_write(db)
    asset = upload(client, png())
    garment = create(client, asset)["data"]["garment_id"]
    first = vision_draft(db, garment, asset, {"material": "cotton"})
    assert vision_draft(db, garment, asset, {"material": "cotton"}) == first
    with pytest.raises(BusinessError) as caught:
        vision_draft(db, garment, asset, {"material": "cotton", "primary_color": None})
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(AnnotationVersion)) == 1


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
def test_unexpected_mcp_errors_are_redacted_in_response_and_logs(
    live_service, monkeypatch, caplog, version
):
    client, _db = live_service
    marker = "SYNTHETIC_PRIVATE_PATH_AND_TOKEN"

    def unavailable(*_args):
        raise RuntimeError(marker)

    monkeypatch.setattr(CatalogService, "list_garments", unavailable)
    session_headers = {}
    if version == "2025-11-25":
        initialized = rpc(
            client,
            "initialize",
            {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "synthetic-review", "version": "1"},
            },
            version=version,
        )
        assert initialized.status_code == 200, initialized.text
        session_headers = {"Mcp-Session-Id": initialized.headers["mcp-session-id"]}
        client.post(
            "/mcp",
            headers={
                **headers(),
                **session_headers,
                "MCP-Protocol-Version": version,
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
    with caplog.at_level(logging.ERROR, logger="wardrobe.app"):
        response = rpc(
            client,
            "tools/call",
            {
                "name": "wardrobe.list_garments",
                "arguments": {"limit": 2},
            },
            version=version,
            extra=session_headers,
        )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"]
    assert result["structuredContent"]["error"]["code"] == "INTERNAL_ERROR"
    assert marker not in response.text and marker not in caplog.text
    assert "RuntimeError" in caplog.text
    assert result["structuredContent"]["request_id"] in caplog.text


@pytest.mark.parametrize("use_date", [False, True])
def test_past_forecast_rejected_before_job_creation_across_api_and_mcp(
    live_service, use_date
):
    client, db = live_service
    seed_wardrobe(client, db)
    if use_date:
        payload = request_payload()
        payload.pop("interval")
        payload["target_date"] = (
            datetime.now(ZoneInfo(LOCATION.timezone)).date() - timedelta(days=1)
        ).isoformat()
    else:
        start = datetime.now(UTC) - timedelta(days=10)
        payload = request_payload(
            interval={
                "start": start.isoformat(),
                "end": (start + timedelta(hours=3)).isoformat(),
            }
        )
    api = client.post(
        "/v1/recommendations",
        headers={**headers(), "Idempotency-Key": "past-api"},
        json=payload,
    )
    assert api.status_code == 422, api.text
    assert api.json()["error"]["code"] == "WEATHER_OUT_OF_RANGE"
    result = rpc(
        client,
        "tools/call",
        {
            "name": "wardrobe.recommend_outfits",
            "arguments": {"payload": payload, "key": "past-mcp"},
        },
    ).json()["result"]
    assert result["isError"]
    assert result["structuredContent"]["error"]["code"] == "WEATHER_OUT_OF_RANGE"
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(Recommendation)) == 0


def test_past_user_weather_is_explicit_retrospective_context(live_service, monkeypatch):
    client, db = live_service
    seed_wardrobe(client, db)
    start = datetime.now(UTC) - timedelta(days=10)
    interval = Interval(
        start=start.isoformat(), end=(start + timedelta(hours=3)).isoformat()
    )
    weather = asyncio.run(FixtureWeather().fetch(LOCATION, interval, int(time.time())))

    async def forbidden_fetch(*_args):
        raise AssertionError("Explicit user weather must not call a forecast provider")

    monkeypatch.setattr(FixtureWeather, "fetch", forbidden_fetch)
    payload = request_payload(
        interval=interval.model_dump(),
        user_weather={
            **weather.model_dump(),
            "source_kind": "user_input",
        },
    )
    identifier = submit(client, "past-user-weather", payload)
    result = completed(client, identifier)
    assert result["state"] == "succeeded", result
    assert result["result"]["weather_acceptance_eligible"] is False
    assert any("retrospective" in warning for warning in result["result"]["warnings"])


def test_forecast_rechecked_when_target_interval_ends():
    end = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
    interval = Interval(
        start=(end - timedelta(hours=2)).isoformat(), end=end.isoformat()
    )
    now = int(end.timestamp())
    weather = asyncio.run(FixtureWeather().fetch(LOCATION, interval, now - 1))
    weather.validate_for(LOCATION, interval, now - 1)
    with pytest.raises(BusinessError) as caught:
        weather.validate_for(LOCATION, interval, now)
    assert caught.value.code == "WEATHER_OUT_OF_RANGE"
    weather.model_copy(update={"source_kind": "user_input"}).validate_for(
        LOCATION, interval, now
    )
