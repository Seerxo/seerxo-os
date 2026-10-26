---
name: postgres-backup
description: Run or verify the existing Company OS backup and restore workflow.
metadata:
  allowed_agents: "[developer, supervisor]"
  input: "recorded backup target"
  output: "backup run with checksum, integrity and restore evidence"
  tools: "[companyctl, postgres_readonly]"
  risks: "[secret_exposure, untested_dump, restore_to_production]"
  verification: "Require integrity verification and a recorded restore test."
---

# PostgreSQL backup

Reuse `companyctl backup` and `companyctl restore-test`. Do not invent a second backup
path. Never restore over production or treat a dump file alone as success.

`coolify-db` and `config` reach R2 only as `age` ciphertext, and the decryption key is
never on the host. Their restore proof is founder-run on the laptop:
`AGE_KEY=<key file> bash scripts/restore-drill-encrypted.sh`. An agent cannot run it and
must not ask for the key.
