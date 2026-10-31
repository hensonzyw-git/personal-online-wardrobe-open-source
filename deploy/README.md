# Deploy templates (examples only)

These systemd / nginx / certbot files are **examples**. They are not a turnkey
production playbook and do not describe any real host, account, or domain.

Replace placeholders:

- Host: `wardrobe.example.com`
- Install root: `/opt/open-wardrobe`
- Data root: `/var/lib/open-wardrobe`
- Runtime env: `/etc/open-wardrobe/runtime.env` (mode 600, not in Git)

Keep the ASGI service on loopback (e.g. `127.0.0.1:8830`) behind a reverse proxy.
Do not expose garment images as public static files. Set `WARDROBE_ALLOWED_HOSTS`
explicitly. Leave `WARDROBE_AGENT_ENABLED=false` unless you intentionally enable it.

This open-source snapshot does not include production secrets, backups, or
migration runbooks for a live database.
