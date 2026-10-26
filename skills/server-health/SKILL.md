---
name: server-health
description: Verify a recorded service against current host and application evidence.
metadata:
  allowed_agents: "[developer, supervisor]"
  input: "service key and expected state"
  output: "health result, evidence, event or incident"
  tools: "[ssh_host_readonly, docker_readonly, company_api]"
  risks: "[secret_exposure, mistaking_container_uptime_for_application_health]"
  verification: "Check process or container, health endpoint, recent logs and expected behavior."
---

# Server health

Connect only with `ssh company-os-host`. Start read-only and never print environment
values. A running container is not sufficient evidence that the service works.
