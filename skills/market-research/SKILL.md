---
name: market-research
description: Answer one market, competitor or platform question with current evidence.
metadata:
  allowed_agents: "[research]"
  input: "explicit question, business unit and decision to support"
  output: "method, sources, evidence, findings, uncertainty and recommendation"
  tools: "[browser, web_search, company_api]"
  risks: "[stale_rules, unsupported_inference, scope_drift]"
  verification: "Sources directly support claims and include checked dates for changing rules."
---

# Market research

Prefer primary and authoritative sources. Keep facts, inference and recommendation
separate. Persist important findings; do not modify production or repositories.
