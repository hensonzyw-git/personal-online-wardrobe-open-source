"""W-AUTH-1..4: only the dedicated human operator can use this boundary."""

import pytest
from operator_support import OWNER, READ_TOKEN, TOKEN, T, headers, snapshot, uid
from operator_support import (
    operator_env as operator_env,  # noqa: PLC0414 - pytest fixture re-export
)
from sqlalchemy import func, select

from wardrobe.auth import AGENT_SCOPES, HUMAN_SCOPES, token_hash
from wardrobe.cli import grant_client
from wardrobe.errors import BusinessError
from wardrobe.models import ClientGrant


@pytest.mark.parametrize("kind", ["missing", "invalid", "expired", "revoked"])
def test_w_auth_1_invalid_credentials(operator_env, kind):
    e = operator_env
    h = headers()
    if kind == "missing":
        h = {}
    elif kind == "invalid":
        h = headers("synthetic-invalid-token-" + "x" * 40)
    else:
        with e.db.write() as s:
            g = s.get(ClientGrant, token_hash(TOKEN))
            if kind == "expired":
                g.created_at, g.expires_at = T - 100, T - 1
            else:
                g.revoked_at = T
    before = snapshot(e.db)
    response = e.client.patch(
        f"/v1/operator/garments/{uid(10)}",
        headers=h,
        json={"base_revision": 1, "patch": {"name": "denied"}},
    )
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert snapshot(e.db) == before


@pytest.mark.parametrize(
    "client,channel,scopes",
    [
        ("untrusted-agent", "agent", sorted(AGENT_SCOPES)),
        (
            "wardrobe-admin",
            "agent",
            ["wardrobe.operator_write", "wardrobe.read"],
        ),
        ("wrong-client", "human_adapter", ["wardrobe.operator_write", "wardrobe.read"]),
        ("wardrobe-admin", "human_adapter", ["wardrobe.read"]),
        ("wardrobe-admin", "human_adapter", ["human.decision"]),
        ("wardrobe-admin", "human_adapter", ["human.intake_create"]),
        (
            "wardrobe-admin",
            "human_adapter",
            ["wardrobe.operator_write", "wardrobe.read", "human.decision"],
        ),
    ],
)
def test_w_auth_1_identity_requires_all_constraints(
    operator_env, client, channel, scopes
):
    e = operator_env
    with e.db.write() as s:
        g = s.get(ClientGrant, token_hash(TOKEN))
        g.client_id, g.channel, g.scopes = client, channel, scopes
    before = snapshot(e.db)
    for method, path, body in [
        ("get", "/v1/operator/garments", None),
        ("get", f"/v1/operator/garments/{uid(10)}", None),
        (
            "patch",
            f"/v1/operator/garments/{uid(10)}",
            {"base_revision": 1, "patch": {"name": "denied"}},
        ),
        ("post", f"/v1/operator/garments/{uid(10)}/delete", {"base_revision": 1}),
    ]:
        kwargs = {"headers": headers()}
        if body is not None:
            kwargs["json"] = body
        assert e.client.request(method, path, **kwargs).status_code == 403
    assert snapshot(e.db) == before


def test_w_auth_2_signing_constraints(operator_env):
    from wardrobe.cli import grant_operator

    e = operator_env
    assert "wardrobe.operator_write" not in AGENT_SCOPES | HUMAN_SCOPES
    with e.db.sessions.begin() as s:
        signed = s.get(ClientGrant, token_hash(TOKEN))
        assert signed.client_id == "wardrobe-admin"
        assert signed.channel == "human_adapter"
        assert signed.scopes == ["wardrobe.operator_write", "wardrobe.read"]
        assert signed.expires_at - signed.created_at == 86400
        count = s.scalar(select(func.count()).select_from(ClientGrant))
    for kwargs in [
        {"owner_id": uid(999)},
        {"ttl_seconds": 0},
        {"ttl_seconds": 604801},
        {"token": "short"},
    ]:
        args = {
            "owner_id": OWNER,
            "token": "synthetic-rejected-grant-" + "q" * 40,
            **kwargs,
        }
        with pytest.raises((ValueError, BusinessError)):
            grant_operator(e.db, e.settings, **args)
    for channel, scopes, client in [
        ("agent", ["wardrobe.operator_write"], "wardrobe-admin"),
        ("human_adapter", ["wardrobe.operator_write", "wardrobe.read"], "wrong"),
        (
            "human_adapter",
            ["wardrobe.operator_write", "wardrobe.read", "human.decision"],
            "wardrobe-admin",
        ),
    ]:
        with pytest.raises(ValueError):
            grant_client(
                e.db,
                e.settings,
                owner_id=OWNER,
                client_id=client,
                scopes=scopes,
                channel=channel,
                token="synthetic-generic-reject-" + "z" * 40,
            )
    with e.db.sessions.begin() as s:
        assert s.scalar(select(func.count()).select_from(ClientGrant)) == count


@pytest.mark.parametrize("kind", ["revoked", "expired", "scope", "client"])
def test_w_auth_3_recheck_inside_write_before_idempotency(operator_env, kind):
    from wardrobe.operator_contracts import OperatorUpdate

    e = operator_env
    p = e.principal
    payload = OperatorUpdate(base_revision=1, patch={"name": "committed"})
    saved = e.app.state.operator.update(p, uid(10), payload, "synthetic-before-revoke")
    assert saved["data"]["revision"] == 2
    with e.db.write() as s:
        g = s.get(ClientGrant, token_hash(TOKEN))
        if kind == "revoked":
            g.revoked_at = T
        elif kind == "expired":
            g.created_at, g.expires_at = T - 100, T - 1
        elif kind == "scope":
            g.scopes = ["wardrobe.read"]
        else:
            g.client_id = "changed-client"
    before = snapshot(e.db)
    for key in ("synthetic-before-revoke", "synthetic-new-action"):
        with pytest.raises(BusinessError) as caught:
            e.app.state.operator.update(p, uid(10), payload, key)
        assert caught.value.http_status in (401, 403)
    assert snapshot(e.db) == before


def test_w_auth_4_host_origin_and_mcp_isolation(operator_env):
    from test_foundation import rpc

    e = operator_env
    for extra, status in [
        ({"Host": "wrong.invalid"}, 421),
        ({"Origin": "https://wrong.invalid"}, 403),
        ({"Origin": "https://localhost"}, 403),
        ({"Origin": "https://example.com"}, 403),
    ]:
        r = e.client.get("/v1/operator/garments", headers={**headers(), **extra})
        assert r.status_code == status
        assert r.headers["cache-control"] == "no-store"
    assert (
        e.client.get(
            "/v1/operator/garments",
            headers=[*headers().items(), ("Host", "localhost"), ("Host", "localhost")],
        ).status_code
        == 421
    )
    assert e.client.get("/v1/operator/garments", headers=headers()).status_code == 200
    assert rpc(e.client, "tools/list", token=TOKEN).status_code == 403
    tools = rpc(e.client, "tools/list", token=READ_TOKEN).json()["result"]["tools"]
    assert not any("operator" in tool["name"] for tool in tools)


def test_w_auth_4_unhandled_error_is_sanitized_and_no_store(operator_env, monkeypatch):
    from fastapi.testclient import TestClient

    e = operator_env

    def failed(*_args, **_kwargs):
        raise RuntimeError("synthetic-private-path-and-credential-marker")

    monkeypatch.setattr(e.app.state.operator, "list_garments", failed)
    client = TestClient(
        e.app, base_url="http://localhost", raise_server_exceptions=False
    )
    try:
        response = client.get("/v1/operator/garments", headers=headers())
        assert response.status_code == 500
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["status"] == "failed"
        assert "synthetic-private-path-and-credential-marker" not in response.text
    finally:
        client.close()
