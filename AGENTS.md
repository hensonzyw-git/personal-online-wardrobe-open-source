# Contributor and agent rules

This is a single-user wardrobe API / MCP reference implementation.

Before significant feature, permission or architecture changes, obtain maintainer
approval of a scoped PRD, then a technical design. Inspect current changes before
editing. Do not alter unrelated work.

Never commit credentials, private wardrobe records, production configuration,
real garment images, or raw logs. Use synthetic fixtures only.

Keep authorization, idempotency, audit and external-result evidence. Test failure
and concurrency paths, not only happy paths. Do not weaken hash or authorization
assertions to pass a test.

Run relevant offline tests and `git diff --check`. Report exact verification limits.
Do not connect production services, call paid models, push or deploy without
explicit authorization.
