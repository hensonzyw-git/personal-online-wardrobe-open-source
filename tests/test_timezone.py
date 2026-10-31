"""Configured defaults, explicit locations, DST and durable replay boundaries."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from conftest import OWNER_A
from fastapi.testclient import TestClient
from pydantic import ValidationError

from wardrobe.app import create_app
from wardrobe.cli import grant_client
from wardrobe.config import Settings
from wardrobe.models import AgentOperation, Recommendation
from wardrobe.recommendations import RecommendationInput, RecommendationService
from wardrobe.seasons import initialize

TOKEN = "synthetic-timezone-recommendation-token-" * 3


@pytest.mark.parametrize("zone", ["UTC", "America/New_York", "Europe/Berlin"])
def test_timezone_environment_and_constructor(monkeypatch, zone):
    monkeypatch.delenv("WARDROBE_TIMEZONE", raising=False)
    assert Settings.from_env().timezone == "UTC"
    assert Settings(timezone=zone).timezone == zone
    monkeypatch.setenv("WARDROBE_TIMEZONE", zone)
    assert Settings.from_env().timezone == zone


@pytest.mark.parametrize(
    "zone",
    ["", "Invalid/Zone", "../UTC", "/etc/localtime", "UTC+08:00", " UTC", "UTC\0"],
)
def test_invalid_timezone_fails_configuration(monkeypatch, zone):
    with pytest.raises(ValidationError, match="IANA timezone"):
        Settings(timezone=zone)
    monkeypatch.setenv("WARDROBE_TIMEZONE", zone.replace("\0", "invalid"))
    with pytest.raises(ValidationError, match="IANA timezone"):
        Settings.from_env()


@pytest.mark.parametrize("transport", ["http", "agent"])
@pytest.mark.parametrize(
    "configured,explicit,day,offset",
    [
        ("UTC", None, "2027-03-15", "+00:00"),
        ("America/New_York", None, "2027-03-14", "-04:00"),
        ("Europe/Berlin", None, "2027-03-15", "+01:00"),
        ("America/New_York", "UTC", "2027-03-15", "+00:00"),
        ("UTC", "America/New_York", "2027-03-14", "-04:00"),
    ],
)
def test_default_timezone_and_explicit_precedence_survive_replay(
    store, monkeypatch, transport, configured, explicit, day, offset
):
    db, original, _ = store
    settings = Settings(
        data_root=original.data_root, timezone=configured, agent_enabled=True
    )
    grant_client(
        db,
        settings,
        owner_id=OWNER_A,
        client_id="client-a",
        scopes=["wardrobe.read", "wardrobe.recommend"],
        token=TOKEN,
    )
    initialize(db, OWNER_A, "autumn_winter")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            # New York is still on the previous calendar day at submission.
            return datetime(2027, 3, 14, 1, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("wardrobe.recommendations.datetime", Clock)
    monkeypatch.setattr("wardrobe.agent_service.datetime", Clock)
    app = create_app(settings, database=db)
    body = {
        "location": {"name": "Synthetic city", "latitude": 40, "longitude": -74},
        "target_date": "tomorrow",
    }
    if explicit is not None:
        body["location"]["timezone"] = explicit
    path = "/v1/recommendations" if transport == "http" else "/v1/agent/recommendations"
    headers = {
        "Authorization": "Bearer " + TOKEN,
        "Idempotency-Key": "timezone-replay",
        "X-Wardrobe-Agent-Protocol": "1",
    }

    def submit():
        # One client per thread: exercise the real service write transaction.
        with_client = TestClient(app, base_url="http://localhost")
        try:
            response = with_client.post(path, headers=headers, json=body)
            assert response.status_code == 202, response.text
            value = response.json()
            return (
                value["data"]["recommendation_id"]
                if transport == "http"
                else value["operation_ref"]
            )
        finally:
            with_client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        refs = list(pool.map(lambda _: submit(), range(2)))
    assert refs[0] == refs[1]
    if transport == "agent":
        with db.sessions() as session:
            frozen = session.get(AgentOperation, refs[0]).details["input"]
            assert frozen["target_date"] == day
            assert frozen["location"]["timezone"] == (explicit or configured)
        claimed = app.state.agents.claim()
        assert claimed
        asyncio.run(app.state.agents.advance(*claimed))
        with db.sessions() as session:
            identifier = session.get(AgentOperation, refs[0]).recommendation_id
    else:
        identifier = refs[0]
    with db.sessions() as session:
        before = session.get(Recommendation, identifier).payload
        assert before["location"]["timezone"] == (explicit or configured)
        assert before["interval"] == {
            "start": day + "T09:00:00" + offset,
            "end": day + "T23:00:00" + offset,
        }
    # Reconfiguration between response loss and replay must not resolve a new zone.
    app.state.agents.recommendations.default_timezone = (
        "Europe/Berlin" if configured != "Europe/Berlin" else "UTC"
    )
    assert submit() == refs[0]
    with db.sessions() as session:
        assert session.get(Recommendation, identifier).payload == before
    if explicit is None:
        # An omitted timezone and explicit UTC are distinct arguments for a key.
        body["location"]["timezone"] = "UTC"
        client = TestClient(app, base_url="http://localhost")
        try:
            response = client.post(path, headers=headers, json=body)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        finally:
            client.close()


def test_user_weather_omitted_timezone_uses_the_same_default():
    from test_recommendations import LOCATION, FixtureWeather

    from wardrobe.weather import Interval

    interval = Interval(
        start="2030-01-01T09:00:00-05:00", end="2030-01-01T23:00:00-05:00"
    )
    weather = asyncio.run(FixtureWeather().fetch(LOCATION, interval, 100)).model_dump()
    weather["source_kind"] = "user_input"
    weather["location"].pop("timezone")
    payload = RecommendationInput(
        location=weather["location"], interval=interval, user_weather=weather
    )
    # Resolve without storage/network; all field validation has already run.
    service = object.__new__(RecommendationService)
    service.default_timezone = "America/New_York"
    resolved = service.with_default_timezone(payload)
    assert resolved.location.timezone == "America/New_York"
    assert resolved.user_weather.location == resolved.location
    assert payload.location.timezone == "UTC"
