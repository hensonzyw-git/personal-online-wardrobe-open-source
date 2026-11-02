#!/usr/bin/env python3
"""Disposable real Nginx authentication/rate-limit/rotation/removal contract.

Exit 77 with NO_EVIDENCE when nginx or sandbox sockets are unavailable.
Uses only synthetic credentials, loopback upstream and a temporary Unix socket.
"""

import base64
import http.client
import importlib.util
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main():
    if not shutil.which("nginx"):
        print(
            "NO_EVIDENCE: nginx binary unavailable; real H-AUTH/H-HEAD/H-OPS/H-E2E not executed"
        )
        return 77
    root = Path(__file__).resolve().parents[1]
    ops = root / "ops/wardrobe-admin"
    stage = Path(sys.argv[1]).resolve()
    stage.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location(
        "nginx_test_admin_proxy", ops / "proxy.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    captured = []

    class SyntheticUpstream(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            captured.append(dict(self.headers))
            body = b'{"status":"succeeded","data":{"items":[],"has_more":false,"next_cursor":null,"catalog_revision":1,"server_now":1800000000}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticUpstream)
        endpoint = socket.socket()
        endpoint.bind(("127.0.0.1", 0))
        port = endpoint.getsockname()[1]
        endpoint.close()
    except PermissionError:
        print("NO_EVIDENCE: sandbox denies local sockets; real Nginx test not executed")
        return 77
    secret = "synthetic-nginx-only-wardrobe-token"
    app = module.AdminApp(
        "synthetic-user",
        module.Upstream("127.0.0.1", upstream.server_port, secret),
        ops / "ui",
    )
    proxy = module.Server(str(stage / "proxy.sock"), app)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (upstream, proxy)
    ]
    for thread in threads:
        thread.start()

    def set_password(password):
        hashed = subprocess.run(
            [
                "openssl",
                "passwd",
                "-apr1" if sys.platform == "darwin" else "-6",
                "-stdin",
            ],
            input=password + "\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        temporary = stage / "auth.new"
        temporary.write_text("synthetic-user:" + hashed + "\n")
        temporary.replace(stage / "auth")

    old_password, new_password = (
        "synthetic-old-password-481",
        "synthetic-new-password-792",
    )
    set_password(old_password)
    location = (
        (ops / "nginx-location.conf")
        .read_text()
        .replace("/etc/nginx/wardrobe-admin.htpasswd", str(stage / "auth"))
        .replace("/run/wardrobe-admin/proxy.sock", str(stage / "proxy.sock"))
        .replace("/var/log/nginx/wardrobe-admin.log", str(stage / "access.log"))
        .replace("/var/log/nginx/wardrobe-admin-error.log", str(stage / "error.log"))
    )
    (stage / "location.conf").write_text(location)
    server = (
        (ops / "nginx-server.conf")
        .read_text()
        .replace(
            "/etc/nginx/snippets/wardrobe-admin-location.conf",
            str(stage / "location.conf"),
        )
    )
    (stage / "server.conf").write_text(server)
    http_conf = (ops / "nginx-http.conf").read_text()

    def config(rate="60r/m", enabled=True):
        (stage / "http.conf").write_text(
            http_conf.replace("rate=60r/m", "rate=" + rate)
        )
        (stage / "nginx.conf").write_text(f"""pid {stage}/nginx.pid;
error_log {stage}/main-error.log crit;
events {{ worker_connections 128; }}
http {{
    access_log off;
    include {stage}/http.conf;
    server {{
        listen 127.0.0.1:{port};
        server_name example.com;
        {f"include {stage}/server.conf;" if enabled else ""}
        location = / {{ return 200 'public'; }}
        location = /en/ {{ return 200 'public-en'; }}
        location ^~ /reports/ {{
            auth_basic "Wardrobe admin";
            auth_basic_user_file {stage}/auth;
            root {stage};
        }}
        location / {{ return 404; }}
    }}
}}
""")

    (stage / "reports").mkdir(exist_ok=True)
    (stage / "reports/index.html").write_text("synthetic-private-reports")
    command = ["nginx", "-p", str(stage), "-c", str(stage / "nginx.conf")]
    config()
    running = False

    def request(path, password=None, method="GET", extra=None):
        headers = {"Host": "example.com"}
        if password is not None:
            headers["Authorization"] = (
                "Basic "
                + base64.b64encode(("synthetic-user:" + password).encode()).decode()
            )
        if extra:
            headers.update(extra)
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            client.request(method, path, headers=headers)
            response = client.getresponse()
            return response.status, response.read(), dict(response.headers)
        finally:
            client.close()

    def reload():
        subprocess.run(command + ["-t"], check=True, capture_output=True)
        subprocess.run(command + ["-s", "reload"], check=True, capture_output=True)
        time.sleep(0.2)

    try:
        subprocess.run(command + ["-t"], check=True, capture_output=True)
        subprocess.run(command, check=True, capture_output=True)
        running = True
        time.sleep(0.2)
        paths = [
            "/wardrobe-admin",
            "/wardrobe-admin/",
            "/wardrobe-admin/admin.js",
            "/wardrobe-admin/admin.css",
            "/wardrobe-admin/api/session",
            "/wardrobe-admin/api/garments",
            "/wardrobe-admin/images/00000000-0000-0000-0000-000000000001/00000000-0000-0000-0000-000000000002/preview",
            "/wardrobe-admin/unknown",
        ]
        for password in (None, "synthetic-wrong"):
            for path in paths:
                status, body, headers = request(path, password)
                assert status == 401, (path, status)
                assert secret.encode() not in body
                for name, value in module.SECURITY.items():
                    assert headers.get(name) == value, name
        for path in (
            "/wardrobe-admin/",
            "/wardrobe-admin/admin.js",
            "/wardrobe-admin/admin.css",
            "/wardrobe-admin/api/session",
            "/wardrobe-admin/api/garments",
            "/reports/index.html",
        ):
            assert request(path, old_password)[0] == 200, path
        assert request("/wardrobe-admin", old_password)[0] == 308
        assert captured and all(
            row["Authorization"] == "Bearer " + secret
            and row["Host"] == "wardrobe.example.com"
            and "Cookie" not in row
            and "Origin" not in row
            for row in captured
        )
        # Synthetic companion route verifies auth rotation and removal isolation.
        set_password(new_password)
        reload()
        for path in (
            "/reports/index.html",
            "/wardrobe-admin/",
            "/wardrobe-admin/api/garments",
        ):
            assert request(path, old_password)[0] == 401
            assert request(path, new_password)[0] == 200
        # Actual content phase rate rejection, including failed passwords.
        config("1r/m")
        reload()
        statuses = [
            request("/wardrobe-admin/", "synthetic-wrong")[0] for _ in range(140)
        ]
        assert 429 in statuses, "real rate limit did not reject"
        config(enabled=False)
        reload()
        for path in paths:
            assert request(path, new_password)[0] == 404, path
        assert request("/")[0] == request("/en/")[0] == 200
        assert request("/reports/index.html", new_password)[0] == 200
        assert request("/reports/index.html")[0] == 401
        log = (stage / "access.log").read_text()
        assert (
            secret not in log
            and old_password not in log
            and new_password not in log
            and "synthetic-user" not in log
        )
        for line in log.splitlines():
            assert set(json.loads(line)) == {
                "route",
                "method",
                "status",
                "request_id",
                "duration",
            }
        print(
            "PASS: real H-AUTH-1/2/3, H-PROXY-1, H-HEAD-1, H-OPS-1 synthetic Nginx contracts"
        )
        print(
            "NO_EVIDENCE: H-E2E-1 real wardrobe TCP writes and browser journeys require the separately initialized synthetic wardrobe service"
        )
        return 0
    finally:
        if running:
            subprocess.run(command + ["-s", "quit"], check=False, capture_output=True)
        for server in (proxy, upstream):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


if __name__ == "__main__":
    raise SystemExit(main())
