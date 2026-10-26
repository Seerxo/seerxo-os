---
name: company-state
description: Load the smallest authoritative Company OS context for one task.
metadata:
  allowed_agents: "[ceo, developer, supervisor, research]"
  input: "task key or founder brief request"
  output: "filtered task, project state, decisions, learnings, recent runs and required files"
  tools: "[company_api, companyctl]"
  risks: "[stale_snapshot, excessive_context]"
  verification: "Compare timestamps and keys with Company OS database output."
---

# Company state

Use `companyctl boot` or the internal API. Never replace current database state with chat
history or bulk-load the repository. If the API is unavailable, use `CURRENT_STATE.md`
and report its timestamp.
