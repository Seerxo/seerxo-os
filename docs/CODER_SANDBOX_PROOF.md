# C2 — offline disposable sandbox proof

This is a proof harness, not an enabled coder runtime. Source base: `a1c7665`
(PR #55 lease/worktree/fingerprint foundation). Stage B is unchanged; no model,
credential handoff, egress proxy, worker wiring, PR push, merge or deploy is added.

Run on an approved Docker host with an **audited image already present locally**:

```bash
python3 scripts/check-coder-sandbox.py --image sha256:<full-local-image-id>
```

The harness creates only an empty disposable workspace and a synthetic adjacent
host canary. It never mounts a repository, mirror, subscription login, production
directory or Docker socket. It accepts immutable local image IDs only and never
pulls an image. A Docker image can contain baked-in files: inspecting its build
provenance for credentials remains mandatory. Passing this probe does not certify
arbitrary image contents or prove a kernel/container-runtime escape impossible.

The fixed Python probe writes one fixture file and checks UID 10001, zero effective
capabilities, no-new-privileges, read-only root, unavailable sensitive paths,
absence of named control/model credentials in the environment, and denied outbound
socket access. The controller additionally verifies the actual Docker profile:
network `none`, private PID/IPC namespaces, one writable fixture mount only,
128 MiB, 0.25 CPU and 32 PIDs. Missing/unknown/false results fail closed.

A second container intentionally sleeps beyond a two-second controller-command
timeout. Inspection must prove the fixture is running before cleanup. This bounds
the Docker command and proves cleanup of its detached fixture; it is not proof of
a future model/task process-runtime deadline.
Both UUID-named fixtures are forcibly removed in a finally block; successful
Docker listing must then prove their absence. No production container is touched.
Raw container output is suppressed on failure. Unexpected report fields are
rejected. The JSON report contains booleans,
not environment values, filesystem contents or model messages.

## Evidence from 2026-10-05

An explicit disposable run on `<production-host>` used the already staged worker
image `sha256:31c9be4181c795d4967b165d70ad6e01c40720c8680a063968336b71ac1229b4`
(source `0471ae0`); it was not deployed as the production worker. The initial probe
failed on inaccessible `/root/.ssh`; using a readability check corrected the probe
without widening permissions. Isolation, runtime-profile, timeout and cleanup
checks passed. Production API/worker stayed on their previous images.

## Remaining enablement gates

This proves only the offline fixture boundary. It does **not** prove the entire
Stage C golden suite. Still required: a separately reviewed coder image, controlled
provider egress and subscription credential boundary; command/capability enforcement;
safe source export from a worktree without exposing mirror control metadata;
governance-file restrictions; lease recovery integration; independent provider
review bound to the exact diff; control-plane-only push and bounded CI repair.

Do not mount a worktree's external `.git` mirror read-write into a coder merely to
make Git commands work. The control plane owns Git metadata and remote writes.
Do not enable `ENABLE_AUTONOMOUS_CODE_WRITE` from this report.

Rollback: remove the unused proof harness; there is no migration, service, flag,
cron, permission or production configuration to reverse. New autonomous spend: $0.

Docker isolation flags follow the existing Stage C design and
[Docker run reference](https://docs.docker.com/reference/cli/docker/container/run/).
Unit regressions run in the existing pytest merge gate. Docker proof is explicit,
not silently skipped or represented as a CI golden-security pass.
