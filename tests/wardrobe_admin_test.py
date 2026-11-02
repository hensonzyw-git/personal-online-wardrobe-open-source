"""Deterministic synthetic fixtures. No production files or external network."""

import http.client
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "ops/wardrobe-admin"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


proxy = load("wardrobe_admin_proxy", OPS / "proxy.py")
install = load("wardrobe_admin_install", OPS / "install.py")
GID = "00000000-0000-0000-0000-000000000010"
AID = "00000000-0000-0000-0000-000000000020"
PATH = "/wardrobe-admin/api/garments/" + GID
BODY = b'{"base_revision":1,"patch":{"name":"synthetic updated"}}'
SYNTHETIC_TOKEN = "synthetic-test-only-secret-123456789"


class FakeUpstream:
    def __init__(self):
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return proxy.Response(
            200,
            b'{"status":"succeeded","data":{}}',
            {"Content-Type": "application/json"},
        )


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000
        self.upstream = FakeUpstream()
        self.app = proxy.AdminApp(
            "synthetic-user", self.upstream, clock=lambda: self.now
        )
        self.base = [
            ("Host", "example.com"),
            ("X-Wardrobe-Admin-User", "synthetic-user"),
        ]
        response = self.app.handle("GET", "/wardrobe-admin/api/session", self.base)
        self.token = json.loads(response.body)["csrf_token"]
        self.write = self.base + [
            ("Origin", proxy.ORIGIN),
            ("Content-Type", "application/json"),
            ("Idempotency-Key", "synthetic-key"),
            ("Cookie", proxy.COOKIE + "=" + self.token),
            ("X-CSRF-Token", self.token),
        ]

    def call(self, method="PATCH", path=PATH, headers=None, body=BODY):
        return self.app.handle(
            method, path, self.write if headers is None else headers, body
        )

    def replace(self, name, value, source=None):
        return [
            (k, v)
            for k, v in (self.write if source is None else source)
            if k.lower() != name.lower()
        ] + ([] if value is None else [(name, value)])

    def assertPrivate(self, response):
        for key, value in proxy.SECURITY.items():
            self.assertEqual(response.headers[key], value)
        self.assertFalse(
            any(k.lower().startswith("access-control") for k in response.headers)
        )

    def test_H_CSRF_1_valid_write_and_no_browser_credentials_forwarded(self):
        result = self.call()
        self.assertEqual(result.status, 200)
        args, kwargs = self.upstream.calls[0]
        self.assertEqual(
            args, ("PATCH", "/v1/operator/garments/" + GID, BODY, "synthetic-key")
        )
        self.assertFalse(kwargs["image"])

    def test_H_CSRF_1_exact_origin(self):
        for origin in (
            None,
            "null",
            "https://evil.test",
            "http://example.com",
            "https://example.com:443",
            "https://wardrobe.example.com",
        ):
            with self.subTest(origin=origin):
                self.assertEqual(
                    self.call(headers=self.replace("Origin", origin)).status, 403
                )
        self.assertEqual(self.upstream.calls, [])

    def test_H_CSRF_1_metadata_referer_and_duplicates(self):
        for name, code in (("Origin", "CSRF_DENIED"), ("X-CSRF-Token", "CSRF_INVALID")):
            value = proxy.ORIGIN if name == "Origin" else self.token
            response = self.call(headers=self.write + [(name, value)])
            self.assertEqual(response.status, 403)
            self.assertEqual(json.loads(response.body)["error"]["code"], code)
        for extra in (
            [("Origin", proxy.ORIGIN)],
            [("Referer", "https://evil.test/")],
            [("Sec-Fetch-Site", "same-site")],
            [("Sec-Fetch-Site", "cross-site")],
            [("Sec-Fetch-Site", "none")],
            [("X-CSRF-Token", self.token)],
        ):
            with self.subTest(extra=extra):
                self.assertIn(self.call(headers=self.write + extra).status, (403, 422))
        self.assertEqual(self.upstream.calls, [])

    def test_H_CSRF_1_cookie_header_signature_user_expiry(self):
        self.assertEqual(
            self.call(headers=self.replace("X-CSRF-Token", "\u00e9")).status, 403
        )
        variants = [
            self.replace("Cookie", None),
            self.replace("X-CSRF-Token", None),
            self.replace("X-CSRF-Token", "wrong"),
            self.replace("Cookie", proxy.COOKIE + "=wrong"),
            self.replace(
                "Cookie",
                proxy.COOKIE
                + "="
                + self.token
                + "; "
                + proxy.COOKIE
                + "="
                + self.token,
            ),
        ]
        other = self.app.token("other-user")
        variants.append(
            self.replace(
                "Cookie",
                proxy.COOKIE + "=" + other,
                self.replace("X-CSRF-Token", other),
            )
        )
        for headers in variants:
            self.assertEqual(self.call(headers=headers).status, 403)
        self.now += 900
        self.assertEqual(self.call().status, 403)
        self.assertEqual(self.upstream.calls, [])

    def test_H_CSRF_2_restart_and_refresh_preserve_business_key(self):
        self.app.secret = b"restarted-memory-secret"
        self.assertEqual(self.call().status, 403)
        response = self.app.handle("GET", "/wardrobe-admin/api/session", self.base)
        new = json.loads(response.body)["csrf_token"]
        headers = self.replace(
            "Cookie", proxy.COOKIE + "=" + new, self.replace("X-CSRF-Token", new)
        )
        self.assertEqual(self.call(headers=headers).status, 200)
        self.assertEqual(self.upstream.calls[0][0][3], "synthetic-key")
        cookie = response.headers["Set-Cookie"]
        for flag in (
            "Secure",
            "HttpOnly",
            "SameSite=Strict",
            "Path=/wardrobe-admin/",
            "Max-Age=900",
        ):
            self.assertIn(flag, cookie)
        self.assertNotIn("Domain=", cookie)
        self.assertNotIn(SYNTHETIC_TOKEN, response.body.decode())

    def test_H_PROXY_1_identity_host_and_basic_rejected(self):
        for headers in (
            self.replace("Host", None),
            self.replace("Host", "wardrobe.example.com"),
            self.replace("X-Wardrobe-Admin-User", "other-user"),
            self.replace("Authorization", "Basic synthetic"),
            self.base + [("Host", "example.com")],
        ):
            self.assertIn(
                self.app.handle("GET", "/wardrobe-admin/", headers).status,
                (403, 421, 422),
            )
        self.assertEqual(self.upstream.calls, [])

    def test_H_PROXY_2_path_and_method_whitelist(self):
        for path in (
            "https://evil.test/",
            "http://[invalid",
            "/wardrobe-admin/../proxy.py",
            "/wardrobe-admin/%2e%2e/proxy.py",
            "/wardrobe-admin/%252fetc",
            "/wardrobe-admin//admin.js",
            "/wardrobe-admin/api/garments/../../file",
            PATH.upper(),
            PATH + "/purge",
            "/wardrobe-admin/proxy.py",
            PATH + "/assets/" + AID + "/content",
        ):
            self.assertEqual(self.call(path=path).status, 404, path)
        for method in ("DELETE", "PUT", "OPTIONS", "GET"):
            self.assertIn(
                self.call(method=method, path=PATH + "/delete", body=b"").status,
                (403, 405),
            )
        self.assertEqual(self.upstream.calls, [])

    def test_H_PROXY_2_query_whitelist_and_default_operator_mapping(self):
        base = "/wardrobe-admin/api/garments"
        for query in (
            "?url=http://evil.test",
            "?limit=0",
            "?limit=101",
            "?limit=01",
            "?state=all",
            "?sort=id",
            "?order=DESC",
            "?category=",
            "?category=x&category=y",
            "?cursor=" + "x" * 4097,
            "?_method=DELETE",
        ):
            self.assertEqual(
                self.app.handle("GET", base + query, self.base).status, 422
            )
        good = "?state=deleted&category=%E8%A1%A3&sort=created_at&order=asc&limit=7"
        self.assertEqual(self.app.handle("GET", base + good, self.base).status, 200)
        self.assertTrue(
            self.upstream.calls[-1][0][1].startswith("/v1/operator/garments?")
        )
        self.assertEqual(self.app.handle("GET", base, self.base).status, 200)
        self.assertEqual(self.upstream.calls[-1][0][1], "/v1/operator/garments")

    def test_H_PROXY_2_request_shape_no_partial_write(self):
        bodies = [
            b'{"base_revision":true,"patch":{"name":"x"}}',
            b'{"base_revision":1,"patch":{}}',
            b'{"base_revision":1,"patch":{"asset_ids":[]}}',
            b'{"base_revision":1,"patch":{"name":"x"},"identity_fields":{}}',
            b'{"base_revision":1,"base_revision":2,"patch":{"name":"x"}}',
            b'{"base_revision":1,"patch":{"name":NaN}}',
            b"null",
            b"[" * 2000 + b"]" * 2000,
        ]
        for body in bodies:
            self.assertEqual(self.call(body=body).status, 422)
        self.assertEqual(self.call(body=b"x" * 65537).status, 413)
        self.assertEqual(
            self.call(headers=self.replace("Content-Type", "text/plain")).status, 415
        )
        self.assertEqual(
            self.call(headers=self.replace("Idempotency-Key", None)).status, 422
        )
        self.assertEqual(
            self.call(headers=self.write + [("Idempotency-Key", "duplicate")]).status,
            422,
        )
        self.assertEqual(
            self.call(
                headers=self.write + [("X-HTTP-Method-Override", "DELETE")]
            ).status,
            422,
        )
        self.assertEqual(self.upstream.calls, [])

    def test_H_PROXY_2_range_and_conditional_get(self):
        self.assertEqual(
            self.app.handle(
                "GET", "/wardrobe-admin/admin.js", self.base + [("Range", "bytes=0-1")]
            ).status,
            416,
        )
        response = self.app.handle(
            "GET", "/wardrobe-admin/admin.js", self.base + [("If-None-Match", "*")]
        )
        self.assertEqual(response.status, 200)
        self.assertNotIn("ETag", response.headers)

    def test_H_HEAD_1_static_errors_session_and_no_redirect_before_auth(self):
        for path in (
            "/wardrobe-admin",
            "/wardrobe-admin/",
            "/wardrobe-admin/admin.js",
            "/wardrobe-admin/admin.css",
            "/wardrobe-admin/api/session",
            "/wardrobe-admin/unknown",
        ):
            response = self.app.handle("GET", path, self.base)
            self.assertPrivate(response)
            self.assertNotIn(SYNTHETIC_TOKEN.encode(), response.body)
        self.assertEqual(
            self.app.handle("GET", "/wardrobe-admin", [("Host", "example.com")]).status,
            403,
        )
        self.assertEqual(
            self.app.handle("GET", "/wardrobe-admin", self.base).status, 308
        )
        for path in (
            "/wardrobe-admin/",
            "/wardrobe-admin/api/session",
            "/wardrobe-admin/api/garments",
        ):
            self.assertEqual(
                self.app.handle(
                    "GET", path, self.base + [("Origin", "https://evil.test")]
                ).status,
                403,
            )
        self.assertEqual(self.upstream.calls, [])

    def test_H_UI_2_image_projection_ignores_upstream_url_and_deleted(self):
        item = {
            "id": GID,
            "name": "<img onerror=alert(1)>",
            "state": "active",
            "image_references": [
                {
                    "asset_id": AID,
                    "content_url": "https://evil.test/steal",
                    "source_reference": {"private_path": "synthetic-private"},
                }
            ],
            "private_path": "synthetic-private",
        }
        data = proxy.project_envelope({"data": {"items": [item]}})["data"]["items"][0]
        self.assertEqual(
            data["image_references"][0]["preview_url"],
            "/wardrobe-admin/images/" + GID + "/" + AID + "/preview",
        )
        self.assertNotIn("evil.test", json.dumps(data))
        self.assertNotIn("synthetic-private", json.dumps(data))
        item["state"] = "deleted"
        self.assertEqual(
            proxy.project_envelope({"data": {"garment": item}})["data"]["garment"][
                "image_references"
            ],
            [],
        )

    def test_H_FILE_1_modes_symlink_owner_and_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            folder.chmod(0o700)
            path = folder / "synthetic.json"
            path.write_text('{"token":"synthetic"}')
            path.chmod(0o600)
            self.assertEqual(proxy.secure_json(path), {"token": "synthetic"})
            for mode in (0o644, 0o640, 0o660):
                path.chmod(mode)
                with self.assertRaises(ValueError):
                    proxy.secure_json(path)
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                proxy.secure_json(path, uid=os.geteuid() + 1)
            link = folder / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                proxy.secure_json(link)
            folder.chmod(0o750)
            with self.assertRaises(ValueError):
                proxy.secure_json(path)
            folder.chmod(0o700)
            path.write_text("x" * 8193)
            with self.assertRaises(ValueError):
                proxy.secure_json(path)

    def test_H_OPS_1_install_plan_copied_and_symlink_sites(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                available = root / "etc/nginx/sites-available/example.com"
                enabled = root / "etc/nginx/sites-enabled/example.com"
                available.parent.mkdir(parents=True)
                enabled.parent.mkdir(parents=True)
                source = "server {\n" + install.ANCHOR + "\n    include reports;\n}\n"
                available.write_text(source)
                if symlink:
                    enabled.symlink_to(available)
                else:
                    enabled.write_text(source)
                files = install.plan(OPS, root)
                sites = [p for p in files if p.name == "example.com"]
                self.assertEqual(len(sites), 1 if symlink else 2)
                self.assertEqual(available.read_text(), source)  # plan is read-only
                self.assertFalse(
                    any("htpasswd" in str(p) or "dist" in str(p) for p in files)
                )
                for p in sites:
                    self.assertIn(install.MARKER.encode(), files[p])
                    self.assertIn(b"include reports", files[p])
                    p.write_bytes(files[p])
                removed = install.plan(OPS, root, remove=True)
                self.assertEqual(len(removed), 1 if symlink else 2)
                self.assertTrue(
                    all(
                        install.MARKER.encode() not in data
                        and b"include reports" in data
                        for data in removed.values()
                    )
                )

    def test_H_OPS_1_installer_failed_reload_restores_files_and_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "synthetic-code.py"
            target.write_bytes(b"old code")
            calls = []
            failed = False

            def run(args, **kwargs):
                nonlocal failed
                calls.append(args)
                if args[:2] == ["systemctl", "is-active"]:
                    return subprocess.CompletedProcess(
                        args, 0 if args[-1].endswith(".service") else 1
                    )
                if args[:2] == ["systemctl", "is-enabled"]:
                    return subprocess.CompletedProcess(
                        args, 0 if args[-1].endswith(".socket") else 1
                    )
                if args == ["systemctl", "reload", "nginx"] and not failed:
                    failed = True
                    raise subprocess.CalledProcessError(1, args)
                return subprocess.CompletedProcess(args, 0)

            with (
                patch.object(install.os, "geteuid", return_value=0),
                patch.object(install, "preflight"),
                patch.object(install.subprocess, "run", side_effect=run),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                install.apply({target: b"new code"}, backup_root=root / "backup")
            self.assertEqual(target.read_bytes(), b"old code")
            self.assertIn(["systemctl", "enable", install.NAME + ".socket"], calls)
            self.assertIn(["systemctl", "stop", install.NAME + ".socket"], calls)
            self.assertIn(["systemctl", "disable", install.NAME + ".service"], calls)
            self.assertIn(["systemctl", "restart", install.NAME + ".service"], calls)

    def test_H_OPS_1_new_units_are_stopped_and_disabled_before_file_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = root / "proxy.py"
            code.write_bytes(b"old code")
            units = {
                install.NAME + suffix: root / (install.NAME + suffix)
                for suffix in (".socket", ".service")
            }
            active = {unit: False for unit in units}
            enabled = dict(active)
            first_reload = True
            teardown = []

            def run(args, **kwargs):
                nonlocal first_reload
                result = 0
                if args[:2] == ["systemctl", "is-active"]:
                    result = 0 if active[args[-1]] else 3
                elif args[:2] == ["systemctl", "is-enabled"]:
                    result = 0 if enabled[args[-1]] else 1
                elif args[:3] == ["systemctl", "enable", "--now"]:
                    self.assertTrue(units[args[-1]].is_file())
                    active[args[-1]] = enabled[args[-1]] = True
                elif args[:2] == ["systemctl", "try-restart"]:
                    active[args[-1]] = (
                        True  # Simulate a request activating the new service.
                    )
                elif args[:2] == ["systemctl", "stop"]:
                    self.assertTrue(
                        units[args[-1]].is_file(), "stop must precede unit removal"
                    )
                    active[args[-1]] = False
                    teardown.append(("stop", args[-1]))
                elif args[:2] == ["systemctl", "disable"]:
                    # Match systemd's failure when a just-installed unit has
                    # already been removed. The old rollback order fails here.
                    if not units[args[-1]].is_file():
                        result = 1
                    else:
                        enabled[args[-1]] = False
                        teardown.append(("disable", args[-1]))
                elif args == ["systemctl", "reload", "nginx"] and first_reload:
                    first_reload = False
                    raise subprocess.CalledProcessError(1, args)
                elif args == ["systemctl", "reload", "nginx"]:
                    self.assertEqual(code.read_bytes(), b"old code")
                    self.assertFalse(any(active.values()))
                    self.assertFalse(any(enabled.values()))
                if kwargs.get("check") and result:
                    raise subprocess.CalledProcessError(result, args)
                return subprocess.CompletedProcess(args, result)

            files = {
                code: b"new code",
                **{path: b"new unit" for path in units.values()},
            }
            with (
                patch.object(install.os, "geteuid", return_value=0),
                patch.object(install, "preflight"),
                patch.object(install.subprocess, "run", side_effect=run),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                install.apply(files, backup_root=root / "backup")
            self.assertFalse(any(path.exists() for path in units.values()))
            self.assertEqual(code.read_bytes(), b"old code")
            self.assertEqual(len(teardown), 4)
            self.assertFalse(any(active.values()))
            self.assertFalse(any(enabled.values()))

    def test_H_OPS_1_emergency_remove_does_not_require_proxy_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "site.conf"
            target.write_bytes(b"old site")
            calls = []

            def run(args, **kwargs):
                calls.append(args)
                return subprocess.CompletedProcess(args, 0)

            with (
                patch.object(install.os, "geteuid", return_value=0),
                patch.object(
                    install,
                    "preflight",
                    side_effect=AssertionError("must not inspect credentials"),
                ),
                patch.object(
                    install.pwd,
                    "getpwnam",
                    side_effect=AssertionError("must not inspect proxy identity"),
                ),
                patch.object(install.subprocess, "run", side_effect=run),
            ):
                install.apply(
                    {target: b"new site"}, remove=True, backup_root=Path(tmp) / "backup"
                )
            self.assertEqual(target.read_bytes(), b"new site")
            self.assertEqual(calls, [["nginx", "-t"], ["systemctl", "reload", "nginx"]])

    def test_H_AUTH_and_HEAD_production_nginx_contract(self):
        loc = (OPS / "nginx-location.conf").read_text()
        shared = "auth_basic_user_file /etc/nginx/wardrobe-admin.htpasswd;"
        self.assertIn(shared, loc)
        self.assertIn('auth_basic "Wardrobe admin";', loc)
        self.assertIn("burst=120 nodelay", loc)
        self.assertIn("rate=60r/m", (OPS / "nginx-http.conf").read_text())
        self.assertNotIn("return 30", (OPS / "nginx-server.conf").read_text())
        for name in proxy.SECURITY:
            self.assertRegex(loc, "add_header " + name + r" .* always;")
        self.assertIn('proxy_set_header Authorization "";', loc)
        self.assertIn("proxy_set_header X-Wardrobe-Admin-User $remote_user;", loc)
        self.assertIn("proxy_request_buffering off;", loc)
        self.assertNotIn('$request"', (OPS / "nginx-http.conf").read_text())

    def test_H_UI_interactions_execute_actual_module_with_dom_harness(self):
        result = subprocess.run(
            ["node", "tests/wardrobe-admin-ui.test.mjs"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_H_UI_1_and_2_node_values_and_source_contract(self):
        script = """import {LABEL_SPEC,formatTime,parseValue} from './ops/wardrobe-admin/ui/admin.js';
import assert from 'node:assert/strict';
assert.equal(Object.keys(LABEL_SPEC).length,36);
assert.equal(parseValue(LABEL_SPEC.windproof,'false'),false);
assert.deepEqual(parseValue(LABEL_SPEC.style,''),[]);
assert.throws(()=>parseValue(LABEL_SPEC.thickness,'invalid'));
assert.throws(()=>parseValue(LABEL_SPEC.style,'a\\nb\\nc\\nd\\ne\\nf'));
assert.equal(formatTime(1800000000), '2027年1月15日 08:00:00 UTC');
console.log(JSON.stringify(Object.keys(LABEL_SPEC)));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        labels = json.loads(result.stdout)
        self.assertEqual(len(labels), 36)
        js = (OPS / "ui/admin.js").read_text()
        for prohibited in (
            "innerHTML",
            "outerHTML",
            "localStorage",
            "sessionStorage",
            "indexedDB",
            "Bearer ",
            "privateKey",
            "eval(",
            "insertAdjacentHTML",
        ):
            self.assertNotIn(prohibited, js)
        self.assertIn("textContent", js)
        self.assertIn("crypto.randomUUID()", js)
        self.assertIn("JSON.stringify", js)
        self.assertIn("CSRF_INVALID", js)
        self.assertIn("latestConflict", js)
        self.assertIn("newAction", js)
        self.assertNotIn("upload", (OPS / "ui/index.html").read_text())
        with patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
            again = subprocess.run(
                ["node", "--input-type=module", "-e", script],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(again.returncode, 0, again.stderr)


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.captured = []
        owner = self
        from email.message import Message

        class FakeSocket:
            def settimeout(self, value):
                pass

            def shutdown(self, _):
                pass

            def close(self):
                pass

        class FakeResponse:
            def __init__(self):
                self.status, mime, self.data, extras = owner.reply
                self.offset = 0
                self.headers = Message()
                self.headers["Content-Type"] = mime
                self.headers["Content-Length"] = str(len(self.data))
                for key, value in extras.items():
                    self.headers[key] = value

            def getheader(self, name, default=None):
                return self.headers.get(name, default)

            def read(self, size):
                data = self.data[self.offset : self.offset + size]
                self.offset += len(data)
                return data

            def close(self):
                pass

        class FakeConnection:
            def __init__(self, *args, **kwargs):
                self.sock = FakeSocket()

            def connect(self):
                pass

            def request(self, method, path, body=None, headers=None):
                owner.captured.append((path, headers))

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        self.patcher = patch.object(proxy, "HTTPConnection", FakeConnection)
        self.patcher.start()
        self.client = proxy.Upstream("127.0.0.1", 18830, SYNTHETIC_TOKEN)
        self.reply = (
            200,
            "application/json",
            b'{"status":"succeeded","data":{"items":[]}}',
            {},
        )

    def tearDown(self):
        self.patcher.stop()

    def test_H_PROXY_1_http_client_reconstructed_headers(self):
        result = self.client.request("GET", "/v1/operator/garments", b"", None)
        self.assertEqual(result.status, 200)
        path, headers = self.captured[0]
        self.assertEqual(path, "/v1/operator/garments")
        self.assertEqual(headers["Host"], "wardrobe.example.com")
        self.assertEqual(headers["Authorization"], "Bearer " + SYNTHETIC_TOKEN)
        for key in (
            "Cookie",
            "Origin",
            "Referer",
            "X-Wardrobe-Admin-User",
            "Proxy-Authorization",
        ):
            self.assertNotIn(key, headers)
        self.assertNotIn(SYNTHETIC_TOKEN.encode(), result.body)

    def test_H_PROXY_2_no_redirect_no_challenge_no_upstream_secrets(self):
        for status, mime, data, extras in [
            (302, "text/html", b"", {"Location": "http://127.0.0.1:1/steal"}),
            (
                401,
                "application/json",
                SYNTHETIC_TOKEN.encode(),
                {"WWW-Authenticate": "Bearer synthetic"},
            ),
            (403, "text/plain", SYNTHETIC_TOKEN.encode(), {}),
            (200, "text/html", b"<secret>", {}),
            (200, "application/json", b"{not-json", {}),
            (
                200,
                "application/json",
                b'{"status":"succeeded","data":{},"data":{}}',
                {},
            ),
            (200, "application/json", b'{"status":"succeeded","data":NaN}', {}),
        ]:
            self.reply = (status, mime, data, extras)
            result = self.client.request("GET", "/v1/operator/garments", b"", None)
            self.assertIn(result.status, (502, 503))
            self.assertNotIn(SYNTHETIC_TOKEN.encode(), result.body)
            for key in ("Location", "WWW-Authenticate", "Set-Cookie"):
                self.assertNotIn(key, result.headers)
        self.assertEqual(len(self.captured), 7)  # No automatic redirect/retry.

    def test_H_PROXY_2_size_and_controlled_business_errors(self):
        self.reply = (200, "application/json", b"x" * (proxy.MAX_JSON + 1), {})
        self.assertEqual(
            self.client.request("GET", "/v1/operator/garments", b"", None).status, 502
        )
        self.reply = (
            409,
            "application/json",
            b'{"error":{"code":"REVISION_CONFLICT","message":"synthetic-private-path"}}',
            {"Set-Cookie": "secret=yes"},
        )
        result = self.client.request(
            "PATCH", "/v1/operator/garments/" + GID, BODY, "synthetic-key"
        )
        self.assertEqual(result.status, 409)
        self.assertEqual(json.loads(result.body)["error"]["code"], "REVISION_CONFLICT")
        self.assertNotIn(b"synthetic-private-path", result.body)
        self.assertNotIn("Set-Cookie", result.headers)

    def test_H_PROXY_2_images_stream_and_cap(self):
        self.reply = (
            200,
            "image/jpeg",
            b"synthetic-image-bytes",
            {"Content-Disposition": "filename=synthetic-private.jpg"},
        )
        response = self.client.request(
            "GET",
            "/v1/operator/garments/" + GID + "/assets/" + AID + "/preview",
            b"",
            None,
            image=True,
            preview=True,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(b"".join(response.body), b"synthetic-image-bytes")
        self.assertNotIn("Content-Disposition", response.headers)
        self.reply = (200, "image/jpeg", b"x" * (150 * 1024 + 1), {})
        self.assertEqual(
            self.client.request(
                "GET", "/preview", b"", None, image=True, preview=True
            ).status,
            502,
        )
        self.reply = (200, "image/svg+xml", b"<svg/>", {})
        self.assertEqual(
            self.client.request("GET", "/preview", b"", None, image=True).status, 502
        )

    def test_H_PROXY_2_hard_deadline_aborts_original_transport_and_cancels(self):
        timers = []

        class Timer:
            def __init__(self, delay, callback):
                self.delay, self.callback, self.cancelled = delay, callback, False
                timers.append(self)

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True

        with patch.object(proxy.threading, "Timer", Timer):
            result = self.client.request("GET", "/v1/operator/garments", b"", None)
            self.assertEqual(result.status, 200)
            self.assertTrue(timers[-1].cancelled)
            self.reply = (200, "image/jpeg", b"synthetic-image", {})
            result = self.client.request(
                "GET", "/preview", b"", None, image=True, preview=True
            )
            self.assertFalse(timers[-1].cancelled)
            self.assertEqual(b"".join(result.body), b"synthetic-image")
            self.assertTrue(timers[-1].cancelled)
            unread = self.client.request(
                "GET", "/preview", b"", None, image=True, preview=True
            )
            self.assertFalse(timers[-1].cancelled)
            unread.body.close()  # Disconnect while headers are being sent, before any bytes.
            self.assertTrue(timers[-1].cancelled)
        self.assertTrue(all(timer.delay == 15 for timer in timers))
        # getresponse may detach HTTPConnection.sock for Connection: close.
        # The hard deadline must still shutdown the original response transport.
        original = proxy.HTTPConnection
        sockets = []

        class Detached(original):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.sock.shutdown = unittest.mock.Mock()
                sockets.append(self.sock)

            def getresponse(self):
                result = super().getresponse()
                self.sock = None

                def expired_read(_):
                    timers[-1].callback()
                    raise OSError("synthetic deadline abort")

                result.read = expired_read
                return result

        self.reply = (200, "application/json", b"{}", {})
        with (
            patch.object(proxy.threading, "Timer", Timer),
            patch.object(proxy, "HTTPConnection", Detached),
        ):
            result = self.client.request("GET", "/v1/operator/garments", b"", None)
        self.assertEqual(result.status, 504)
        sockets[-1].shutdown.assert_called_once_with(socket.SHUT_RDWR)

    def test_H_CSRF_2_timeout_no_network_retry_and_fixed_upstream(self):
        for host in ("evil.test", "http://127.0.0.1", "0.0.0.0", "localhost"):
            with self.assertRaises(ValueError):
                proxy.Upstream(host, 8830, SYNTHETIC_TOKEN)
        with patch.object(proxy.HTTPConnection, "connect", side_effect=socket.timeout):
            response = self.client.request(
                "PATCH", "/v1/operator/garments/" + GID, BODY, "same-key"
            )
        self.assertEqual(response.status, 504)
        self.assertEqual(self.captured, [])


class RealSocketTest(unittest.TestCase):
    def test_H_PROXY_1_real_unix_socket_and_unsupported_methods_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "proxy.sock")
            fake = FakeUpstream()
            app = proxy.AdminApp("synthetic-user", fake)
            try:
                server = proxy.Server(path, app)
            except PermissionError:
                self.skipTest("NO_EVIDENCE: sandbox denies creating Unix sockets")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for method in ("GET", "TRACE"):
                    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    client.connect(path)
                    client.sendall(
                        (
                            method
                            + " /wardrobe-admin/ HTTP/1.1\r\nHost: example.com\r\nX-Wardrobe-Admin-User: synthetic-user\r\nConnection: close\r\n\r\n"
                        ).encode()
                    )
                    response = http.client.HTTPResponse(client)
                    response.begin()
                    response.read()
                    self.assertEqual(response.status, 200 if method == "GET" else 501)
                    self.assertEqual(response.getheader("Cache-Control"), "no-store")
                    self.assertEqual(
                        response.getheader("X-Content-Type-Options"), "nosniff"
                    )
                    client.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
