# Optional private admin adapter

See [setup, API, rotation and limitations](../../docs/admin.md) and the
[synchronization scope/design](../../docs/admin-sync-design.md).

- `proxy.py`: standard-library Unix-socket server; reconstructs narrow requests
  to a loopback API. Stores no wardrobe database or browser bearer credentials.
- `ui/`: private Chinese-language UI, served only by the authenticated adapter;
  includes the original-image fallback fix. Times display in UTC.
- `nginx-*.conf`: exact admin prefix, dedicated Basic Auth file, CSRF/security
  headers, redacted logging and limits. Hostnames are reserved examples.
- `wardrobe-admin.socket` / `.service`: dedicated UID, restricted socket and
  systemd sandbox. Adapt Python path, Nginx group and inaccessible data path.
- `install.py`: read-only installation/removal plan by default. `--apply` is an
  explicit host operation; it never creates secrets, grants or database schemas.

Do not run the templates unchanged on a real host. Follow the prerequisites in
the guide, replace both example hostnames consistently, and review the plan.
