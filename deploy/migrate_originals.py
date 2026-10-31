"""Copy frozen originals through the API; never create or publish garments."""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx


def save_private(path, value):
    temporary = path.with_name(path.name + ".writing")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w") as file:
            json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def data(response):
    if response.status_code != 200:
        raise RuntimeError(f"API_HTTP_{response.status_code}")
    value = response.json()
    if value.get("status") != "succeeded":
        raise RuntimeError("API_WRITE_NOT_SUCCEEDED")
    return value["data"]


def migrate(source, manifest_file, receipt_file, base_url, token_file):
    source, manifest_file, receipt_file, token_file = map(
        Path, (source, manifest_file, receipt_file, token_file)
    )
    endpoint = urlsplit(base_url)
    if endpoint.scheme != "https" and not (
        endpoint.scheme == "http" and endpoint.hostname in {"127.0.0.1", "localhost"}
    ):
        raise ValueError("HTTPS_OR_LOOPBACK_REQUIRED")
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("INVALID_ENDPOINT")
    if source.is_symlink() or not source.is_dir():
        raise ValueError("INVALID_SOURCE")
    root = source.resolve()
    if receipt_file.resolve().is_relative_to(root):
        raise ValueError("RECEIPT_MUST_BE_OUTSIDE_SOURCE")
    if token_file.stat().st_mode & 0o077:
        raise ValueError("TOKEN_FILE_MUST_BE_PRIVATE")
    token = token_file.read_text().strip()
    if not 32 <= len(token) <= 512:
        raise ValueError("INVALID_TOKEN_LENGTH")
    raw = manifest_file.read_bytes()
    manifest = json.loads(raw)
    if manifest["schema_version"] != "wardrobe-manifest/1":
        raise ValueError("INVALID_MANIFEST_VERSION")
    batch = str(UUID(manifest["manifest_id"]))
    manifest_hash = hashlib.sha256(raw).hexdigest()
    records = manifest["records"]
    ids = [str(UUID(record["source_id"])) for record in records]
    if len(set(ids)) != len(records):
        raise ValueError("DUPLICATE_SOURCE_ID")
    if receipt_file.exists():
        receipt = json.loads(receipt_file.read_text())
        if (
            receipt["manifest_id"] != batch
            or receipt["manifest_sha256"] != manifest_hash
        ):
            raise ValueError("RECEIPT_MANIFEST_MISMATCH")
    else:
        receipt = {
            "schema_version": "wardrobe-original-transfer/1",
            "manifest_id": batch,
            "manifest_sha256": manifest_hash,
            "records": {},
            "garments_created": 0,
            "published": False,
        }
    with httpx.Client(
        base_url=base_url,
        headers={"Authorization": "Bearer " + token},
        timeout=120,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for index, record in enumerate(records, 1):
            source_id = str(UUID(record["source_id"]))
            item = receipt["records"].setdefault(
                source_id,
                {
                    "relative_path": record["relative_path"],
                    "sha256": record["sha256"],
                    "size": record["size"],
                    "state": "pending",
                    "attempt": 0,
                },
            )
            try:
                if record["state"] != "readable":
                    raise ValueError("SOURCE_NOT_READABLE")
                relative = Path(record["relative_path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("INVALID_SOURCE_PATH")
                path = root
                for part in relative.parts:
                    path = path / part
                    if path.is_symlink():
                        raise ValueError("SOURCE_SYMLINK")
                if not path.resolve().is_relative_to(root):
                    raise ValueError("SOURCE_OUTSIDE_ROOT")
                content = path.read_bytes()
                if (
                    len(content) != record["size"]
                    or hashlib.sha256(content).hexdigest() != record["sha256"]
                ):
                    raise ValueError("SOURCE_CHANGED_AFTER_FREEZE")
                if item.get("upload_id"):
                    state = data(client.get(f"/v1/asset-uploads/{item['upload_id']}"))
                    if state["state"] == "ready":
                        if state["sha256"] != record["sha256"]:
                            raise ValueError("UPLOAD_HASH_MISMATCH")
                        item.update(state="ready", asset_id=state["asset_id"])
                        item.pop("error", None)
                        save_private(receipt_file, receipt)
                        continue
                    if state["expires_at"] <= int(time.time()):
                        item["attempt"] += 1
                        item.pop("upload_id", None)
                prepared = data(
                    client.post(
                        "/v1/asset-uploads",
                        headers={
                            "Idempotency-Key": f"{batch}:{source_id}:{item['attempt']}"
                        },
                        json={"sha256": record["sha256"], "size": record["size"]},
                    )
                )
                upload_id = str(UUID(prepared["upload_id"]))
                expected_url = f"/v1/asset-uploads/{upload_id}/content"
                if prepared["upload_url"] != expected_url:
                    raise ValueError("UNEXPECTED_UPLOAD_PATH")
                item.update(upload_id=upload_id, state="pending")
                save_private(receipt_file, receipt)
                result = data(
                    client.put(
                        expected_url,
                        headers={"X-Wardrobe-Upload-Token": prepared["upload_token"]},
                        content=content,
                    )
                )
                if (
                    result["state"] != "ready"
                    or result["sha256"] != record["sha256"]
                    or result["size"] != record["size"]
                ):
                    raise ValueError("UPLOAD_RECEIPT_MISMATCH")
                item.update(state="ready", asset_id=result["asset_id"])
                item.pop("error", None)
            except (
                ValueError,
                OSError,
                RuntimeError,
                KeyError,
                httpx.HTTPError,
            ) as error:
                item.update(state="failed", error=type(error).__name__)
                if isinstance(error, RuntimeError) and str(error).startswith(
                    "API_HTTP_"
                ):
                    item["error"] = str(error)
            save_private(receipt_file, receipt)
            if index % 10 == 0 or index == len(records):
                ready = sum(x["state"] == "ready" for x in receipt["records"].values())
                print(f"Processed {index}/{len(records)}; ready {ready}", flush=True)
    ready = sum(x["state"] == "ready" for x in receipt["records"].values())
    receipt["summary"] = {
        "source_files": len(records),
        "ready": ready,
        "unresolved": len(records) - ready,
    }
    save_private(receipt_file, receipt)
    return receipt["summary"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()
    try:
        summary = migrate(
            args.source, args.manifest, args.receipt, args.base_url, args.token_file
        )
        print(json.dumps(summary))
        raise SystemExit(0 if summary["unresolved"] == 0 else 1)
    except (ValueError, OSError, RuntimeError, KeyError, httpx.HTTPError) as error:
        parser.exit(
            2,
            f"Transfer failed ({type(error).__name__}); inspect the private receipt.\n",
        )


if __name__ == "__main__":
    main()
