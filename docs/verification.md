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
