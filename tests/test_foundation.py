import time
from uuid import UUID

import pytest
from conftest import OWNER_A, OWNER_B, TOKEN_A, TOKEN_B, TOKEN_NO_READ
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from wardrobe.auth import Authenticator, token_hash
from wardrobe.catalog import CatalogService
from wardrobe.contracts import ListQuery
from wardrobe.errors import BusinessError
from wardrobe.models import ClientGrant, Garment, ServiceState


def headers(token=TOKEN_A):
    return {"Authorization": "Bearer " + token}


def rpc(
    client, method, params=None, *, token=TOKEN_A, version="2026-07-28", extra=None
):
    params = dict(params or {})
    protocol_headers = {}
    if version == "2026-07-28":
        params["_meta"] = {
            **params.get("_meta", {}),
            "io.modelcontextprotocol/protocolVersion": version,
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        protocol_headers["Mcp-Method"] = method
        if "name" in params:
            protocol_headers["Mcp-Name"] = params["name"]
    return client.post(
        "/mcp",
        headers={
            **headers(token),
            "MCP-Protocol-Version": version,
            "Accept": "application/json, text/event-stream",
            **protocol_headers,
            **(extra or {}),
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


def test_migrations_repeat_and_constraints(store):
    db, _settings, _key = store
    db.migrate()
    db.assert_initialized()
    with db.engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
    with pytest.raises(IntegrityError), db.sessions.begin() as session:
        session.add(
            ServiceState(
                owner_id=str(UUID(int=999)), catalog_revision=0, history_revision=0
            )
        )
    with pytest.raises(IntegrityError), db.sessions.begin() as session:
        session.execute(
            update(Garment).where(Garment.id == str(UUID(int=10))).values(revision=0)
        )
    assert db.path.stat().st_mode & 0o077 == 0


def test_sqlite_read_snapshot_survives_concurrent_writer(store):
    db, _settings, _key = store
    with db.sessions.begin() as reader:
        assert reader.get(ServiceState, OWNER_A).catalog_revision == 0
        with db.sessions.begin() as writer:
            writer.execute(
                update(ServiceState)
                .where(ServiceState.owner_id == OWNER_A)
                .values(catalog_revision=1)
            )
            writer.execute(
                update(Garment)
                .where(Garment.id == str(UUID(int=10)))
                .values(name="Changed")
            )
        assert (
            reader.scalar(select(Garment.name).where(Garment.id == str(UUID(int=10))))
            == "Synthetic 10"
        )
    with db.sessions.begin() as reader:
        assert reader.get(ServiceState, OWNER_A).catalog_revision == 1


def test_cursor_scope_filter_tampering_and_stale(store):
    db, settings, key = store
    catalog = CatalogService(db.sessions, key)
    auth = Authenticator(db.sessions, settings.audience)
    a, b = auth.authenticate(TOKEN_A), auth.authenticate(TOKEN_B)
    page = catalog.list_garments(a, ListQuery(limit=2))["data"]
    cursor = page["next_cursor"]
    assert page["has_more"]
    for principal, query in [
        (b, ListQuery(limit=2, cursor=cursor)),
        (a, ListQuery(limit=3, cursor=cursor)),
        (a, ListQuery(limit=2, cursor=cursor, category="shoe")),
        (a, ListQuery(limit=2, cursor="x" + cursor[1:])),
    ]:
        with pytest.raises(BusinessError) as error:
            catalog.list_garments(principal, query)
        assert error.value.code == "INVALID_CURSOR"
    with db.sessions.begin() as session:
        session.execute(
            update(ServiceState)
            .where(ServiceState.owner_id == OWNER_A)
            .values(catalog_revision=1)
        )
    with pytest.raises(BusinessError) as error:
        catalog.list_garments(a, ListQuery(limit=2, cursor=cursor))
    assert error.value.code == "CURSOR_STALE"


@pytest.mark.parametrize("kind", ["expired", "revoked", "audience"])
def test_grant_rejection(store, kind):
    db, settings, _key = store
    with db.sessions.begin() as session:
        grant = session.get(ClientGrant, token_hash(TOKEN_A))
        if kind == "expired":
            grant.created_at, grant.expires_at = (
                int(time.time()) - 100,
                int(time.time()) - 1,
            )
        elif kind == "revoked":
            grant.revoked_at = int(time.time())
        else:
            grant.audience = "another-service"
    with pytest.raises(BusinessError) as error:
        Authenticator(db.sessions, settings.audience).authenticate(TOKEN_A)
    assert error.value.http_status == 401


def test_http_api_and_modern_mcp_complete_pagination(live_service):
    client, _db = live_service
    discovery = rpc(client, "tools/list")
    assert discovery.status_code == 200, discovery.text
    tools = {x["name"] for x in discovery.json()["result"]["tools"]}
    assert {"wardrobe.list_garments", "wardrobe.get_garment"} <= tools
    assert not any("confirm" in name or "decision" in name for name in tools)
    api_ids, mcp_ids = [], []
    cursor = None
    while True:
        query = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        api = client.get("/v1/garments", params=query, headers=headers()).json()
        mcp = rpc(
            client, "tools/call", {"name": "wardrobe.list_garments", "arguments": query}
        ).json()["result"]
        assert not mcp["isError"]
        assert api["data"] == mcp["structuredContent"]["data"]
        api_ids.extend(x["id"] for x in api["data"]["items"])
        mcp_ids.extend(x["id"] for x in mcp["structuredContent"]["data"]["items"])
        cursor = api["data"]["next_cursor"]
        if cursor is None:
            break
    assert api_ids == mcp_ids == [str(UUID(int=n)) for n in range(10, 16)]
    details = client.get(f"/v1/garments/{api_ids[0]}", headers=headers()).json()[
        "data"
    ]["garment"]
    assert details["annotation_fields"]["material"]["value"] is None
    assert not details["recommendation_eligible"]


def test_http_authorization_host_origin_scope_and_owner(live_service):
    client, _db = live_service
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/garments").status_code == 401
    assert client.post("/mcp", json={"method": "initialize"}).status_code == 401
    assert (
        client.get(
            "/v1/garments", headers={**headers(), "Host": "attacker.invalid"}
        ).status_code
        == 421
    )
    assert (
        client.get(
            "/v1/garments", headers={**headers(), "Origin": "https://attacker.invalid"}
        ).status_code
        == 403
    )
    assert client.get("/v1/garments", headers=headers(TOKEN_NO_READ)).status_code == 403
    assert (
        client.get(
            "/v1/garments", params={"owner_id": OWNER_B}, headers=headers()
        ).status_code
        == 422
    )
    assert (
        client.get("/v1/garments", params={"limit": 101}, headers=headers()).status_code
        == 422
    )
    assert (
        client.get(f"/v1/garments/{UUID(int=30)!s}", headers=headers()).status_code
        == 404
    )
    result = rpc(
        client,
        "tools/call",
        {
            "name": "wardrobe.list_garments",
            "arguments": {},
            "_meta": {"owner_id": OWNER_B},
        },
    ).json()["result"]["structuredContent"]
    assert len(result["data"]["items"]) == 6
    result = rpc(
        client,
        "tools/call",
        {"name": "wardrobe.list_garments", "arguments": {}},
        token=TOKEN_NO_READ,
    ).json()["result"]
    assert (
        result["isError"]
        and result["structuredContent"]["error"]["code"] == "SCOPE_DENIED"
    )


def test_legacy_mcp_call_then_revoke_same_session(live_service):
    client, db = live_service
    initialized = rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "synthetic-test", "version": "1"},
        },
        version="2025-11-25",
    )
    assert initialized.status_code == 200, initialized.text
    session_id = initialized.headers["mcp-session-id"]
    session_headers = {"Mcp-Session-Id": session_id}
    client.post(
        "/mcp",
        headers={
            **headers(),
            **session_headers,
            "MCP-Protocol-Version": "2025-11-25",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    result = rpc(
        client,
        "tools/call",
        {"name": "wardrobe.list_garments", "arguments": {}},
        version="2025-11-25",
        extra=session_headers,
    )
    assert result.status_code == 200 and not result.json()["result"]["isError"]
    with db.sessions.begin() as session:
        session.get(ClientGrant, token_hash(TOKEN_A)).revoked_at = int(time.time())
    assert (
        rpc(
            client, "tools/list", version="2025-11-25", extra=session_headers
        ).status_code
        == 401
    )


def test_rollback_no_half_catalog_changes(store):
    db, _settings, _key = store
    with pytest.raises(RuntimeError), db.sessions.begin() as session:
        session.execute(
            update(Garment)
            .where(Garment.id == str(UUID(int=10)))
            .values(name="Should roll back")
        )
        session.execute(
            update(ServiceState)
            .where(ServiceState.owner_id == OWNER_A)
            .values(catalog_revision=42)
        )
        raise RuntimeError("synthetic failure")
    with db.sessions.begin() as session:
        assert session.get(Garment, str(UUID(int=10))).name == "Synthetic 10"
        assert session.get(ServiceState, OWNER_A).catalog_revision == 0


def test_recheck_uses_operation_scope_without_preserving_revoked_permissions(store):
    from wardrobe.auth import recheck_grant

    db, settings, _key = store
    with db.write() as session:
        grant = session.get(ClientGrant, token_hash(TOKEN_A))
        grant.scopes = ["wardrobe.read", "wardrobe.recommend"]
    principal = Authenticator(db.sessions, settings.audience).authenticate(TOKEN_A)
    with db.write() as session:
        session.get(ClientGrant, token_hash(TOKEN_A)).scopes = ["wardrobe.read"]
    with db.sessions.begin() as session:
        recheck_grant(session, principal, required_scope="wardrobe.read")
        with pytest.raises(BusinessError, match="Authorization changed"):
            recheck_grant(session, principal, required_scope="wardrobe.recommend")
