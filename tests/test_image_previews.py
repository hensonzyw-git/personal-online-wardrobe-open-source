"""Synthetic/offline tests. In-process child fixture does not prove Linux limits."""

import copy
import fcntl
import hashlib
import io
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from conftest import OWNER_A, TOKEN_B, TOKEN_NO_READ
from PIL import Image, ImageCms
from test_catalog_writes import create, enable_write, upload
from test_foundation import headers, rpc

from wardrobe.errors import BusinessError
from wardrobe.image_previews import (
    MAX_BYTES,
    RECIPE,
    PreviewStore,
    present_images,
    validate_preview,
    warm_previews,
)
from wardrobe.models import ClientGrant, Garment, ServiceState
from wardrobe.preview_worker import transform


def raw_image(fmt="PNG", size=(1200, 800), mode="RGB", **kwargs):
    raw = io.BytesIO()
    Image.new(mode, size, (0, 80, 150, 0) if mode == "RGBA" else "blue").save(
        raw, fmt, **kwargs
    )
    return raw.getvalue()


def original(tmp_path, raw=None):
    raw = raw or raw_image()
    sha = hashlib.sha256(raw).hexdigest()
    path = tmp_path / "assets" / "original" / sha
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return sha, path


@pytest.fixture
def simulated_child(monkeypatch):
    def run(arguments, **kwargs):
        assert kwargs["timeout"] == 8
        assert arguments[1:3] == ["-m", "wardrobe.preview_worker"]
        transform(Path(arguments[3]), arguments[4], Path(arguments[5]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("wardrobe.image_previews.subprocess.run", run)


@pytest.mark.parametrize("fmt,mode", [("PNG", "RGB"), ("JPEG", "RGB"), ("PNG", "RGBA")])
def test_private_cached_preview_preserves_original(
    tmp_path, simulated_child, monkeypatch, fmt, mode
):
    sha, path = original(tmp_path, raw_image(fmt, mode=mode))
    raw = path.read_bytes()
    result = PreviewStore(tmp_path).get(sha)
    assert validate_preview(result) and result.stat().st_size <= MAX_BYTES
    assert result.stat().st_mode & 0o777 == 0o600
    assert result.parent.stat().st_mode & 0o777 == 0o700
    with Image.open(result) as image:
        assert image.size == (768, 512)
        assert not image.getexif() and "icc_profile" not in image.info
        if mode == "RGBA":
            assert min(image.getpixel((0, 0))) >= 250
    monkeypatch.setattr(
        "wardrobe.image_previews.subprocess.run",
        lambda *a, **k: pytest.fail("cache regenerated"),
    )
    assert PreviewStore(tmp_path).get(sha) == result
    assert path.read_bytes() == raw
    path.unlink()
    with pytest.raises(BusinessError):
        PreviewStore(tmp_path).get(sha)


def test_orientation_icc_small_image_and_noise(tmp_path):
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    exif = Image.Exif()
    exif[274] = 6
    raw = raw_image("JPEG", (1200, 800), exif=exif, icc_profile=profile)
    sha, source = original(tmp_path, raw)
    target = tmp_path / "preview.jpg"
    transform(source, sha, target)
    with Image.open(target) as image:
        assert image.size == (512, 768)
    sha, source = original(tmp_path, raw_image(size=(8, 5)))
    transform(source, sha, target)
    with Image.open(target) as image:
        assert image.size == (8, 5)
    noisy = Image.effect_noise((1600, 1600), 100).convert("RGB")
    raw = io.BytesIO()
    noisy.save(raw, "PNG")
    sha, source = original(tmp_path, raw.getvalue())
    transform(source, sha, target)
    assert validate_preview(target)


@pytest.mark.parametrize("kind", ["checksum", "bad", "pixels", "icc"])
def test_worker_refuses_invalid_inputs(tmp_path, kind):
    raw = {"bad": b"not an image", "pixels": None}.get(
        kind, raw_image(icc_profile=b"bad") if kind == "icc" else raw_image()
    )
    if kind == "pixels":
        raw = raw_image(size=(4001, 3000))
    sha, source = original(tmp_path, raw)
    if kind == "checksum":
        sha = "f" * 64
    target = tmp_path / "target.jpg"
    with pytest.raises((ValueError, OSError, ImageCms.PyCMSError)):
        transform(source, sha, target)
    assert not target.exists()


@pytest.mark.parametrize("failure", ["timeout", "exit", "disk", "partial"])
def test_failed_child_never_publishes_and_cleans_temp(tmp_path, monkeypatch, failure):
    sha, source = original(tmp_path)

    def run(args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("synthetic", 8)
        if failure == "disk":
            raise OSError("synthetic disk full")
        Path(args[-1]).write_bytes(b"incomplete")
        return SimpleNamespace(returncode=1 if failure == "exit" else 0)

    monkeypatch.setattr("wardrobe.image_previews.subprocess.run", run)
    with pytest.raises(BusinessError, match="Preview is unavailable"):
        PreviewStore(tmp_path).get(sha)
    assert not list((tmp_path / "assets" / "derived").rglob("*.jpg"))
    assert not list((tmp_path / "assets" / "derived").rglob("*.tmp"))
    assert source.exists()


def test_global_lock_budget_symlink_and_recovery(
    tmp_path, simulated_child, monkeypatch
):
    sha, source = original(tmp_path)
    store = PreviewStore(tmp_path)
    store.directory.mkdir(mode=0o700)
    with (store.directory / ".generation.lock").open("wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BusinessError) as error:
            store.get(sha)
        assert error.value.code == "PREVIEW_BUSY"
    monkeypatch.setattr("wardrobe.image_previews.MAX_STORE_BYTES", 1)
    with pytest.raises(BusinessError):
        store.get(sha)
    monkeypatch.setattr(
        "wardrobe.image_previews.MAX_STORE_BYTES", 128 * 1024**2
    )
    temporary = store.directory / sha / ".preview-orphan.tmp"
    temporary.write_bytes(b"orphan")
    target = store.get(sha)
    assert not temporary.exists()
    target.write_bytes(b"bad cache")
    assert validate_preview(store.get(sha))
    target.unlink()
    target.symlink_to(source)
    with pytest.raises(BusinessError):
        store.get(sha)
    target.unlink()
    target.parent.rmdir()
    target.parent.symlink_to(source.parent)
    with pytest.raises(BusinessError):
        store.get(sha)


def test_presentation_copy_is_narrow_and_idempotent():
    asset = str(UUID(int=50))
    reference = {
        "asset_id": asset,
        "content_url": f"/v1/assets/{asset}/content",
        "mime": "image/png",
        "width": 1000,
    }
    value = {
        "result": {"outfits": [{"garment": {"image_references": [reference]}}]},
        "image_references": [
            {**reference, "content_url": f"/v1/product-drafts/x/assets/{asset}/content"}
        ],
    }
    before = copy.deepcopy(value)
    result = present_images(value)
    assert value == before
    preview = result["result"]["outfits"][0]["garment"]["image_references"][0][
        "preview"
    ]
    assert preview["max_bytes"] == MAX_BYTES and preview["recipe_version"] == RECIPE
    assert "preview" not in result["image_references"][0]
    assert present_images(result) == result


def test_http_preview_authorization_and_operator_read_only(
    live_service, simulated_child
):
    client, db = live_service
    enable_write(db)
    raw = raw_image()
    asset = upload(client, raw)
    garment = create(client, asset)["data"]["garment_id"]
    ref = client.get(f"/v1/garments/{garment}", headers=headers()).json()["data"][
        "garment"
    ]["image_references"][0]
    path = ref["preview"]["url"]
    mcp = rpc(
        client,
        "tools/call",
        {"name": "wardrobe.get_garment", "arguments": {"garment_id": garment}},
    ).json()
    assert (
        mcp["result"]["structuredContent"]["data"]["garment"]["image_references"][0]
        == ref
    )
    root = db.path.parent
    with db.engine.connect() as conn:
        before = "\n".join(conn.connection.driver_connection.iterdump())
    response = client.get(path, headers=headers())
    assert (
        response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    )
    assert (
        len(response.content) <= MAX_BYTES
        and response.headers["cache-control"] == "private, no-store"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers(TOKEN_B)).status_code == 404
    assert client.get(path, headers=headers(TOKEN_NO_READ)).status_code == 403
    assert client.get(ref["content_url"], headers=headers()).content == raw
    schema = client.get("/openapi.json", headers=headers()).json()
    assert set(
        schema["paths"]["/v1/assets/{asset_id}/preview"]["get"]["responses"]["200"][
            "content"
        ]
    ) == {"image/jpeg"}
    report = warm_previews(db, root, OWNER_A)
    assert report["assets"] == report["existing"] == 1 and report["failed"] == 0
    with db.engine.connect() as conn:
        assert "\n".join(conn.connection.driver_connection.iterdump()) == before
    with db.sessions.begin() as session:
        session.get(Garment, garment).state = "deleted"
    assert client.get(path, headers=headers()).status_code == 404


def test_revoke_during_generation_refuses_response(live_service, monkeypatch):
    client, db = live_service
    enable_write(db)
    asset = upload(client, raw_image())
    create(client, asset)

    def run(args, **kwargs):
        transform(Path(args[3]), args[4], Path(args[5]))
        with db.sessions.begin() as session:
            for grant in session.query(ClientGrant).all():
                grant.revoked_at = 1
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("wardrobe.image_previews.subprocess.run", run)
    assert (
        client.get(f"/v1/assets/{asset}/preview", headers=headers()).status_code == 401
    )


def test_prewarm_detects_catalog_change(live_service, simulated_child, monkeypatch):
    client, db = live_service
    enable_write(db)
    create(client, upload(client, raw_image()))
    get = PreviewStore.get

    def changing_get(store, sha):
        result = get(store, sha)
        with db.sessions.begin() as session:
            session.get(ServiceState, OWNER_A).catalog_revision += 1
        return result

    monkeypatch.setattr(PreviewStore, "get", changing_get)
    report = warm_previews(db, db.path.parent, OWNER_A)
    assert report["catalog_changed"] and report["generated"] == 1


@pytest.mark.skipif(
    sys.platform != "linux", reason="Production worker resource limits require Linux"
)
def test_real_linux_child_roundtrip(tmp_path):
    sha, source = original(tmp_path)
    target = PreviewStore(tmp_path).get(sha)
    assert validate_preview(target)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == sha
