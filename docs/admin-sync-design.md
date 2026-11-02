# Admin synchronization scope and design

## Maintainer request and product scope

The maintainer requested synchronization of the shipped changes and administration
function into this public repository on 2026-11-03. This ports the existing
approved single-user admin behavior; it does not expand its permission model.

Include catalog/category paging and time order, garment details and protected
images, confirmed single-item updates, 30-day soft deletion/restoration, revision
conflicts, idempotency and durable audit receipts. Include the private static UI,
Unix-socket proxy, example Nginx/systemd artifacts and original-image fallback fix.

Keep this repository's generic product-provider injection, configurable timezone,
`wardrobe` package, schema and public API/MCP compatibility. Do not import personal
approval transcripts, production inventories, images, credentials, proprietary
provider adapters or vendor-specific agent integrations.

The recent preview-busy/no-automatic-retry behavior is a known limitation, not an
implemented fix. No production service is contacted or deployed during this sync.

## Technical design

- Add a dedicated `human_adapter` operator grant, exact client identity and scopes;
  generic agent/human issuers cannot grant operator rights. Operator routes never
  become MCP tools. Grants are rechecked before reads, writes and replay.
- Reuse the existing SQLite schema and migration chain unchanged. Shared restore
  checks preserve the existing human-confirmation path. Writes remain atomic,
  revision-checked, idempotent and audited.
- Public readers keep their v1 cursor/id order. Admin readers use independent v2
  cursors bound to owner, client, channel, state and query; timestamps are additive.
- Ship the admin adapter under `ops/wardrobe-admin`, separate from the ASGI server
  and public static roots. Only Nginx connects to its restricted Unix socket. It
  reconstructs narrow upstream requests and retains exact-origin/signed-CSRF,
  request limits, timeout and no-store behavior.
- Public examples use `example.com`, `wardrobe.example.com`, a dedicated admin
  password file and service identity. Deployers replace these placeholders
  consistently; no dependency on the author's homepage or analytics service.
- Keep secrets outside Git and browsers. The UI never receives a bearer grant;
  the proxy cannot read the wardrobe database or shared password file.

## Verification contract

Run operator authorization/paging/write tests, existing public regressions
(including timezone and generic product providers), proxy/UI failure tests,
synthetic real-Nginx tests when available, build the wheel, and scan the complete
public tree and Git additions for personal domains/paths, inventories and secrets.
Record platform-specific gaps. Offline success does not prove another user's
production deployment, Basic Auth setup or third-party integration.
