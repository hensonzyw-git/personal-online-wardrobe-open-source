# Open Wardrobe (Open Source)

**Language：** [简体中文](./README.md)

> Public snapshot prepared from a private single-user wardrobe service. This tree
> does **not** include the author's personal data, production credentials, or
> live deployment inventory. See [verification notes](docs/verification.md).

A **single-user** wardrobe API and MCP service: store garment metadata and
protected images, expose governed read/write tools to personal agents, and
generate weather-aware outfit recommendations with human confirmation for
destructive changes.

`Single-user` · `FastAPI + MCP` · `SQLite` · `Human-in-the-loop writes`

> [!IMPORTANT]
> **This repository publishes code and architecture, not a ready-to-run hosted
> product.** You must supply your own data root, credentials, hosts, and any
> third-party API keys. Do not expect the author's wardrobe contents to appear
> here.

## License

[MIT](./LICENSE) — Copyright (c) 2026 hensonzyw-git

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)

## Quick start (local, synthetic)

```sh
cp .env.example .local/runtime.env   # then edit; keep mode 600; do not commit
uv sync --locked
uv run wardrobectl --data-root .local/wardrobe init-db
mkdir -p .local/credentials && chmod 700 .local/credentials
uv run wardrobectl --data-root .local/wardrobe grant-client \
  --owner-id 00000000-0000-0000-0000-000000000001 \
  --client-id local-agent \
  --scope wardrobe.read --scope wardrobe.create \
  --scope wardrobe.propose_change --scope wardrobe.recommend \
  --scope wardrobe.feedback --scope wardrobe.annotate \
  --token-file .local/credentials/local-agent.token
uv run wardrobectl --data-root .local/wardrobe serve --port 8000
```

The server listens on loopback by default. Use
`Authorization: Bearer …` for API, MCP (`/mcp`), and image routes.
Health probe: `GET /healthz` (no private content).

## Tests

```sh
uv sync --locked --group dev
uv run pytest
```

Use synthetic fixtures only. Do not point tests at production data.

## Configuration

See [`.env.example`](./.env.example). Important flags:

| Variable | Notes |
| --- | --- |
| `WARDROBE_DATA_ROOT` | SQLite + asset storage |
| `WARDROBE_ALLOWED_HOSTS` | Required for non-local Host headers |
| `WARDROBE_AGENT_ENABLED` | Default `false` |
| `WARDROBE_MODEL_CREDENTIALS_FILE` | Optional; JSON `{"api_key":"..."}` mode 600 |
| `WARDROBE_TIMEZONE` | Default `UTC`; validated IANA zone for locations without an explicit timezone (for example `America/New_York`) |

`WARDROBE_TIMEZONE` applies when a recommendation location omits `timezone`;
an explicit location timezone takes precedence. Export it into the server's
environment (the `.env.example` file is a template, not automatically loaded).

Product lookup has no built-in remote provider. Local catalog imports and product
evidence remain supported; custom providers can supply generic product drafts
through `create_app(product_provider=...)` with the existing review and publication checks.

## What is in / out of scope

**In this snapshot**

- FastAPI HTTP API and MCP tools for garments, assets, change proposals,
  recommendations, feedback/history, and optional product drafts
- Alembic migrations shipped with the package
- Example systemd / nginx templates under `deploy/`
- Offline unit/integration tests with synthetic data

**Out of this snapshot**

- Real images, catalogs, backups, or databases
- Production nginx hostnames, SSH inventory, or ECS runbooks
- Personal PRD / gate / review / acceptance transcripts from private development
- Guarantees about third-party integration wiring on your hosts

## Privacy

Clothes images and identifiers are personal data. This public tree was scrubbed
for private domains, account names, home paths, and operational evidence.
Please file an issue if you spot residual personal information.

## Contributing

See [AGENTS.md](./AGENTS.md). Significant changes need a scoped design and
maintainer approval. Never commit secrets.
