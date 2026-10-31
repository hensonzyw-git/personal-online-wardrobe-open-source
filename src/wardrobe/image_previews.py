"""Private, reconstructible previews. Authorization belongs to AssetService."""

import contextlib
import copy
import functools
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID

from PIL import Image

from .errors import BusinessError
from .preview_worker import MAX_BYTES, RECIPE

MAX_STORE_BYTES = 128 * 1024**2
MAX_FILES = 4096


def preview_reference(asset_id):
    return {
        "url": f"/v1/assets/{asset_id}/preview",
        "mime": "image/jpeg",
        "max_edge": 768,
        "max_bytes": MAX_BYTES,
        "recipe_version": RECIPE,
    }


def present_images(value):
    """Decorate only ordinary image references, never stored snapshots or URLs."""
    result = copy.deepcopy(value)

    def visit(node):
        if isinstance(node, dict):
            references = node.get("image_references", [])
            for ref in references if isinstance(references, list) else []:
                if not isinstance(ref, dict):
                    continue
                asset = ref.get("asset_id")
                try:
                    valid = str(UUID(asset)) == asset
                except (ValueError, TypeError, AttributeError):
                    valid = False
                if valid and ref.get("content_url") == f"/v1/assets/{asset}/content":
                    ref["preview"] = preview_reference(asset)
            for key, child in node.items():
                if key != "image_references":
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(result)
    return result


def image_preview_response(function):
    @functools.wraps(function)
    def decorated(*args, **kwargs):
        return present_images(function(*args, **kwargs))

    return decorated


def unavailable():
    return BusinessError(
        "PREVIEW_UNAVAILABLE",
        "Preview is unavailable; retry or explicitly view the original",
        503,
    )


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise unavailable()
    path.chmod(0o700)


def validate_preview(path):
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
            return False
        with Image.open(path, formats=["JPEG"]) as image:
            if not 0 < image.width <= 768 or not 0 < image.height <= 768:
                return False
            image.load()
        return True
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


class PreviewStore:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / "assets" / "derived"

    def _budget(self):
        count = total = 0

        def fail_walk(error):
            raise error

        for parent, dirs, files in os.walk(
            self.directory, followlinks=False, onerror=fail_walk
        ):
            for name in [*dirs, *files]:
                count += 1
                if count > MAX_FILES:
                    raise unavailable()
                path = Path(parent) / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise unavailable()
                if stat.S_ISREG(info.st_mode):
                    if name.startswith(".preview-") and name.endswith(".tmp"):
                        path.unlink()
                        continue
                    total += info.st_size
        if total + MAX_BYTES > MAX_STORE_BYTES:
            raise unavailable()

    def get(self, sha256, *, source_path=None):
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise unavailable()
        source = (
            Path(source_path)
            if source_path is not None
            else self.root / "assets" / "original" / sha256
        )
        target = self.directory / sha256 / f"{RECIPE}.jpg"
        temporary = None
        lock = None
        try:
            if (
                source.parent.is_symlink()
                or source.is_symlink()
                or not source.is_file()
                or not 0 < source.stat().st_size <= 20 * 1024**2
            ):
                raise unavailable()
            private_directory(self.root / "assets")
            private_directory(self.directory)
            if target.parent.is_symlink():
                raise unavailable()
            if validate_preview(target):
                return target
            import fcntl

            lock = os.open(
                self.directory / ".generation.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if validate_preview(target):
                    return target
                raise BusinessError(
                    "PREVIEW_BUSY", "Preview generation is busy; retry later", 503
                ) from None
            private_directory(target.parent)
            if validate_preview(target):
                return target
            self._budget()
            descriptor, name = tempfile.mkstemp(
                prefix=".preview-", suffix=".tmp", dir=target.parent
            )
            temporary = Path(name)
            os.close(descriptor)
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "wardrobe.preview_worker",
                    str(source),
                    sha256,
                    name,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            )
            if process.returncode or not validate_preview(temporary):
                raise unavailable()
            os.replace(temporary, target)
            temporary = None
            fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return target
        except BusinessError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            raise unavailable() from None
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)
            if lock is not None:
                os.close(lock)


def warm_previews(database, root, owner_id):
    """Explicit local operator command; no grants, model calls or business writes."""
    from sqlalchemy import select

    from .models import (
        Asset,
        Garment,
        GarmentAsset,
        IntakeSourceObject,
        Owner,
        ServiceState,
    )

    owner_id = str(UUID(owner_id))
    with database.sessions() as session:
        if session.get(Owner, owner_id) is None:
            raise ValueError("Owner not found")
        state = session.get(ServiceState, owner_id)
        revision = state.catalog_revision if state else None
    report = {
        "assets": 0,
        "generated": 0,
        "existing": 0,
        "failed": 0,
        "bytes": 0,
        "failures": [],
        "catalog_revision": revision,
        "catalog_changed": False,
    }
    store, last = PreviewStore(root), ""
    while True:
        with database.sessions() as session:
            rows = session.execute(
                select(Asset.id, Asset.sha256)
                .join(GarmentAsset)
                .join(Garment)
                .where(
                    Garment.owner_id == owner_id,
                    Garment.state == "active",
                    Asset.id > last,
                )
                .distinct()
                .order_by(Asset.id)
                .limit(100)
            ).all()
        if not rows:
            break
        for identifier, sha in rows:
            report["assets"] += 1
            existing = validate_preview(store.directory / sha / f"{RECIPE}.jpg")
            try:
                with database.sessions.begin() as session:
                    source = session.scalar(
                        select(IntakeSourceObject).where(
                            IntakeSourceObject.asset_id == identifier,
                            IntakeSourceObject.owner_id == owner_id,
                            IntakeSourceObject.state == "submitted",
                            IntakeSourceObject.report[
                                "asset_validation_mode"
                            ].as_string()
                            == "client_decoded",
                        )
                    )
                    view = source.view_sha256 if source else None
                path = (
                    store.get(
                        view, source_path=Path(root) / "intake" / "objects" / view
                    )
                    if view
                    else store.get(sha)
                )
                report["existing" if existing else "generated"] += 1
                report["bytes"] += path.stat().st_size
            except BusinessError as error:
                report["failed"] += 1
                report["failures"].append({"asset_id": identifier, "code": error.code})
        last = rows[-1][0]
    with database.sessions() as session:
        state = session.get(ServiceState, owner_id)
        report["catalog_changed"] = (
            state.catalog_revision if state else None
        ) != revision
    return report
