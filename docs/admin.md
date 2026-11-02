# Private wardrobe administration

This optional Chinese-language UI is for one human owner. The backend is packaged
with `open-wardrobe`; its separate proxy/UI and Linux deployment examples live in
[`ops/wardrobe-admin`](../ops/wardrobe-admin/README.md). There is no hosted admin
service or default password. This synchronization does not deploy a service.

## Capabilities and authority

- Browse active/deleted garments by category with paging and `sort=created_at&order=desc`
  order (default), or ascending order; details include record revision and times.
- Load authenticated image previews, with an explicit original-image button in
  details. Neither data storage paths nor upstream bearer credentials reach the UI.
- Confirm a single garment's edits, soft deletion or restoration in the UI.
  Restore requires the 30-day deadline and original-image checks to pass again.
- Preserve idempotency keys across uncertain writes, show revision conflicts for
  fresh human confirmation, and persist audit/write receipts atomically.
- Read generic catalog product evidence through an allowlist. Raw provider
  responses, source paths and proprietary evidence formats are not exposed.

The dedicated client is exactly `wardrobe-admin`, channel `human_adapter`, scopes
`wardrobe.read` and `wardrobe.operator_write`. Generic agent credentials and
`grant-client` cannot acquire these operator rights. Operator routes are HTTP
only and are not registered as MCP tools. The browser authenticates through
Nginx Basic Auth over TLS; a restricted local proxy adds the dedicated grant.
Protect the proxy's Unix socket: direct socket access is part of the trusted
human-adapter boundary. Do not expose the socket/proxy as a public TCP listener.

## Issue a grant

Initialize the API and its owner first using the normal setup. `grant-operator`
refuses to create an owner. Run this against your own initialized data root:

```sh
mkdir -p .local/credentials && chmod 700 .local/credentials
uv run wardrobectl --data-root .local/wardrobe grant-operator \
  --owner-id 00000000-0000-0000-0000-000000000001 \
  --token-file .local/credentials/admin.token
```

The command creates a new mode-600 raw token file exclusively; it never prints
the token or overwrites an existing file. Default lifetime is 86,400 seconds;
`--ttl-seconds` allows 1–604,800 seconds. Configure rotation before expiry. Expired
or revoked grants fail closed; the proxy never falls back to agent credentials.
To revoke a grant, use `wardrobectl --data-root ... revoke-client --token-file ...`.
Do not pass token values on command lines or put them in JavaScript or Git.

The proxy's protected configuration expects a JSON object with only `token`,
not the raw CLI file. Convert it on the server without printing its value (use
your actual protected source/target locations):

```python
import json
import os
from pathlib import Path

source = Path(".local/credentials/admin.token")
target = Path("/etc/wardrobe-admin/wardrobe-token.json")
fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
with os.fdopen(fd, "w") as output:
    json.dump({"token": source.read_text().strip()}, output)
```

The server administrator must assign the directory and files to the dedicated
`wardrobe-admin` UID. See deployment prerequisites below for exact permissions.
Keep the original raw credential in protected storage for revocation. Rotation
means issuing a new grant/file, atomically replacing the proxy JSON, restarting
the proxy to load it, verifying access, and revoking the old grant. Never restore
an old database just to undo an admin release.

## Deployment examples

These artifacts target Debian/Ubuntu-style Nginx + systemd and Python **3.12+**.
They require an existing TLS vhost and a loopback-only wardrobe API. Before use:

1. Replace `example.com` / `wardrobe.example.com` consistently in `proxy.py`,
   `install.py` and Nginx snippets. The apex is the browser origin; the wardrobe
   host is the upstream `Host` header and must be allowed by the API settings.
   Keep exact Origin/Host checks aligned; do not remove them.
2. Provision a non-login `wardrobe-admin` user/group. Nginx is assumed to run as
   `www-data`; adapt socket group/preflight on other distributions. The service's
   `/usr/bin/python3` must meet the Python requirement, or update `ExecStart`.
3. Create `/etc/wardrobe-admin/` as that UID, mode 700; create `operator.json`
   and `wardrobe-token.json` as the same UID, mode 600, without symlinks.
4. `operator.json` has exactly `username`, `upstream_host` and `upstream_port`.
   Use your Basic Auth username, `127.0.0.1` (or `::1`) and the verified API port.
   It contains no password. Example: `{"username":"admin","upstream_host":
   "127.0.0.1","upstream_port":8000}`.
5. Create the dedicated `/etc/nginx/wardrobe-admin.htpasswd` using an interactive
   password tool (for example `sudo htpasswd -c /etc/nginx/wardrobe-admin.htpasswd admin`
   on first creation). Set owner/group `root:www-data`, mode 640. Never use `-c`
   when updating an existing password file. Choose a hash supported by your host's
   Nginx/crypt implementation. The proxy cannot read this file.
6. Ensure your Nginx `http` context loads `conf.d/*.conf`. The installer expects
   the TLS site at `sites-available/example.com` and `sites-enabled/example.com`,
   containing one `root /var/www/example.com/current;` anchor. Adjust these exact
   paths/anchor before planning if your layout differs. Both independent config
   copies are updated; a symlink target is changed once. Root inaccessibility in
   the service must cover your actual wardrobe data directory.
7. Review the read-only plan on the intended host, then apply only after deciding
   to deploy. Back up and inspect shared Nginx configuration first.

```sh
python3 ops/wardrobe-admin/install.py
# Explicit deployment operation on your own host:
sudo python3 ops/wardrobe-admin/install.py --apply
```

The installer stages only its allowlisted artifacts, validates Nginx, enables
socket activation and reloads Nginx. It retains backups and rolls back its own
file/unit changes on failure; concurrent changes prevent automatic rollback.
It does not create credentials, issue grants, migrate a database or deploy any
other web content. The UI is served privately by the proxy, never by a public
static-file alias. Open `https://example.com/wardrobe-admin/` after replacing the
placeholder. A path on an existing host needs no separate DNS record; a new host
name needs your own DNS and TLS setup.

Browser requests enforce exact origin and signed CSRF for writes, 64 KiB JSON
limits, no-store/security headers and narrow routes. Upstream requests exclude
browser cookies and Basic Auth. Images are garment-bound and authenticated;
range requests and arbitrary upstream URLs are rejected. Access logs contain
only route class, method, status, request ID and duration.

For password rotation, atomically update the admin htpasswd file, run `nginx -t`
and reload Nginx, then check that the old password fails and the new one works.
For emergency removal, review `install.py --remove`, then use `--remove --apply`.
This removes only the admin vhost includes and works without proxy credentials.
Verify the entire admin prefix returns 404 before disabling its socket/service.
Business edits remain persisted; code rollback does not undo them.

## HTTP contract

All responses under `/v1/operator/` are authenticated and `no-store`. A browser
`Origin` header is rejected there; only the trusted adapter sends the bearer.
Normal API/MCP v1 paging retains its existing ID order and cursor format. The
operator uses separate v2 cursors bound to owner, client, channel and query.
No database migration is added. The adapter maps the following API surface to
`/wardrobe-admin/api/` and protected `/wardrobe-admin/images/` paths:

| Method | API path | Input |
| --- | --- | --- |
| GET | `/v1/operator/garments` | `state`, `category`, `sort`, `order`, `limit`, `cursor` |
| GET | `/v1/operator/categories` | `state`, `limit`, `cursor` |
| GET | `/v1/operator/garments/{id}` | `state` |
| PATCH | `/v1/operator/garments/{id}` | `base_revision`, allowlisted `patch` |
| POST | `/v1/operator/garments/{id}/delete` | `base_revision` |
| POST | `/v1/operator/garments/{id}/restore` | `base_revision` |
| GET | `/v1/operator/garments/{id}/assets/{asset_id}/preview` | No query |
| GET | `/v1/operator/garments/{id}/assets/{asset_id}/content` | No query |

Writes require `Idempotency-Key`. A successful duplicate returns its saved
receipt only after checking current authority. Invalid/reused keys, stale
revisions/cursors, unavailable assets, restore deadlines and revoked grants have
explicit failures. The UI shows timestamps in UTC. `WARDROBE_TIMEZONE` continues
to configure recommendation defaults; it does not alter the admin display zone.

## Known limits and verification

There is no multi-user administration, bulk mutation, upload UI, automatic grant
renewal or built-in remote product provider. Thumbnail generation uses a global
nonblocking lock: simultaneous cold previews may return `PREVIEW_BUSY`/503.
The UI currently displays a static unavailable message and has no automatic
preview retry. Reload after generation, or use the original-image button in
details. The original button restores the image element even after preview
failure, and remains available after an original-image load failure.

Run offline API tests with `uv run pytest`, proxy tests with
`uv run python tests/wardrobe_admin_test.py`, and Node UI tests with
`node --test tests/wardrobe-admin-ui.test.mjs`. The synthetic Nginx check is
`uv run python tests/wardrobe_admin_nginx.py /tmp/wardrobe-admin-nginx-test`;
choose a fresh temporary directory. It verifies actual authentication, headers,
rotation, limits and removal with a fake upstream. It does not prove real API
writes, a browser journey, systemd sandboxing or your production TLS setup.
On macOS only this disposable test uses APR1 because SHA-512 crypt support differs;
Linux uses a synthetic SHA-512 hash. Production password storage is host-managed.
