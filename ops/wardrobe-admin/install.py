#!/usr/bin/env python3
"""Render a read-only installation plan; --apply requires separate release authorization.

Never issues wardrobe grants, reads their values, changes htpasswd, runs migrations,
or deploys other site content. Run only against a preflight-verified host.
"""

import argparse
import grp
import hashlib
import json
import os
import pwd
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

MARKER = "    include /etc/nginx/snippets/wardrobe-admin.conf;"
ANCHOR = "    root /var/www/example.com/current;"
NAME = "wardrobe-admin"
FILES = {
    "proxy.py": "/opt/wardrobe-admin/proxy.py",
    "nginx-http.conf": "/etc/nginx/conf.d/wardrobe-admin.conf",
    "nginx-server.conf": "/etc/nginx/snippets/wardrobe-admin.conf",
    "nginx-location.conf": "/etc/nginx/snippets/wardrobe-admin-location.conf",
    "wardrobe-admin.service": "/etc/systemd/system/wardrobe-admin.service",
    "wardrobe-admin.socket": "/etc/systemd/system/wardrobe-admin.socket",
    **{
        f"ui/{name}": f"/var/lib/wardrobe-admin/ui/{name}"
        for name in ("index.html", "admin.js", "admin.css")
    },
}


def render_site(source, remove=False):
    if remove:
        return source.replace(MARKER + "\n", "")
    if MARKER in source:
        return source
    if source.count(ANCHOR) != 1:
        raise ValueError("Unexpected apex vhost shape; inspect before installation")
    return source.replace(ANCHOR, MARKER + "\n" + ANCHOR)


def plan(stage, root=Path("/"), remove=False):
    stage, root = Path(stage).resolve(), Path(root)
    files = {}
    if not remove:
        for source, target in FILES.items():
            path = stage / source
            if path.is_symlink() or not path.is_file():
                raise ValueError("Missing or unsafe staged artifact")
            files[root / target.lstrip("/")] = path.read_bytes()
    seen = set()
    for directory in ("sites-available", "sites-enabled"):
        path = root / "etc/nginx" / directory / "example.com"
        resolved = path.resolve()
        if root.resolve() not in resolved.parents:
            raise ValueError("Vhost target escapes installation root")
        if resolved in seen:
            continue
        seen.add(resolved)
        source = path.read_text()
        rendered = render_site(source, remove)
        if rendered != source:
            files[resolved] = rendered.encode()
    return files


def metadata(path, mode, uid=None):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != mode
        or uid is not None
        and info.st_uid != uid
    ):
        raise ValueError("Private credential metadata failed preflight")


def preflight():
    user = pwd.getpwnam(
        NAME
    )  # Provision this independent identity during authorized preflight.
    www = grp.getgrnam("www-data")
    private = Path("/etc/wardrobe-admin")
    info = private.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != user.pw_uid
    ):
        raise ValueError("Invalid private configuration directory")
    metadata(private / "wardrobe-token.json", 0o600, user.pw_uid)
    metadata(private / "operator.json", 0o600, user.pw_uid)
    metadata(Path("/etc/nginx/wardrobe-admin.htpasswd"), 0o640, 0)
    if Path("/etc/nginx/wardrobe-admin.htpasswd").stat().st_gid != www.gr_gid:
        raise ValueError("Admin auth group must match Nginx")


def apply(files, remove=False, backup_root=Path("/var/backups/wardrobe-admin")):
    if os.geteuid() != 0:
        raise SystemExit("Authorized installation requires the host administrator")
    if not remove:
        preflight()
    # Emergency removal must work even after proxy credentials/user disappear.
    previous_units = {}
    if not remove:
        for unit in (NAME + ".socket", NAME + ".service"):
            previous_units[unit] = {
                "active": subprocess.run(
                    ["systemctl", "is-active", "--quiet", unit],
                    capture_output=True,
                    check=False,
                ).returncode
                == 0,
                "enabled": subprocess.run(
                    ["systemctl", "is-enabled", "--quiet", unit],
                    capture_output=True,
                    check=False,
                ).returncode
                == 0,
            }
    backup = backup_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup.mkdir(parents=True, mode=0o700)
    old = {path: path.read_bytes() if path.exists() else None for path in files}
    for number, (path, data) in enumerate(old.items()):
        if data is not None:
            (backup / str(number)).write_bytes(data)
    (backup / "manifest.json").write_text(
        json.dumps({str(i): str(path) for i, path in enumerate(old)})
    )
    changed = []
    try:
        for path, data in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".wardrobe-admin-new")
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "wb") as output:
                output.write(data)
            temporary.replace(path)
            changed.append(path)
        subprocess.run(["nginx", "-t"], check=True)
        if not remove:
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            subprocess.run(
                ["systemctl", "enable", "--now", NAME + ".socket"], check=True
            )
            subprocess.run(["systemctl", "try-restart", NAME + ".service"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
    except Exception:
        # Validate the whole rollback set before changing files or service state.
        for path in changed:
            if not path.is_file() or path.read_bytes() != files[path]:
                raise RuntimeError("Concurrent change prevents automatic rollback")
        if previous_units:
            # New unit files must still exist while stopping/disabling them. A
            # failed first installation may not have loaded every unit yet.
            for unit in previous_units:
                subprocess.run(
                    ["systemctl", "stop", unit], check=False, capture_output=True
                )
            for unit, state in previous_units.items():
                if not state["enabled"]:
                    subprocess.run(
                        ["systemctl", "disable", unit], check=False, capture_output=True
                    )
        for path in reversed(changed):
            if old[path] is None:
                path.unlink()
            else:
                path.write_bytes(old[path])
        if previous_units:
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            for unit, state in previous_units.items():
                if state["enabled"]:
                    subprocess.run(["systemctl", "enable", unit], check=True)
                if state["active"]:
                    subprocess.run(["systemctl", "restart", unit], check=True)
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, default=Path(__file__).parent)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/"),
        help="Fixture root for read-only plan only",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove only the apex include; retain private artifacts and admin auth",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply only with separate explicit deployment authorization",
    )
    args = parser.parse_args()
    if args.apply and args.root != Path("/"):
        parser.error("--root is read-only fixture support")
    files = plan(args.stage, args.root, args.remove)
    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "plan",
                "files": [
                    {
                        "path": str(path),
                        "current_sha256": hashlib.sha256(path.read_bytes()).hexdigest()
                        if path.is_file()
                        else None,
                        "new_sha256": hashlib.sha256(data).hexdigest(),
                        "changed": not path.is_file() or path.read_bytes() != data,
                    }
                    for path, data in files.items()
                ],
            },
            indent=2,
        )
    )
    if args.apply:
        apply(files, args.remove)


if __name__ == "__main__":
    main()
