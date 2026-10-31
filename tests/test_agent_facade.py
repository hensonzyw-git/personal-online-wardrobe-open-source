"""Synthetic recommendation operations; no external services."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import OWNER_A
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from wardrobe.app import create_app
from wardrobe.cli import grant_client
from wardrobe.models import AgentOperation
from wardrobe.seasons import initialize

TOKEN = "synthetic-agent-business-token-" * 3
READ = "synthetic-agent-read-token-" * 3
FOREIGN = "synthetic-agent-other-token-" * 3


@pytest.fixture
def agent_http(store):
    db, original, _ = store
    scopes = [
        "wardrobe.read",
        "wardrobe.recommend",
        "wardrobe.feedback",
        "wardrobe.season",
    ]
    for client, token, allowed in [
        ("agent-test", TOKEN, scopes),
        ("agent-read", READ, ["wardrobe.read"]),
        ("agent-other", FOREIGN, scopes),
    ]:
        grant_client(
            db,
            original,
            owner_id=OWNER_A,
            client_id=client,
            scopes=allowed,
            token=token,
        )
    initialize(db, OWNER_A, "autumn_winter")
    settings = original.model_copy(update={"agent_enabled": True})
    app = create_app(settings, database=db)
    # Drive operation workers explicitly for deterministic fault injection.
    client = TestClient(app, base_url="http://localhost")
    yield client, app.state.agents, db, SimpleNamespace(calls=0), settings
    client.close()


def headers(token=TOKEN, key=None, **values):
    return {
        "Authorization": "Bearer " + token,
        "X-Wardrobe-Agent-Protocol": "1",
        "Idempotency-Key": key or str(uuid4()),
        **values,
    }


def work_once(service):
    claim = service.claim()
    assert claim
    asyncio.run(service.advance(*claim))


def test_recommendation_reserves_date_then_projects_partial_read_only(agent_http):
    from wardrobe.models import Recommendation

    client, service, db, observer, _ = agent_http
    payload = {
        "location": {
            "name": "Synthetic city",
            "latitude": 31.0,
            "longitude": 121.0,
            "timezone": "UTC",
        },
        "target_date": "tomorrow",
    }
    response = client.post("/v1/agent/recommendations", headers=headers(), json=payload)
    assert response.status_code == 202, response.text
    ref = response.json()["operation_ref"]
    with db.sessions() as s:
        reserved = s.get(AgentOperation, ref).details["input"]
        assert reserved["target_date"] != "tomorrow"
        assert reserved["expected_season_revision"] == 1
    work_once(service)
    with db.write() as s:
        op = s.get(AgentOperation, ref)
        row = s.get(Recommendation, op.recommendation_id)
        row.state = "partial"
        row.result = {"outfits": [], "completeness": "partial", "missing": ["footwear"]}
        before = row.payload
    for _ in range(2):
        result = client.get("/v1/agent/operations/" + ref, headers=headers()).json()
        assert result["status"] == "partial", result
    with db.sessions() as s:
        assert s.get(Recommendation, row.id).payload == before
    assert observer.calls == 0


def test_capabilities_are_read_only_and_protocol_blocks_writes(agent_http):
    client, service, db, _, _ = agent_http
    with db.sessions() as s:
        before = s.scalar(select(func.count()).select_from(AgentOperation))
    capability = client.get("/v1/agent/capabilities", headers=headers()).json()
    assert capability["data"]["tools"] == {
        "query": True,
        "recommend": True,
        "feedback": True,
        "season": True,
    }
    assert capability["data"]["dependencies"]["supplier_health"] == "unprobed"
    body = {"location": {"name": "Synthetic city", "latitude": 31, "longitude": 121}}
    response = client.post(
        "/v1/agent/recommendations",
        json=body,
        headers={**headers(), "X-Wardrobe-Agent-Protocol": "2"},
    )
    assert response.status_code == 426
    assert response.json()["error"]["code"] == "AGENT_PROTOCOL_UNSUPPORTED"
    assert (
        client.post(
            "/v1/agent/recommendations", headers=headers(READ), json=body
        ).status_code
        == 403
    )
    service.settings = service.settings.model_copy(update={"agent_enabled": False})
    assert (
        client.get("/v1/agent/capabilities", headers=headers()).json()["data"][
            "enabled"
        ]
        is False
    )
    assert (
        client.post(
            "/v1/agent/recommendations", headers=headers(), json=body
        ).status_code
        == 503
    )
    with db.sessions() as s:
        assert before == s.scalar(select(func.count()).select_from(AgentOperation))


def test_worker_recovers_from_temporary_claim_database_outage(agent_http, monkeypatch):
    from sqlalchemy.exc import OperationalError

    client, service, _, _, _ = agent_http
    response = client.post(
        "/v1/agent/recommendations",
        headers=headers(),
        json={
            "location": {"name": "Synthetic city", "latitude": 31, "longitude": 121},
            "target_date": "tomorrow",
        },
    )
    assert response.status_code == 202
    ref = response.json()["operation_ref"]
    real_claim = service.claim
    calls = []

    def flaky_claim():
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError(
                "synthetic", {}, Exception("synthetic-private-detail")
            )
        return real_claim()

    monkeypatch.setattr(service, "claim", flaky_claim)

    async def exercise():
        worker = asyncio.create_task(service.run())
        try:
            for _ in range(300):
                with service.db.sessions() as s:
                    if s.get(AgentOperation, ref).recommendation_id:
                        return
                await asyncio.sleep(0.01)
            pytest.fail("worker did not recover")
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

    asyncio.run(exercise())
    assert len(calls) >= 2
