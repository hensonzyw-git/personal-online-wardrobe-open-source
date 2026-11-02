import argparse
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from sqlalchemy.exc import SQLAlchemyError

from .auth import (
    AGENT_SCOPES,
    HUMAN_SCOPES,
    OPERATOR_CLIENT_ID,
    OPERATOR_SCOPES,
    token_hash,
)
from .catalog_import import import_catalog, load_batch
from .config import Settings
from .db import Database, load_cursor_key, load_private_key
from .errors import BusinessError
from .manifest import freeze_manifest, private_json, reconcile
from .models import ClientGrant, Owner, ServiceState, TrustedIssuer


def register_issuer(
    database,
    settings,
    *,
    issuer,
    kid,
    public_key,
    subject,
    owner_id,
    client_id,
    ttl_seconds,
    intake_only=False,
):
    owner_id = str(UUID(owner_id))
    key = load_pem_public_key(public_key.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError("Human issuer requires an Ed25519 public key")
    if (
        any(not value or len(value) > 100 for value in (issuer, kid, client_id))
        or not subject
        or len(subject) > 200
        or not 1 <= ttl_seconds <= 604800
    ):
        raise ValueError("Invalid issuer binding or lifetime")
    with database.write() as session:
        if (
            session.get(Owner, owner_id) is None
            or session.get(TrustedIssuer, (issuer, kid)) is not None
        ):
            raise ValueError("Owner must exist and issuer kid must be new")
        session.add(
            TrustedIssuer(
                issuer=issuer,
                kid=kid,
                public_key=public_key,
                bindings=[
                    {"sub": subject, "owner_id": owner_id, "client_id": client_id}
                ],
                purposes=["intake_create"]
                if intake_only
                else ["decision", "review_preview"],
                audience="wardrobe:intake-create"
                if intake_only
                else settings.audience + ":human-decisions",
                expires_at=int(time.time()) + ttl_seconds,
                revoked_at=None,
            )
        )


def grant_client(
    database: Database,
    settings: Settings,
    *,
    owner_id: str,
    client_id: str,
    scopes: list[str],
    token: str,
    ttl_seconds: int = 604800,
    channel: str = "agent",
) -> None:
    owner_id = str(UUID(owner_id))
    allowed = (
        AGENT_SCOPES
        if channel == "agent"
        else HUMAN_SCOPES
        if channel == "human_adapter"
        else frozenset()
    )
    if (
        not client_id
        or len(client_id) > 100
        or not scopes
        or not set(scopes) <= allowed
    ):
        raise ValueError("Invalid client or scopes")
    if not 1 <= ttl_seconds <= 604800 or not 32 <= len(token) <= 512:
        raise ValueError("Token length or lifetime is invalid")
    now = int(time.time())
    with database.write() as session:
        if session.get(Owner, owner_id) is None:
            session.add(Owner(id=owner_id))
            session.flush()
            session.add(
                ServiceState(owner_id=owner_id, catalog_revision=0, history_revision=0)
            )
            session.flush()
        session.add(
            ClientGrant(
                token_hash=token_hash(token),
                owner_id=owner_id,
                client_id=client_id,
                scopes=sorted(set(scopes)),
                audience=settings.audience,
                channel=channel,
                created_at=now,
                expires_at=now + ttl_seconds,
                revoked_at=None,
            )
        )


def grant_operator(
    database: Database,
    settings: Settings,
    *,
    owner_id: str,
    token: str,
    ttl_seconds: int = 86400,
    client_id: str = OPERATOR_CLIENT_ID,
    channel: str = "human_adapter",
    scopes: list[str] | None = None,
) -> None:
    """Issue the private UI grant only; unlike grant-client, never create an owner."""
    owner_id = str(UUID(owner_id))
    scopes = sorted(OPERATOR_SCOPES) if scopes is None else scopes
    if (
        client_id != OPERATOR_CLIENT_ID
        or channel != "human_adapter"
        or not isinstance(scopes, list)
        or len(scopes) != len(OPERATOR_SCOPES)
        or any(not isinstance(scope, str) for scope in scopes)
        or frozenset(scopes) != OPERATOR_SCOPES
    ):
        raise ValueError("Invalid operator identity or scopes")
    if (
        type(ttl_seconds) is not int
        or not 1 <= ttl_seconds <= 604800
        or not isinstance(token, str)
        or not 32 <= len(token) <= 512
    ):
        raise ValueError("Token length or lifetime is invalid")
    now = int(time.time())
    with database.write() as session:
        if session.get(Owner, owner_id) is None:
            raise ValueError("Operator owner must already exist")
        session.add(
            ClientGrant(
                token_hash=token_hash(token),
                owner_id=owner_id,
                client_id=OPERATOR_CLIENT_ID,
                scopes=sorted(OPERATOR_SCOPES),
                audience=settings.audience,
                channel="human_adapter",
                created_at=now,
                expires_at=now + ttl_seconds,
                revoked_at=None,
            )
        )


def main():
    parser = argparse.ArgumentParser(prog="wardrobectl")
    parser.add_argument("--data-root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db")
    preview = commands.add_parser("warm-previews")
    preview.add_argument("--owner-id", required=True)
    season = commands.add_parser("init-season")
    season.add_argument("--owner-id", required=True)
    season.add_argument(
        "--season", choices=["spring_summer", "autumn_winter"], required=True
    )
    enroll_parser = commands.add_parser("enroll-renewal")
    enroll_parser.add_argument("--owner-id", required=True)
    enroll_parser.add_argument("--client-id", required=True)
    enroll_parser.add_argument("--token-json", type=Path, required=True)
    enroll_parser.add_argument("--policy-file", type=Path, required=True)
    renew_parser = commands.add_parser("renew-credentials")
    renew_parser.add_argument("--policy-file", type=Path, required=True)
    renew_parser.add_argument("--force-active", action="store_true")
    renew_parser.add_argument("--check-only", action="store_true")
    ledger = commands.add_parser("init-model-ledger")
    ledger.add_argument("--call-limit", type=int, required=True)
    catalog_import = commands.add_parser("import-catalog")
    catalog_import.add_argument("--bundle", type=Path, required=True)
    catalog_import.add_argument("--owner-id", required=True)
    catalog_import.add_argument("--expected-digest", required=True)
    catalog_import.add_argument("--apply", action="store_true")
    grant = commands.add_parser("grant-client")
    grant.add_argument("--owner-id", required=True)
    grant.add_argument("--client-id", required=True)
    grant.add_argument("--scope", action="append", required=True)
    grant.add_argument("--token-file", type=Path, required=True)
    grant.add_argument("--ttl-seconds", type=int, default=604800)
    grant.add_argument("--channel", choices=["agent", "human_adapter"], default="agent")
    operator_grant = commands.add_parser("grant-operator")
    operator_grant.add_argument("--owner-id", required=True)
    operator_grant.add_argument("--token-file", type=Path, required=True)
    operator_grant.add_argument("--ttl-seconds", type=int, default=86400)
    revoke = commands.add_parser("revoke-client")
    revoke.add_argument("--token-file", type=Path, required=True)
    issuer = commands.add_parser("register-issuer")
    issuer.add_argument("--issuer", required=True)
    issuer.add_argument("--kid", required=True)
    issuer.add_argument("--public-key-file", type=Path, required=True)
    issuer.add_argument("--subject", required=True)
    issuer.add_argument("--owner-id", required=True)
    issuer.add_argument("--client-id", required=True)
    issuer.add_argument("--ttl-seconds", type=int, default=604800)
    issuer.add_argument("--intake-only", action="store_true")
    commands.add_parser("collect-intakes")
    backup_manifest = commands.add_parser("intake-backup-manifest")
    backup_manifest.add_argument("--output", type=Path, required=True)
    verify_backup = commands.add_parser("verify-intake-backup")
    verify_backup.add_argument("--manifest", type=Path, required=True)
    verify_backup.add_argument("--output", type=Path, required=True)
    enroll_intake = commands.add_parser("enroll-intake-issuer-renewal")
    enroll_intake.add_argument("--policy-file", type=Path, required=True)
    enroll_intake.add_argument("--issuer", required=True)
    enroll_intake.add_argument("--kid", required=True)
    renew_intake = commands.add_parser("renew-intake-issuer")
    renew_intake.add_argument("--policy-file", type=Path, required=True)
    renew_intake.add_argument("--check-only", action="store_true")
    revoke_issuer = commands.add_parser("revoke-issuer")
    revoke_issuer.add_argument("--issuer", required=True)
    revoke_issuer.add_argument("--kid", required=True)
    manifest = commands.add_parser("freeze-manifest")
    manifest.add_argument("--source", type=Path, required=True)
    manifest.add_argument("--output", type=Path, required=True)
    inventory = commands.add_parser("reconcile")
    inventory.add_argument("--output", type=Path, required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.data_root is not None:
        settings = settings.model_copy(update={"data_root": args.data_root})
    database = Database(settings.data_root / "wardrobe.sqlite")
    try:
        if args.command in {"intake-backup-manifest", "verify-intake-backup"}:
            from .intake_backup import build, verify

            if args.command == "intake-backup-manifest":
                database.assert_initialized()
                report = build(database, settings.data_root)
            else:
                report = verify(settings.data_root, json.loads(args.manifest.read_text()))
            private_json(args.output, report)
            print("Private intake backup report saved; no source files changed.")
            if report.get("ok") is False:
                raise SystemExit(2)
        elif args.command == "collect-intakes":
            from .intakes import IntakeService

            database.assert_initialized()
            print(
                json.dumps(
                    IntakeService(
                        database,
                        settings.data_root,
                        load_private_key(settings.data_root, "upload.key"),
                    ).collect()
                )
            )
        elif args.command in {"enroll-intake-issuer-renewal", "renew-intake-issuer"}:
            from .managed_issuers import enroll, renew

            database.assert_initialized()
            result = (
                enroll(
                    database.path, args.policy_file, issuer=args.issuer, kid=args.kid
                )
                if args.command == "enroll-intake-issuer-renewal"
                else renew(args.policy_file, check_only=args.check_only)
            )
            print(json.dumps(result))
        elif args.command == "warm-previews":
            from .image_previews import warm_previews

            database.assert_initialized()
            report = warm_previews(database, settings.data_root, args.owner_id)
            print(json.dumps(report))
            if report["failed"] or report["catalog_changed"]:
                raise SystemExit(2)
        elif args.command == "freeze-manifest":
            summary = freeze_manifest(args.source, args.output)
            print(
                f"Read-only manifest saved: {summary['readable']} readable; {summary['unresolved']} unresolved. Nothing uploaded or published."
            )
        elif args.command == "reconcile":
            database.assert_initialized()
            report = reconcile(database, settings.data_root)
            private_json(args.output, report)
            print("Read-only integrity report saved. No files or records changed.")
        elif args.command == "init-model-ledger":
            from .outfit_provider import ModelCallQuota

            ModelCallQuota(
                settings.data_root / "model-calls.sqlite", args.call_limit, create=True
            )
            print(
                "New model call ledger initialized; existing ledgers are never overwritten."
            )
        elif args.command in {"enroll-renewal", "renew-credentials"}:
            from .managed_grants import RenewalError, enroll, renew

            try:
                if args.command == "enroll-renewal":
                    result = enroll(
                        database.path,
                        args.token_json,
                        args.policy_file,
                        owner_id=args.owner_id,
                        client_id=args.client_id,
                    )
                else:
                    result = renew(
                        args.policy_file,
                        expected_database=database.path,
                        force_active=args.force_active,
                        check_only=args.check_only,
                    )
                print(json.dumps(result))
            except RenewalError as error:
                print(json.dumps({"status": "refused", "reason": error.code}))
                raise SystemExit(2) from None
        elif args.command == "init-db":
            database.migrate()
            load_cursor_key(settings.data_root, create=True)
            load_private_key(settings.data_root, "upload.key", create=True)
            print("Database initialized; no wardrobe data imported.")
        elif args.command == "init-season":
            from .seasons import initialize

            database.assert_initialized()
            initialize(database, args.owner_id, args.season)
            print("Confirmed storage season initialized; no garment labels changed.")
        elif args.command == "import-catalog":
            database.assert_initialized()
            batch = load_batch(args.bundle, args.expected_digest, args.owner_id)
            print(
                json.dumps(
                    import_catalog(
                        database, settings.data_root, batch, apply=args.apply
                    )
                )
            )
        elif args.command in {"grant-client", "grant-operator"}:
            database.assert_initialized()
            # Never print credentials. Only create a new private token file.
            descriptor = os.open(
                args.token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            token = secrets.token_urlsafe(48)
            try:
                with os.fdopen(descriptor, "w") as file:
                    file.write(token)
                    file.flush()
                    os.fsync(file.fileno())
                if args.command == "grant-operator":
                    grant_operator(
                        database,
                        settings,
                        owner_id=args.owner_id,
                        token=token,
                        ttl_seconds=args.ttl_seconds,
                    )
                else:
                    grant_client(
                        database,
                        settings,
                        owner_id=args.owner_id,
                        client_id=args.client_id,
                        scopes=args.scope,
                        token=token,
                        ttl_seconds=args.ttl_seconds,
                        channel=args.channel,
                    )
            except Exception:
                args.token_file.unlink()
                raise
            print("Client registered; credential saved to the specified private file.")
        elif args.command == "revoke-client":
            database.assert_initialized()
            with database.write() as session:
                row = session.get(
                    ClientGrant, token_hash(args.token_file.read_text().strip())
                )
                if row is None:
                    raise ValueError("Client grant not found")
                row.revoked_at = int(time.time())
            print("Client grant revoked.")
        elif args.command == "register-issuer":
            database.assert_initialized()
            register_issuer(
                database,
                settings,
                issuer=args.issuer,
                kid=args.kid,
                public_key=args.public_key_file.read_text(),
                subject=args.subject,
                owner_id=args.owner_id,
                client_id=args.client_id,
                ttl_seconds=args.ttl_seconds,
                intake_only=args.intake_only,
            )
            print(
                "Public issuer registered. Signing private key remains with the trusted human adapter."
            )
        elif args.command == "revoke-issuer":
            database.assert_initialized()
            with database.write() as session:
                row = session.get(TrustedIssuer, (args.issuer, args.kid))
                if row is None:
                    raise ValueError("Issuer not found")
                row.revoked_at = int(time.time())
            print("Issuer key revoked.")
        else:
            if os.environ.get("WEB_CONCURRENCY", "1") != "1":
                raise ValueError("This service requires exactly one ASGI worker")
            import uvicorn

            from .app import create_app

            uvicorn.run(
                create_app(settings, database=database),
                host="127.0.0.1",
                port=args.port,
                workers=1,
                access_log=False,
            )
    except (
        BusinessError,
        ValueError,
        TypeError,
        RuntimeError,
        OSError,
        sqlite3.Error,
        SQLAlchemyError,
    ) as error:
        # Configuration errors expose no credential values or private DB contents.
        parser.exit(
            2,
            f"Operation failed ({error.code if isinstance(error, BusinessError) else type(error).__name__}); check initialization and configuration.\n",
        )
    finally:
        database.close()
