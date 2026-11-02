#!/usr/bin/env python3
"""Private Unix-socket adapter. No database, arbitrary URL or generic proxy surface."""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPException
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer
from urllib.parse import parse_qsl, urlencode, urlsplit

PREFIX = "/wardrobe-admin"
ORIGIN = "https://example.com"
COOKIE = "__Secure-wardrobe-admin-csrf"
MAX_BODY = 65536
MAX_JSON = 4 * 1024 * 1024
MAX_IMAGE = 32 * 1024 * 1024
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
SECURITY = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": CSP,
}
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ERRORS = {
    "NOT_FOUND": "未找到记录或页面",
    "INVALID_ARGUMENT": "请求参数不合法",
    "REVISION_CONFLICT": "记录已被修改，请对照最新值重新确认",
    "TARGET_REMOVED": "记录已删除",
    "RESTORE_UNAVAILABLE": "恢复期已结束或图片不满足恢复条件",
    "IDEMPOTENCY_CONFLICT": "该操作编号与原请求不一致",
    "IDEMPOTENCY_KEY_REQUIRED": "缺少有效操作编号",
    "INVALID_CURSOR": "分页参数不合法",
    "CURSOR_STALE": "目录已变化，请从首页重新加载",
    "DATABASE_UNAVAILABLE": "数据服务暂不可用",
    "ASSET_UNAVAILABLE": "图片不可用",
    "PREVIEW_BUSY": "预览暂不可用，请稍后重试",
    "ADMIN_CREDENTIAL_UNAVAILABLE": "后台服务授权不可用，请维护凭据",
    "UPSTREAM_UNAVAILABLE": "衣橱服务暂不可用，写入结果可能未知，请保留原操作重试",
    "UPSTREAM_TIMEOUT": "衣橱服务超时，写入结果可能未知，请保留原操作重试",
    "CSRF_DENIED": "来源校验失败",
    "CSRF_INVALID": "安全令牌已失效，请重新获取",
    "SCOPE_DENIED": "无权访问后台",
    "REQUEST_TOO_LARGE": "请求内容过大",
    "UNSUPPORTED_MEDIA_TYPE": "仅支持 JSON 请求",
    "METHOD_NOT_ALLOWED": "不支持此请求方法",
    "RANGE_NOT_SUPPORTED": "不支持分段读取",
    "RATE_LIMITED": "请求过于频繁，请稍后重试",
}


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("nonfinite JSON")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except RecursionError:
        raise ValueError("JSON nesting limit") from None


@dataclass
class Response:
    status: int
    body: object = b""
    headers: dict = field(default_factory=dict)

    def __post_init__(self):
        self.headers = {**SECURITY, **self.headers}


def encoded(data):
    return json.dumps(
        data, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def failure(status, code):
    return Response(
        status,
        encoded(
            {
                "status": "failed",
                "request_id": str(uuid.uuid4()),
                "error": {
                    "code": code,
                    "message": ERRORS.get(code, "请求失败，请稍后重试"),
                },
            }
        ),
        {"Content-Type": "application/json; charset=utf-8"},
    )


def secure_json(path, uid=None):
    """Reject symlinks, wrong UID/modes, and races before reading a credential."""
    path = Path(path)
    uid = os.geteuid() if uid is None else uid
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != uid
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise ValueError("Invalid credential directory")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 8192
        ):
            raise ValueError("Invalid credential file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return strict_json(handle.read(8193))
    finally:
        os.close(fd)


class ClosingBody:
    """Close the upstream even when downstream disconnects before first iteration."""

    def __init__(self, iterator, cleanup):
        self.iterator, self.cleanup, self.closed = iter(iterator), cleanup, False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.iterator)
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.iterator.close()
            finally:
                self.cleanup()


class Upstream:
    def __init__(self, host, port, token):
        if (
            host not in ("127.0.0.1", "::1")
            or type(port) is not int
            or not 1 <= port <= 65535
        ):
            raise ValueError("Upstream must be a fixed loopback endpoint")
        if (
            not isinstance(token, str)
            or not 16 <= len(token) <= 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
        ):
            raise ValueError("Invalid credential")
        self.host, self.port, self.token = host, port, token

    def request(self, method, path, body, key, image=False, preview=False):
        connection = HTTPConnection(self.host, self.port, timeout=2)
        deadline = time.monotonic() + 15
        response = None
        streamed = False
        timed_out = threading.Event()
        transport_socket = None

        def abort():
            timed_out.set()
            current = transport_socket or connection.sock
            if current is not None:
                try:
                    current.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                current.close()

        watchdog = threading.Timer(15, abort)
        watchdog.daemon = True
        watchdog.start()
        try:
            connection.connect()
            transport_socket = connection.sock
            connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
            headers = {
                "Host": "wardrobe.example.com",
                "Authorization": "Bearer " + self.token,
                "Accept": "image/*" if image else "application/json",
            }
            if body:
                headers["Content-Type"] = "application/json"
            if key:
                headers["Idempotency-Key"] = key
            connection.request(method, path, body=body or None, headers=headers)
            response = connection.getresponse()
            if response.status in (401, 403):
                return failure(503, "ADMIN_CREDENTIAL_UNAVAILABLE")
            if 300 <= response.status < 400:
                return failure(502, "UPSTREAM_UNAVAILABLE")
            mime = response.getheader("Content-Type", "").split(";")[0].lower()
            image_ok = image and response.status == 200
            cap = (150 * 1024 if preview else MAX_IMAGE) if image_ok else MAX_JSON
            lengths = response.headers.get_all("Content-Length", [])
            if (
                len(lengths) > 1
                or lengths
                and (not lengths[0].isdigit() or int(lengths[0]) > cap)
            ):
                return failure(502, "UPSTREAM_UNAVAILABLE")
            if image_ok:
                if (
                    mime not in ("image/jpeg", "image/png", "image/heic", "image/heif")
                    or preview
                    and mime != "image/jpeg"
                    or not lengths
                ):
                    return failure(502, "UPSTREAM_UNAVAILABLE")
                size = int(lengths[0])

                def chunks():
                    read = 0
                    try:
                        while read < size:
                            left = deadline - time.monotonic()
                            if left <= 0:
                                raise TimeoutError()
                            if connection.sock:
                                connection.sock.settimeout(left)
                            chunk = response.read(min(65536, size - read))
                            if not chunk:
                                raise OSError("Incomplete image")
                            read += len(chunk)
                            yield chunk
                    finally:
                        watchdog.cancel()
                        response.close()
                        connection.close()

                streamed = True

                def cleanup():
                    watchdog.cancel()
                    response.close()
                    connection.close()

                return Response(
                    200,
                    ClosingBody(chunks(), cleanup),
                    {"Content-Type": mime, "Content-Length": str(size)},
                )
            if mime != "application/json":
                return failure(502, "UPSTREAM_UNAVAILABLE")
            raw = bytearray()
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError()
                if connection.sock:
                    connection.sock.settimeout(left)
                chunk = response.read(min(65536, cap + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > cap:
                    return failure(502, "UPSTREAM_UNAVAILABLE")
            payload = strict_json(raw)
            if not isinstance(payload, dict):
                return failure(502, "UPSTREAM_UNAVAILABLE")
            if response.status != 200:
                error = payload.get("error", {})
                code = error.get("code") if isinstance(error, dict) else None
                code = code if code in ERRORS else "UPSTREAM_UNAVAILABLE"
                result = failure(
                    response.status
                    if response.status in (404, 409, 413, 415, 422, 429, 503)
                    else 502,
                    code,
                )
                if code == "PREVIEW_BUSY":
                    result.headers["Retry-After"] = "2"
                return result
            if payload.get("status") != "succeeded" or not isinstance(
                payload.get("data"), dict
            ):
                return failure(502, "UPSTREAM_UNAVAILABLE")
            return Response(
                200,
                encoded(project_envelope(payload)),
                {"Content-Type": "application/json; charset=utf-8"},
            )
        except TimeoutError:
            return failure(504, "UPSTREAM_TIMEOUT")
        except (OSError, ValueError, TypeError, KeyError, HTTPException):
            return (
                failure(504, "UPSTREAM_TIMEOUT")
                if timed_out.is_set()
                else failure(502, "UPSTREAM_UNAVAILABLE")
            )
        finally:
            if not streamed:
                watchdog.cancel()
                if response:
                    response.close()
                connection.close()


GARMENT_FIELDS = {
    "id",
    "name",
    "category",
    "roles",
    "availability",
    "revision",
    "state",
    "created_at",
    "deleted_at",
    "restore_until",
    "identity_fields",
    "annotation_fields",
    "annotation_schema_version",
    "annotation_review_state",
    "product_reference",
    "product_evidence_summary",
    "product_evidence",
    "recommendation_eligible",
    "missing_requirements",
    "image_access_status",
}
DATA_FIELDS = {
    "garment",
    "items",
    "next_cursor",
    "has_more",
    "catalog_revision",
    "server_now",
    "receipt_id",
    "operation",
    "garment_id",
    "base_revision",
    "revision",
    "state",
    "created_at",
    "deleted_at",
    "restore_until",
    "changed_fields",
}


def project_envelope(payload):
    """Discard unknown transport metadata; construct image URLs from validated IDs."""

    def garment(row):
        gid = row.get("id")
        if not isinstance(gid, str) or not re.fullmatch(UUID, gid):
            raise ValueError("Invalid garment")
        result = {k: v for k, v in row.items() if k in GARMENT_FIELDS}
        result["image_references"] = []
        if row.get("state") == "active":
            for ref in row.get("image_references", []):
                aid = ref.get("asset_id")
                if not isinstance(aid, str) or not re.fullmatch(UUID, aid):
                    raise ValueError("Invalid asset")
                result["image_references"].append(
                    {
                        "asset_id": aid,
                        "mime": ref.get("mime"),
                        "width": ref.get("width"),
                        "height": ref.get("height"),
                        "preview_url": f"{PREFIX}/images/{gid}/{aid}/preview",
                        "content_url": f"{PREFIX}/images/{gid}/{aid}/content",
                    }
                )
        return result

    data = {k: v for k, v in payload["data"].items() if k in DATA_FIELDS}
    if "garment" in data:
        data["garment"] = garment(data["garment"])
    if "items" in data:
        data["items"] = [
            garment(row) if isinstance(row, dict) else row for row in data["items"]
        ]
    return {
        "schema_version": "wardrobe/1.0",
        "status": "succeeded",
        "request_id": str(uuid.uuid4()),
        "data": data,
    }


class AdminApp:
    def __init__(self, username, upstream, ui=None, clock=time.time, csrf_secret=None):
        if not isinstance(username, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}", username
        ):
            raise ValueError("Invalid operator binding")
        self.username, self.upstream = username, upstream
        self.ui = Path(ui) if ui else Path(__file__).with_name("ui")
        self.clock = clock
        self.secret = csrf_secret or secrets.token_bytes(32)

    def token(self, username):
        raw = encoded([username, int(self.clock()) + 900, secrets.token_hex(24)])
        value = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        signature = hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()
        return value + "." + signature

    def csrf_valid(self, token, username):
        try:
            value, signature = token.split(".")
            expected = hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return False
            user, expires, nonce = strict_json(
                base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            )
            return (
                user == username
                and type(expires) is int
                and int(self.clock()) < expires <= int(self.clock()) + 900
                and isinstance(nonce, str)
                and bool(re.fullmatch("[0-9a-f]{48}", nonce))
            )
        except (ValueError, TypeError, UnicodeError):
            return False

    def handle(self, method, target, header_pairs, body=b""):
        headers = {}
        for name, value in header_pairs:
            key = name.lower()
            if key in headers:
                if key == "origin":
                    return failure(403, "CSRF_DENIED")
                if key == "x-csrf-token":
                    return failure(403, "CSRF_INVALID")
                return failure(422, "INVALID_ARGUMENT")
            headers[key] = value
        if headers.get("host") not in ("example.com", "example.com:443"):
            return failure(421, "INVALID_ARGUMENT")
        if (
            headers.get("x-wardrobe-admin-user") != self.username
            or "authorization" in headers
            or "proxy-authorization" in headers
        ):
            return failure(403, "SCOPE_DENIED")
        if any(
            key in headers
            for key in (
                "x-http-method-override",
                "x-method-override",
                "x-http-method",
                "transfer-encoding",
            )
        ):
            return failure(422, "INVALID_ARGUMENT")
        origin = headers.get("origin")
        write = method in ("POST", "PATCH")
        if (write and origin != ORIGIN) or (origin is not None and origin != ORIGIN):
            return failure(403, "CSRF_DENIED")
        fetch = headers.get("sec-fetch-site")
        if fetch is not None and fetch not in (
            ("same-origin",) if write else ("same-origin", "none")
        ):
            return failure(403, "CSRF_DENIED")
        if "referer" in headers:
            try:
                ref = urlsplit(headers["referer"])
                if f"{ref.scheme}://{ref.netloc}" != ORIGIN:
                    return failure(403, "CSRF_DENIED")
            except ValueError:
                return failure(403, "CSRF_DENIED")
        if "range" in headers:
            return failure(416, "RANGE_NOT_SUPPORTED")
        if len(body) > MAX_BODY:
            return failure(413, "REQUEST_TOO_LARGE")
        if (
            any(c in target for c in ("\x00", "\\", "#"))
            or ".." in target
            or len(target) > 8192
        ):
            return failure(404, "NOT_FOUND")
        try:
            parsed = urlsplit(target)
        except ValueError:
            return failure(404, "NOT_FOUND")
        path = parsed.path
        if parsed.scheme or parsed.netloc or "%" in path or "//" in path:
            return failure(404, "NOT_FOUND")
        static = {
            PREFIX: None,
            PREFIX + "/": "index.html",
            PREFIX + "/admin.js": "admin.js",
            PREFIX + "/admin.css": "admin.css",
        }
        if path in static:
            if method not in ("GET", "HEAD"):
                return failure(405, "METHOD_NOT_ALLOWED")
            if parsed.query:
                return failure(422, "INVALID_ARGUMENT")
            if static[path] is None:
                return Response(308, headers={"Location": PREFIX + "/"})
            mime = {
                "index.html": "text/html; charset=utf-8",
                "admin.js": "text/javascript; charset=utf-8",
                "admin.css": "text/css; charset=utf-8",
            }[static[path]]
            return Response(
                200, (self.ui / static[path]).read_bytes(), {"Content-Type": mime}
            )
        if path == PREFIX + "/api/session":
            if method != "GET":
                return failure(405, "METHOD_NOT_ALLOWED")
            if parsed.query:
                return failure(422, "INVALID_ARGUMENT")
            token = self.token(self.username)
            return Response(
                200,
                encoded({"csrf_token": token, "expires_in": 900}),
                {
                    "Content-Type": "application/json; charset=utf-8",
                    "Set-Cookie": f"{COOKIE}={token}; Secure; HttpOnly; SameSite=Strict; Path={PREFIX}/; Max-Age=900",
                },
            )
        match = re.fullmatch(
            PREFIX + r"/api/garments/(" + UUID + r")(/delete|/restore)?", path
        )
        image = re.fullmatch(
            PREFIX + r"/images/(" + UUID + r")/(" + UUID + r")/(preview|content)", path
        )
        if path in (PREFIX + "/api/garments", PREFIX + "/api/categories"):
            allowed = ("GET",)
            keys = {"state", "limit", "cursor"}
            if path.endswith("/garments"):
                keys |= {"category", "sort", "order"}
            upstream_path = "/v1/operator/" + path.rsplit("/", 1)[1]
        elif match:
            allowed = ("POST",) if match[2] else ("GET", "PATCH")
            keys = {"state"} if method == "GET" else set()
            upstream_path = "/v1/operator/garments/" + match[1] + (match[2] or "")
        elif image:
            allowed, keys = ("GET",), set()
            upstream_path = (
                f"/v1/operator/garments/{image[1]}/assets/{image[2]}/{image[3]}"
            )
        else:
            return failure(404, "NOT_FOUND")
        if method not in allowed:
            return failure(405, "METHOD_NOT_ALLOWED")
        try:
            pairs = parse_qsl(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
                encoding="utf-8",
                errors="strict",
                max_num_fields=8,
            )
            query = dict(pairs)
            if len(query) != len(pairs) or set(query) - keys:
                raise ValueError()
            for key, value in pairs:
                if (
                    key == "state"
                    and value not in ("active", "deleted")
                    or key == "sort"
                    and value != "created_at"
                    or key == "order"
                    and value not in ("asc", "desc")
                ):
                    raise ValueError()
                if key == "limit" and (
                    not re.fullmatch(r"[1-9][0-9]{0,2}", value) or int(value) > 100
                ):
                    raise ValueError()
                if (
                    key == "category"
                    and not 1 <= len(value) <= 100
                    or key == "cursor"
                    and not 1 <= len(value) <= 4096
                    or any(ord(c) < 32 for c in value)
                ):
                    raise ValueError()
        except (ValueError, UnicodeError):
            return failure(422, "INVALID_ARGUMENT")
        if pairs:
            upstream_path += "?" + urlencode(pairs)
        key = None
        if write:
            if (
                headers.get("content-type", "").split(";")[0].strip().lower()
                != "application/json"
            ):
                return failure(415, "UNSUPPORTED_MEDIA_TYPE")
            key = headers.get("idempotency-key")
            if not key or not re.fullmatch(r"[!-~]{1,128}", key):
                return failure(422, "IDEMPOTENCY_KEY_REQUIRED")
            try:
                payload = strict_json(body)
                if (
                    not isinstance(payload, dict)
                    or type(payload.get("base_revision")) is not int
                    or payload["base_revision"] < 1
                ):
                    raise ValueError()
                expected = (
                    {"base_revision", "patch"}
                    if method == "PATCH"
                    else {"base_revision"}
                )
                if set(payload) != expected:
                    raise ValueError()
                if method == "PATCH" and (
                    not isinstance(payload["patch"], dict)
                    or not payload["patch"]
                    or set(payload["patch"])
                    - {"name", "category", "roles", "availability", "annotation_fields"}
                ):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError):
                return failure(422, "INVALID_ARGUMENT")
            try:
                cookie = SimpleCookie()
                cookie.load(headers.get("cookie", ""))
                token = headers.get("x-csrf-token", "")
                # Duplicate cookie names are ambiguous even when a parser accepts them.
                if (
                    headers.get("cookie", "").count(COOKIE + "=") != 1
                    or COOKIE not in cookie
                    or not token
                    or not hmac.compare_digest(cookie[COOKIE].value, token)
                    or not self.csrf_valid(token, self.username)
                ):
                    return failure(403, "CSRF_INVALID")
            except (CookieError, ValueError, TypeError):
                return failure(403, "CSRF_INVALID")
        elif body:
            return failure(422, "INVALID_ARGUMENT")
        return self.upstream.request(
            method,
            upstream_path,
            body,
            key,
            image=bool(image),
            preview=bool(image and image[3] == "preview"),
        )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass  # Never log URI, username, submitted text or credentials.

    def send_error(self, code, message=None, explain=None):
        result = failure(
            code, "METHOD_NOT_ALLOWED" if code == 501 else "INVALID_ARGUMENT"
        )
        self.send_response(code)
        for name, value in result.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(result.body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(result.body)
        self.close_connection = True

    def handle_one_request(self):
        self.connection.settimeout(15)
        try:
            super().handle_one_request()
        except (OSError, ValueError):
            self.close_connection = True

    def dispatch(self):
        result = None
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) > 1 or lengths and (not lengths[0].isdigit()):
                result = failure(422, "INVALID_ARGUMENT")
            elif lengths and (len(lengths[0]) > 6 or int(lengths[0]) > MAX_BODY):
                result = failure(413, "REQUEST_TOO_LARGE")
            else:
                body = self.rfile.read(int(lengths[0])) if lengths else b""
                result = self.server.app.handle(
                    self.command, self.path, list(self.headers.items()), body
                )
            self.send_response(result.status)
            for name, value in result.headers.items():
                self.send_header(name, value)
            if (
                isinstance(result.body, bytes)
                and "Content-Length" not in result.headers
            ):
                self.send_header("Content-Length", str(len(result.body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if self.command != "HEAD":
                if isinstance(result.body, bytes):
                    self.wfile.write(result.body)
                else:
                    for chunk in result.body:
                        self.wfile.write(chunk)
        except (OSError, ValueError):
            self.close_connection = True
        finally:
            if result is not None and hasattr(result.body, "close"):
                result.body.close()

    do_GET = do_HEAD = do_POST = do_PATCH = do_PUT = do_DELETE = do_OPTIONS = dispatch


class Server(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, address, app, activated=None):
        self.app = app
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler, bind_and_activate=activated is None)
        if activated is not None:
            self.socket.close()
            self.socket = activated
            self.server_address = activated.getsockname()

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                headers = "".join(
                    name + ": " + value + "\r\n" for name, value in SECURITY.items()
                )
                request.sendall(
                    (
                        "HTTP/1.1 503 Service Unavailable\r\n"
                        + headers
                        + "Content-Length: 0\r\nConnection: close\r\n\r\n"
                    ).encode()
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def handle_error(self, request, client_address):
        pass  # No exception text or submitted material in logs.

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    if os.geteuid() == 0:
        raise SystemExit("Run under the dedicated nonprivileged service identity")
    if (
        os.environ.get("LISTEN_PID") != str(os.getpid())
        or os.environ.get("LISTEN_FDS") != "1"
    ):
        raise SystemExit("Exactly one systemd Unix socket is required")
    try:
        token = secure_json("/etc/wardrobe-admin/wardrobe-token.json")
        config = secure_json("/etc/wardrobe-admin/operator.json")
        if set(token) != {"token"} or set(config) != {
            "username",
            "upstream_host",
            "upstream_port",
        }:
            raise ValueError()
        upstream = Upstream(
            config["upstream_host"], config["upstream_port"], token["token"]
        )
        app = AdminApp(config["username"], upstream, "/var/lib/wardrobe-admin/ui")
        activated = socket.socket(fileno=3)
        if activated.family != socket.AF_UNIX or activated.type != socket.SOCK_STREAM:
            raise ValueError()
    except (ValueError, OSError, KeyError, TypeError):
        raise SystemExit("Invalid private service configuration")
    with Server(None, app, activated) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
