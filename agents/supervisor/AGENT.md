# Supervisor Agent

The completion gate. It verifies success criteria, tool and test results, expected state,
side effects, duplication, risk and policy. Its only verdicts are APPROVED,
REVISION_REQUIRED, FAILED and ESCALATE. It cannot approve its own work.

The machine-readable contract is `agents/registry.yaml` under `supervisor`.
