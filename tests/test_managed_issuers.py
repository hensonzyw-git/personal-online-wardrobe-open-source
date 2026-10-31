import hashlib
import json
import os
import sqlite3
import time

import pytest
from conftest import OWNER_A
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from wardrobe import managed_grants as grants
from wardrobe import managed_issuers as issuers
from wardrobe.cli import grant_client, register_issuer


@pytest.fixture
def registered(store, tmp_path, monkeypatch):
    db, settings, _ = store
    monkeypatch.setattr(grants, "POLICY_UID", os.geteuid())
    key = Ed25519PrivateKey.generate()
    register_issuer(
        db,
        settings,
        issuer="synthetic",
        kid="intake",
        public_key=key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode(),
        subject="subject",
        owner_id=OWNER_A,
        client_id="personal-agent",
        ttl_seconds=86400,
        intake_only=True,
    )
    parent = tmp_path / "policies"
    parent.mkdir(mode=0o700)
    policy = parent / "issuer.json"
    now = int(time.time())
    issuers.enroll(db.path, policy, issuer="synthetic", kid="intake", now=now)
    return db, settings, policy, now


def row(db):
    with sqlite3.connect(db.path) as c:
        c.row_factory = sqlite3.Row
        return dict(
            c.execute(
                "SELECT * FROM trusted_issuers WHERE issuer='synthetic'"
            ).fetchone()
        )


def test_issuer_renewal_keeps_exact_identity_and_continues_across_seven_days(
    registered,
):
    db, _, policy, now = registered
    before = row(db)
    assert issuers.renew(policy, now=now, check_only=True)["status"] == "due"
    for day in (0, 4, 8, 12):
        assert issuers.renew(policy, now=now + day * 86400)["status"] == "renewed"
        after = row(db)
        assert after["expires_at"] == now + day * 86400 + grants.WINDOW
        assert {k: v for k, v in after.items() if k != "expires_at"} == {
            k: v for k, v in before.items() if k != "expires_at"
        }
    assert issuers.renew(policy, now=now + 12 * 86400)["status"] == "unchanged"
    with sqlite3.connect(db.path) as c:
        assert (
            c.execute(
                "SELECT count(*) FROM audit_events WHERE action='intake_issuer_renewed'"
            ).fetchone()[0]
            == 4
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("revoked_at", 1),
        ("expires_at", 1),
        ("public_key", "changed"),
        ("audience", "wrong"),
        ("purposes", '["decision"]'),
        ("bindings", "[]"),
    ],
)
def test_issuer_change_expiry_or_revocation_never_revives(registered, field, value):
    db, _, policy, now = registered
    with sqlite3.connect(db.path) as c:
        c.execute(
            f"UPDATE trusted_issuers SET {field}=? WHERE issuer='synthetic'", (value,)
        )
    with pytest.raises(grants.RenewalError):
        issuers.renew(policy, now=now)


def test_issuer_policy_and_instance_rollback_fail_closed(registered):
    db, _, policy, now = registered
    issuers.renew(policy, now=now)
    with sqlite3.connect(db.path) as c:
        c.execute(
            "UPDATE trusted_issuers SET expires_at=expires_at-1 WHERE issuer='synthetic'"
        )
    with pytest.raises(grants.RenewalError, match="continuity_lost"):
        issuers.renew(policy, now=now)
    with pytest.raises(grants.RenewalError, match="database_instance_changed"):
        issuers.renew(
            policy, now=now, expected_database=db.path.with_name("different.sqlite")
        )


def test_only_intake_human_grant_can_enroll(registered, tmp_path):
    db, settings, policy, now = registered
    for index, scopes in enumerate((["human.intake_create"], ["human.decision"])):
        token = "synthetic-human-token-" + str(index) * 40
        grant_client(
            db,
            settings,
            owner_id=OWNER_A,
            client_id="personal-agent",
            scopes=scopes,
            token=token,
            channel="human_adapter",
        )
        token_path = tmp_path / f"token{index}.json"
        token_path.write_text(json.dumps({"token": token}))
        token_path.chmod(0o600)
        target = policy.with_name(f"human{index}.json")
        if index:
            with pytest.raises(grants.RenewalError, match="binding_changed"):
                grants.enroll(
                    db.path,
                    token_path,
                    target,
                    owner_id=OWNER_A,
                    client_id="personal-agent",
                    now=now,
                )
        else:
            grants.enroll(
                db.path,
                token_path,
                target,
                owner_id=OWNER_A,
                client_id="personal-agent",
                now=now,
            )
            assert grants.renew(target, now=now, expected_database=db.path)[
                "status"
            ] in ("renewed", "unchanged")
            assert hashlib.sha256(token.encode()).hexdigest() in target.read_text()
