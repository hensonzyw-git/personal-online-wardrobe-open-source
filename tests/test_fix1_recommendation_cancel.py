"""Fix1 contracts with synthetic grants and real SQLite write transactions."""

import asyncio
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

import pytest
from conftest import OWNER_A
from sqlalchemy import event, func, select
from test_agent_facade import (  # noqa: F401
    FOREIGN,
    TOKEN,
    agent_http,
    headers,
    work_once,
)
from test_catalog_writes import png
from test_recommendations import FixtureWeather

from wardrobe.auth import token_hash
from wardrobe.cli import grant_client
from wardrobe.contracts import Principal
from wardrobe.errors import BusinessError
from wardrobe.models import (
    AgentOperation,
    Asset,
    ClientGrant,
    Garment,
    GarmentAsset,
    HistoryEvent,
    Idempotency,
    Recommendation,
    ServiceState,
)
from wardrobe.recommendations import (
    RecommendationInput,
    candidates_for,
    outfit_explanation,
)
from wardrobe.thermal_budget import (
    assumptions_for,
    full_day_allowed,
    selected_output,
    thermal_context,
)
from wardrobe.weather import (
    Interval,
    Location,
    weather_context,
    weather_output,
)

READ_SAME = "synthetic-same-client-read-token-" * 3
PAYLOAD = {
    "location": {
        "name": "Synthetic city",
        "latitude": 31.0,
        "longitude": 121.0,
        "timezone": "UTC",
    },
    "target_date": "tomorrow",
}


@pytest.fixture
def fix1(request):
    client, service, db, observer, settings = request.getfixturevalue("agent_http")
    grant_client(
        db,
        settings,
        owner_id=OWNER_A,
        client_id="agent-test",
        scopes=["wardrobe.read"],
        token=READ_SAME,
    )
    return client, service, db, observer


def recommend(client, *, key=None, count=2):
    key = key or str(uuid4())
    response = client.post(
        "/v1/agent/recommendations",
        headers=headers(key=key),
        json={**PAYLOAD, "count": count},
    )
    assert response.status_code == 202, response.text
    return response.json()["operation_ref"], key


def cancel(client, ref, *, token=TOKEN, key=None, body=None):
    return client.post(
        f"/v1/agent/operations/{ref}/cancel",
        headers=headers(token, key=key),
        json={} if body is None else body,
    )


def get(client, ref):
    return client.get(f"/v1/agent/operations/{ref}", headers=headers())


def count(s, model):
    return s.scalar(select(func.count()).select_from(model))


def assert_cancelled(client, service, db, ref, key):
    assert get(client, ref).json()["status"] == "cancelled"
    assert (
        client.get(
            "/v1/agent/operations/by-key",
            headers=headers(),
            params={"kind": "recommendation", "key": key},
        ).json()["status"]
        == "cancelled"
    )
    assert service.claim() is None
    response = client.post(f"/v1/agent/operations/{ref}/resume", headers=headers())
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    with db.sessions() as s:
        op = s.get(AgentOperation, ref)
        assert (op.state, op.stage, op.lease_token, op.lease_until) == (
            "cancelled",
            "closed",
            None,
            0,
        )


def test_recommendation_resume_requires_same_client_write_scope(fix1):
    client, _, db, _ = fix1
    ref, _ = recommend(client)
    with db.write() as s:
        s.get(AgentOperation, ref).state = "needs_input"
    response = client.post(
        f"/v1/agent/operations/{ref}/resume", headers=headers(READ_SAME)
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "SCOPE_DENIED"
    with db.sessions() as s:
        assert s.get(AgentOperation, ref).state == "needs_input"
    assert get(client, ref).status_code == 200
    assert (
        client.post(f"/v1/agent/operations/{ref}/resume", headers=headers()).status_code
        == 200
    )


def test_recommendation_cancel_operation_succeeds_from_needs_input(fix1):
    client, service, db, observer = fix1
    ref, key = recommend(client)
    with db.write() as s:
        op = s.get(AgentOperation, ref)
        op.state, op.lease_token, op.lease_until = "needs_input", "old-lease", 1
    response = cancel(client, ref)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert_cancelled(client, service, db, ref, key)
    assert observer.calls == 0


def test_recommendation_enqueue_atomic_rollback(fix1):
    client, service, db, _ = fix1
    ref, _ = recommend(client)
    claim = service.claim()
    assert claim and claim[0] == ref
    flushed = []

    def fail_binding(session, *_):
        for obj in session.dirty:
            if (
                isinstance(obj, AgentOperation)
                and obj.id == ref
                and obj.recommendation_id
            ):
                # Verify downstream insertion has really flushed before the fault.
                assert session.get(Recommendation, obj.recommendation_id) is not None
                flushed.append(obj.recommendation_id)
                raise RuntimeError("synthetic binding failure")

    event.listen(db.sessions.class_, "before_flush", fail_binding)
    try:
        with pytest.raises(RuntimeError, match="synthetic binding failure"):
            asyncio.run(service.advance(*claim))
    finally:
        event.remove(db.sessions.class_, "before_flush", fail_binding)
    assert flushed
    with db.sessions() as s:
        orphan_jobs = count(s, Recommendation)
        orphan_receipts = count(s, Idempotency)
        assert (orphan_jobs, orphan_receipts) == (0, 0), (
            f"orphan Recommendation={orphan_jobs}, idempotency={orphan_receipts}"
        )
        op = s.get(AgentOperation, ref)
        assert op.recommendation_id is None and op.state == "processing"
        assert op.lease_token == claim[1]
    asyncio.run(service.advance(*claim))
    # Simulate committed enqueue response loss, resume and advance again.
    assert (
        client.post(f"/v1/agent/operations/{ref}/resume", headers=headers()).status_code
        == 200
    )
    work_once(service)
    with db.sessions() as s:
        op = s.get(AgentOperation, ref)
        assert op.recommendation_id and count(s, Recommendation) == 1
        assert count(s, Idempotency) == 2  # recommend + resume


@pytest.mark.parametrize(
    "method",
    [
        "resume",
        "advance",
    ],
)
def test_recommendation_write_call_sites_require_scope(fix1, method):
    _, service, db, _ = fix1
    ref, _ = recommend(fix1[0])
    with db.write() as s:
        grant = s.get(ClientGrant, token_hash(READ_SAME))
        p = Principal(
            OWNER_A,
            "agent-test",
            frozenset(grant.scopes),
            grant.channel,
            grant.audience,
            grant.token_hash,
        )
        if method == "advance":
            s.get(AgentOperation, ref).grant_id = grant.token_hash
    calls = {
        "resume": lambda: service.resume(p, ref, "scope-test"),
        "advance": lambda: asyncio.run(service.advance(ref, "lease")),
    }
    with pytest.raises(BusinessError) as error:
        calls[method]()
    assert (error.value.code, error.value.http_status) == ("SCOPE_DENIED", 403)


def test_recommendation_cancel_requires_recommend_scope(fix1):
    client, _, db, _ = fix1
    ref, _ = recommend(client)
    response = cancel(client, ref, token=READ_SAME)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "SCOPE_DENIED"
    with db.sessions() as s:
        assert s.get(AgentOperation, ref).state == "processing"
        assert count(s, Idempotency) == 0
    assert cancel(client, ref, token=FOREIGN).status_code == 404
    assert cancel(client, str(uuid4())).status_code == 404


@pytest.mark.parametrize(
    "state", ["receiving", "processing", "needs_confirmation", "unknown"]
)
def test_recommendation_cancel_nonterminal_and_idempotency(fix1, state):
    client, service, db, _ = fix1
    ref, _ = recommend(client)
    with db.write() as s:
        op = s.get(AgentOperation, ref)
        op.state = state
        op.details = {**op.details, "error_code": "OPERATION_OUTCOME_UNKNOWN"}
    cancel_key = str(uuid4())
    first = cancel(client, ref, key=cancel_key)
    assert first.status_code == 200, first.text
    assert cancel(client, ref, key=cancel_key).json() == first.json()
    assert cancel(client, ref).status_code == 200
    other, _ = recommend(client)
    conflict = cancel(client, other, key=cancel_key)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert get(client, ref).json()["status"] == "cancelled"
    asyncio.run(service.advance(ref, "stale"))
    with db.sessions() as s:
        assert (
            s.get(AgentOperation, ref).details["error_code"]
            == "OPERATION_OUTCOME_UNKNOWN"
        )
        assert count(s, Recommendation) == 0
        assert count(s, Idempotency) == 2
    assert (
        client.post(
            f"/v1/agent/operations/{ref}/cancel",
            headers={
                "Authorization": "Bearer " + TOKEN,
                "X-Wardrobe-Agent-Protocol": "1",
            },
            json={},
        ).status_code
        == 422
    )
    assert cancel(client, ref, body={"view_ref": "a" * 64}).status_code == 422


@pytest.mark.parametrize(
    ("state", "downstream"),
    [
        ("succeeded", False),
        ("failed", False),
        ("succeeded", True),
        ("partial", True),
        ("failed", True),
    ],
)
def test_recommendation_cancel_terminal_conflict(fix1, downstream, state):
    client, service, db, _ = fix1
    ref, _ = recommend(client)
    if downstream:
        work_once(service)
    with db.write() as s:
        op = s.get(AgentOperation, ref)
        row = s.get(Recommendation, op.recommendation_id) if downstream else op
        row.state = state
        if downstream and state == "failed":
            row.error = {"code": "UPSTREAM_RESULT_UNKNOWN"}
    response = cancel(client, ref)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "OPERATION_NOT_CANCELLABLE"
    with db.sessions() as s:
        assert s.get(AgentOperation, ref).state == (
            "needs_input" if downstream else state
        )
        assert (
            s.scalar(
                select(func.count())
                .select_from(Idempotency)
                .where(Idempotency.action == "agent-cancel-operation")
            )
            == 0
        )


@pytest.mark.parametrize("state", ["queued", "running", "cancelled"])
def test_recommendation_cancel_seals_downstream_job(fix1, state):
    client, service, db, _ = fix1
    ref, key = recommend(client)
    work_once(service)
    with db.write() as s:
        row = s.get(Recommendation, s.get(AgentOperation, ref).recommendation_id)
        row.state, row.lease_id, row.lease_until = state, "old-lease", 123
        identifier = row.id
    response = cancel(client, ref)
    assert response.status_code == 200, response.text
    assert_cancelled(client, service, db, ref, key)
    with db.sessions() as s:
        row = s.get(Recommendation, identifier)
        assert row.state == "cancelled"
        if state != "cancelled":
            assert row.lease_id is None and row.lease_until == 0
    assert service.recommendations.claim() is None
    # A stale projection must never override the cancelled operation.
    with db.write() as s:
        s.get(Recommendation, identifier).state = "partial"
    assert get(client, ref).json()["status"] == "cancelled"


def test_operations_cancel_legacy_kind_not_found(fix1):
    client, _, db, _ = fix1
    ref, _ = recommend(client)
    with db.write() as s:
        s.get(AgentOperation, ref).kind = "intake"
        before = count(s, Idempotency)
    response = cancel(client, ref)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert get(client, ref).status_code == 404
    with db.sessions() as s:
        assert count(s, Idempotency) == before


def publication(fix1):
    client, service, db, _ = fix1
    ref, key = recommend(client, count=1)
    work_once(service)
    recs = service.recommendations
    content = png()
    sha = hashlib.sha256(content).hexdigest()
    (recs.assets.originals / sha).write_bytes(content)
    asset_id = str(uuid4())
    with db.write() as session:
        session.add(
            Asset(
                id=asset_id,
                sha256=sha,
                size=len(content),
                mime="image/png",
                width=4,
                height=4,
            )
        )
        for role, category in [
            ("upper", "短袖T恤"),
            ("lower", "长裤"),
            ("footwear", "低帮运动鞋"),
        ]:
            identifier = str(uuid4())
            session.add(
                Garment(
                    id=identifier,
                    owner_id=OWNER_A,
                    name="Synthetic publication " + role,
                    category=category,
                    roles=[role],
                    availability="unknown",
                    state="active",
                    revision=1,
                    annotation_fields={
                        "sleeve": {"value": "短袖" if role == "upper" else None},
                        "thickness": {
                            "value": "thin",
                            "verification_state": "user_verified",
                        },
                    },
                    annotation_review_state="initial",
                    created_at=int(time.time()),
                )
            )
            session.flush()
            session.add(GarmentAsset(garment_id=identifier, asset_id=asset_id))
    identifier, lease = recs.claim()
    p, data, _, catalog_rev, history_rev, garments, _ = recs._snapshot(
        identifier, lease
    )
    weather = asyncio.run(
        FixtureWeather().fetch(
            Location.model_validate(data["location"]),
            Interval.model_validate(data["interval"]),
            int(time.time()),
        )
    )
    weather = weather_context(weather)
    payload = RecommendationInput.model_validate(
        {k: v for k, v in data.items() if k != "assumptions"}
    )
    candidates, search = candidates_for(garments, payload, weather)
    assert candidates
    # Test-only fixture repair: thin T at 24–26°C is within the budget
    # (W=1.5, G=1). Project internal candidates into a publishable result,
    # as process() does, so these tests reach the real commit/cancel boundary.
    ctx = thermal_context(
        weather,
        outerwear_required=search["layering_check"]["outerwear_required"],
        full_day=full_day_allowed(payload.occasion),
    )
    outfits = [
        {
            **{k: v for k, v in candidates[0].items() if not k.startswith("_")},
            "outfit_id": str(uuid4()),
            "assessment": dict.fromkeys(
                ("color", "material", "style", "silhouette"), 0.8
            ),
        }
    ]
    search["layering_check"]["thermal_budget"]["selected_outfits"] = selected_output(
        outfits, ctx, search.guard
    )
    for outfit in outfits:
        outfit["explanation"] = outfit_explanation(outfit, weather, ctx, search.guard)
    result = {
        "completeness": "complete",
        "location": data["location"],
        "interval": data["interval"],
        "weather": weather_output(weather),
        "search": search,
        "outfits": outfits,
        "assumptions": assumptions_for(outfits, ctx),
    }
    args = (p, identifier, lease, catalog_rev, history_rev, weather, result)
    return ref, key, args


def test_recommendation_cancel_before_commit_blocks_publication(fix1):
    client, service, db, observer = fix1
    ref, key, args = publication(fix1)
    at_commit, release = threading.Event(), threading.Event()
    recs = service.recommendations
    calls = []

    def fake_process():
        calls.append("synthetic evaluation")
        at_commit.set()
        assert release.wait(5), "release fake process"
        try:
            recs._commit(*args)
        except BusinessError as error:
            recs._fail(args[1], args[2], error.code, "synthetic late result")
            return error.code
        pytest.fail("cancelled downstream published a late result")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fake_process)
        try:
            assert at_commit.wait(5)
            response = cancel(client, ref)
            assert response.status_code == 200, response.text
            with db.sessions() as s:
                row = s.get(Recommendation, args[1])
                assert (row.state, row.lease_id, row.lease_until) == (
                    "cancelled",
                    None,
                    0,
                )
        finally:
            release.set()
        assert future.result(timeout=5) == "TASK_DEADLINE_EXCEEDED"
    assert_cancelled(client, service, db, ref, key)
    with db.sessions() as s:
        assert count(s, HistoryEvent) == 0
        assert s.get(ServiceState, OWNER_A).history_revision == args[4]
        assert s.get(Recommendation, args[1]).state == "cancelled"
        assert count(s, Recommendation) == 1
    assert len(calls) == 1 and observer.calls == 0


def test_recommendation_commit_before_cancel_conflicts(fix1, monkeypatch):
    client, service, db, _ = fix1
    ref, _, args = publication(fix1)
    held, attempted, release = threading.Event(), threading.Event(), threading.Event()
    original = db.write

    def commit():
        threading.current_thread().name = "fix1-commit"
        return service.recommendations._commit(*args)

    @contextmanager
    def gated_write():
        if threading.current_thread().name != "fix1-commit":
            attempted.set()
        with original() as s:
            yield s
            if threading.current_thread().name == "fix1-commit":
                s.flush()
                held.set()
                assert release.wait(5), "release commit transaction"

    monkeypatch.setattr(db, "write", gated_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(commit)
        try:
            assert held.wait(5), "real _commit holds BEGIN IMMEDIATE"
            second = pool.submit(cancel, client, ref)
            assert attempted.wait(5), "cancel attempted database.write"
        finally:
            release.set()
        assert first.result(timeout=5) is True
        response = second.result(timeout=5)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "OPERATION_NOT_CANCELLABLE"
    with db.sessions() as s:
        assert s.get(AgentOperation, ref).state != "cancelled"
        assert s.get(Recommendation, args[1]).state == "succeeded"
        assert count(s, HistoryEvent) == 1
        assert s.get(ServiceState, OWNER_A).history_revision == args[4] + 1
        assert not s.scalar(
            select(Idempotency).where(Idempotency.action == "agent-cancel-operation")
        )


def test_recommendation_cancel_seal_zero_rows_conflicts(fix1):
    client, service, db, _ = fix1
    ref, _, args = publication(fix1)
    assert service.recommendations._commit(*args)
    with pytest.raises(BusinessError) as error, db.write() as s:
        service.recommendations.seal_cancelled(s, args[1])
    assert (error.value.code, error.value.http_status) == (
        "OPERATION_NOT_CANCELLABLE",
        409,
    )
    assert cancel(client, ref).status_code == 409


@pytest.mark.parametrize("enqueue_first", [False, True])
def test_recommendation_cancel_enqueue_interleaving(fix1, monkeypatch, enqueue_first):
    client, service, db, _ = fix1
    ref, _ = recommend(client)
    claim = service.claim()
    held, attempted, release = threading.Event(), threading.Event(), threading.Event()
    original = db.write
    enqueue_role = ContextVar("enqueue_role", default=False)

    @contextmanager
    def gated_write():
        is_enqueue = enqueue_role.get()
        if not is_enqueue:
            attempted.set()
        if is_enqueue and not enqueue_first:
            held.set()
            assert release.wait(5), "release enqueue before write lock"
        with original() as s:
            yield s
            if is_enqueue and enqueue_first:
                s.flush()
                op = s.get(AgentOperation, ref)
                assert op.recommendation_id
                held.set()
                assert release.wait(5), "release enqueue before binding commit"

    def enqueue():
        enqueue_role.set(True)
        asyncio.run(service.advance(*claim))

    monkeypatch.setattr(db, "write", gated_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(enqueue)
        try:
            assert held.wait(5)
            second = pool.submit(cancel, client, ref)
            assert attempted.wait(5)
            if not enqueue_first:
                assert second.result(timeout=5).status_code == 200
        finally:
            release.set()
        first.result(timeout=5)
        assert second.result(timeout=5).status_code == 200
    with db.sessions() as s:
        op = s.get(AgentOperation, ref)
        assert op.state == "cancelled"
        assert count(s, Recommendation) == int(enqueue_first)
        receipts = s.scalar(
            select(func.count())
            .select_from(Idempotency)
            .where(Idempotency.action == "recommend")
        )
        assert receipts == int(enqueue_first)
        if enqueue_first:
            assert s.get(Recommendation, op.recommendation_id).state == "cancelled"
        else:
            assert op.recommendation_id is None


def test_read_paths_allow_same_client_read_scope(fix1):
    client, _, _, _ = fix1
    ref, key = recommend(client)
    assert (
        client.get(
            f"/v1/agent/operations/{ref}", headers=headers(READ_SAME)
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/v1/agent/operations/by-key",
            headers=headers(READ_SAME),
            params={"kind": "recommendation", "key": key},
        ).status_code
        == 200
    )


def test_cancel_write_scope_does_not_require_read_or_body(fix1):
    client, service, db, _ = fix1
    ref, _ = recommend(client)
    token = "synthetic-recommend-only-token-" * 3
    grant_client(
        db,
        service.settings,
        owner_id=OWNER_A,
        client_id="agent-test",
        scopes=["wardrobe.recommend"],
        token=token,
    )
    response = client.post(f"/v1/agent/operations/{ref}/cancel", headers=headers(token))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"


def test_existing_recommendation_api_mcp_share_submit_receipt(fix1, monkeypatch):
    from fastapi.testclient import TestClient
    from test_foundation import rpc

    client, service, db, _ = fix1

    async def idle_worker():
        await asyncio.wait_for(asyncio.Event().wait(), timeout=5)

    monkeypatch.setattr(service.recommendations, "run", idle_worker)
    monkeypatch.setattr(service, "run", idle_worker)
    key = str(uuid4())
    with TestClient(client.app, base_url="http://localhost") as running:
        first = running.post(
            "/v1/recommendations", headers=headers(key=key), json=PAYLOAD
        )
        assert first.status_code == 202, first.text
        identifier = first.json()["data"]["recommendation_id"]
        replay = running.post(
            "/v1/recommendations", headers=headers(key=key), json=PAYLOAD
        )
        assert replay.json() == first.json()
        mcp = rpc(
            running,
            "tools/call",
            {
                "name": "wardrobe.recommend_outfits",
                "arguments": {"payload": PAYLOAD, "key": key},
            },
            token=TOKEN,
        )
        assert mcp.status_code == 200, mcp.text
        result = mcp.json()["result"]
        assert not result["isError"], result
        assert result["structuredContent"] == first.json()
        assert (
            running.get(
                f"/v1/recommendations/{identifier}", headers=headers(READ_SAME)
            ).json()["data"]["state"]
            == "queued"
        )
    with db.sessions() as s:
        assert count(s, Recommendation) == 1
        assert count(s, Idempotency) == 1
