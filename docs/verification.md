# Verification notes

This repository is a cleaned public snapshot of a private single-user wardrobe
service. It includes application code, migrations, synthetic tests, and example
deploy templates.

**Not included**

- Real garment images, catalogs, or databases
- Production credentials, tokens, certificates, or runtime env files
- Personal approval gates, review transcripts, or live acceptance evidence
- Author home paths, private domains, or server inventory

**What was verified for this snapshot**

- Offline test suite run on the cleaned tree (see evidence directory produced
  during packaging; results are recorded outside this Git tree until published)
- Secret / PII rescans after redaction

Passing offline tests does not prove a production deployment or third-party
integration works for your environment.

## Admin synchronization — 2026-11-03

The public candidate was checked locally on macOS with synthetic fixtures only:

- Full pytest collection: **896 tests; 895 passed, 1 skipped**. The existing skip
  is the Linux-only preview-worker resource-limit roundtrip; it requires Linux.
  This includes the new operator authorization, paging, read/write, concurrency,
  idempotency, audit and old-v1-cursor regression tests, plus existing generic
  product-provider, recommendation and configurable-timezone tests.
- Standalone proxy/installer suite: **27 passed**, including real Unix sockets,
  CSRF, bounded streaming, private file handling and rollback behavior.
- Actual UI module in the Node DOM harness: **3 passed**, including preview
  failure followed by original-image display/retry. This is not browser layout QA.
- Real local Nginx against a synthetic upstream: authentication, response headers,
  password rotation, rate rejection and admin-prefix removal passed. No production
  upstream or real wardrobe write was exercised by that check.
- Ruff passed for application code and the new Python adapters/tests.
  `git diff --check` passed. The wheel and source distribution built successfully;
  the wheel contains the operator backend and the unchanged ten migrations, and
  the source distribution includes the admin UI.
- The complete versioned/public candidate was scanned for private domains, author
  home paths, private package/adapters, credential signatures, and data/image/key
  files; no matches were found. Examples use reserved domains and synthetic data.

Proxy/installer checks and the real-Nginx fixture were rerun after formatting the
new operational scripts. No production deployment, external model call, live
wardrobe import, systemd sandbox validation or human acceptance is claimed.
The preview-busy/automatic-retry limitation remains documented in
[the admin guide](admin.md#known-limits-and-verification).
