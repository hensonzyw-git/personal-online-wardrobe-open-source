import json

import httpx
import pytest
from conftest import TOKEN_A
from sqlalchemy import func, select
from test_catalog_writes import enable_write, png

from deploy.migrate_originals import migrate
from wardrobe.manifest import freeze_manifest
from wardrobe.models import Asset, Garment, Upload


@pytest.fixture
def transfer_service(live_service, monkeypatch):
    """Route the CLI's HTTP client to the same in-process ASGI service."""
    client, db = live_service
    original_client = httpx.Client

    def send(request):
        response = client.request(
            request.method,
            request.url.raw_path.decode(),
            headers=dict(request.headers),
            content=request.read(),
        )
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )

    def local_client(*args, **kwargs):
        return original_client(*args, **kwargs, transport=httpx.MockTransport(send))

    monkeypatch.setattr("deploy.migrate_originals.httpx.Client", local_client)
    return client, db


def prepare(tmp_path, names):
    source = tmp_path / "source"
    source.mkdir()
    for name in names:
        (source / name).write_bytes(png())
    manifest = tmp_path / "manifest.json"
    freeze_manifest(source, manifest)
    token = tmp_path / "client.token"
    token.write_text(TOKEN_A)
    token.chmod(0o600)
    return source, manifest, tmp_path / "receipt.json", token


def test_transfer_preserves_source_mappings_and_resumes_without_garments(
    transfer_service, tmp_path
):
    client, db = transfer_service
    enable_write(db)
    source, manifest, receipt, token = prepare(tmp_path, ["one.png", "two.png"])
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.iterdir()}
    with db.sessions.begin() as session:
        garments_before = session.scalar(select(func.count()).select_from(Garment))
    args = (source, manifest, receipt, str(client.base_url), token)
    assert migrate(*args) == {"source_files": 2, "ready": 2, "unresolved": 0}
    first = json.loads(receipt.read_text())
    assert len(first["records"]) == 2
    assert len({r["asset_id"] for r in first["records"].values()}) == 1
    assert migrate(*args) == first["summary"]
    assert json.loads(receipt.read_text())["records"] == first["records"]
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 1
        assert session.scalar(select(func.count()).select_from(Upload)) == 2
        assert (
            session.scalar(select(func.count()).select_from(Garment)) == garments_before
        )
    assert receipt.stat().st_mode & 0o077 == 0
    assert first["published"] is False and first["garments_created"] == 0
    assert {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.iterdir()
    } == before


def test_changed_source_never_uploads_old_manifest(transfer_service, tmp_path):
    client, db = transfer_service
    enable_write(db)
    source, manifest, receipt, token = prepare(tmp_path, ["one.png"])
    (source / "one.png").write_bytes(png("blue"))
    assert migrate(source, manifest, receipt, str(client.base_url), token) == {
        "source_files": 1,
        "ready": 0,
        "unresolved": 1,
    }
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(Upload)) == 0


def test_transfer_rejects_public_http_and_nonprivate_credentials(tmp_path):
    source, manifest, receipt, token = prepare(tmp_path, ["one.png"])
    with pytest.raises(ValueError, match="HTTPS_OR_LOOPBACK_REQUIRED"):
        migrate(source, manifest, receipt, "http://example.invalid", token)
    token.chmod(0o644)
    with pytest.raises(ValueError, match="TOKEN_FILE_MUST_BE_PRIVATE"):
        migrate(source, manifest, receipt, "http://127.0.0.1:1", token)
    assert not receipt.exists()
