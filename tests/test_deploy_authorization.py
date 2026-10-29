"""Merging is not deploying. The legacy workflow only checks readiness; host changes use the signed gateway.
Driver authorization, migration and recovery guards remain enforced independently of the retired SSH job.

Incident (2026-10-08): a push to main auto-deployed #168 and ran `alembic upgrade c7d2e8a4f1b3 ->
b4f8a2d6c1e9` although automatic deploys were meant to refuse schema changes: the workflow set
COMPANY_OS_AUTOMATIC_DEPLOY=true but the ssh/sudo hop never carried it to the host.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
DEPLOY = ROOT / "scripts" / "deploy-company-os.sh"
SOURCE = DEPLOY.read_text()
REMOTE = SOURCE[SOURCE.index("<<'REMOTE'\n"):SOURCE.index("\nREMOTE\n")]
SPEC = importlib.util.spec_from_file_location("deploy_authorization", ROOT / "scripts" / "deploy-authorization.py")
authz = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(authz)
FOUNDER = "semihbugrasezer"
SHA = "a" * 40
ON = CI.get("on", CI.get(True))          # YAML 1.1 reads the bare key `on` as True


# --------------------------------------------------------------------------- workflow: push never deploys

def test_a_push_or_pull_request_can_never_reach_a_deploy_job():
    for name in ("deploy-authorization", "deploy-provenance"):
        cond = CI["jobs"][name]["if"]
        assert "github.event_name == 'workflow_dispatch'" in cond, name
        assert "'push'" not in cond and "pull_request" not in cond, name
    assert set(ON) == {"workflow_dispatch"}                                   # PR and main CI run on CircleCI
    assert CI["jobs"]["ci-gate"]["needs"] == ["deploy-authorization"]          # no code runs before the founder's dispatch


def test_the_readiness_chain_requires_authorization_then_tests_then_provenance_and_deploys_nothing():
    assert set(CI["jobs"]["deploy-provenance"]["needs"]) == {"ci-gate", "deploy-authorization"}
    assert set(CI["jobs"]) == {"ci-gate", "deploy-authorization", "deploy-provenance"}   # the SSH deploy job is gone


def test_dispatch_inputs_bind_one_exact_commit_and_a_separate_migration_authorization():
    inputs = ON["workflow_dispatch"]["inputs"]
    assert inputs["sha"]["required"] is True
    assert inputs["allow_migration_to"]["required"] is False and inputs["allow_migration_to"]["default"] == ""
    gate = CI["jobs"]["ci-gate"]
    checkout = next(s for s in gate["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))
    assert "ref" not in checkout["with"]                                     # main's own gate code, never the payload's
    step = next(s for s in gate["steps"] if "ci-status-gate.py" in s.get("run", ""))
    assert step["env"]["DEPLOY_SHA"] == "${{ inputs.sha }}"                  # CircleCI must have passed on the commit
    assert 'commits/$DEPLOY_SHA/statuses' in step["run"] and "$GITHUB_SHA" not in step["run"]
    assert CI["permissions"]["statuses"] == "read"
    # The Sengi frontend workflow's push run on main for the exact commit is the frontend evidence.
    assert "actions/runs?head_sha=$DEPLOY_SHA&event=push&branch=main" in step["run"]
    assert '--runs "$runs"' in step["run"]
    assert CI["permissions"]["actions"] == "read"
    assert set(CI["permissions"].values()) == {"read"}                       # no write scope added


def test_the_required_frontend_workflow_runs_on_pr_and_main_without_secrets_or_write_access():
    policy = yaml.safe_load((ROOT / "agents/repository_guardian.yaml").read_text())
    for path in policy["workflows"]:
        text = (ROOT / path).read_text()
        workflow = yaml.safe_load(text)
        on = workflow.get("on", workflow.get(True))
        assert on == {"pull_request": {"branches": ["main"]}, "push": {"branches": ["main"]}}, path
        assert workflow["permissions"] == {"contents": "read"}, path
        assert "secrets." not in text and "sez-deploy" not in text, path
        assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_readiness_jobs_use_a_hosted_runner_without_deploy_secrets():
    for name, job in CI["jobs"].items():
        assert job["runs-on"] == "ubuntu-24.04", name                                # never a persistent runner
    assert "secrets." not in (ROOT / ".github/workflows/ci.yml").read_text()          # no key reaches the runner


def test_provenance_checks_the_dispatched_commit_not_the_branch_head():
    run = next(s for s in CI["jobs"]["deploy-provenance"]["steps"] if s.get("id") == "provenance")["run"]
    assert "$GITHUB_SHA" not in run and '"$DEPLOY_SHA"' in run


@pytest.mark.parametrize("valid", [True, False])
def test_readiness_job_fails_when_founder_pr_provenance_is_rejected(tmp_path, valid):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    facts = tmp_path / "pr.json"
    facts.write_text(json.dumps({"state": "closed", "merged": True, "base": {"ref": "main"},
                                "merged_by": {"login": FOUNDER if valid else "someone-else"}, "merge_commit_sha": SHA}))
    gh = bin_dir / "gh"
    gh.write_text('#!/usr/bin/env bash\ncase "$*" in\n'
                  '  *"/commits/"*) echo 1 ;;\n'
                  '  *"/pulls/1"*) cat "$PROVENANCE_FACTS" ;;\n'
                  '  *) exit 1 ;;\nesac\n')
    gh.chmod(0o755)
    (bin_dir / "python").symlink_to(sys.executable)
    output = tmp_path / "outputs"
    run = next(s["run"] for s in CI["jobs"]["deploy-provenance"]["steps"] if s.get("id") == "provenance")
    result = subprocess.run(["bash", "-c", run], cwd=ROOT, text=True, capture_output=True, timeout=30,
                            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                                 "GITHUB_REPOSITORY": "seerxo/os", "DEPLOY_SHA": SHA, "FOUNDER_LOGIN": FOUNDER,
                                 "GITHUB_OUTPUT": str(output), "PROVENANCE_FACTS": str(facts)})
    assert (result.returncode == 0) is valid, result.stdout + result.stderr
    assert output.read_text().strip() == f"allowed={str(valid).lower()}"


# --------------------------------------------------------------------------- scripts/deploy-authorization.py

def ok(**over):
    args = {"actor": FOUNDER, "triggering_actor": FOUNDER, "founder": FOUNDER, "sha": SHA,
            "allow_migration_to": "", "sha_on_main": True} | over
    return authz.evaluate(**args)


def test_the_founder_dispatching_a_main_commit_is_authorized():
    assert ok() == (True, "deploy authorized for " + SHA)
    assert ok(allow_migration_to="b4f8a2d6c1e9")[0] is True


@pytest.mark.parametrize("over, why", [
    ({"actor": "someone-else"}, "not the founder"),
    ({"triggering_actor": "someone-else"}, "not the founder"),          # a re-run by another account
    ({"actor": ""}, "not the founder"),
    ({"founder": ""}, "no founder configured"),
    ({"sha": "a" * 39}, "40 lowercase hex"), ({"sha": "A" * 40}, "40 lowercase hex"),
    ({"sha": "main"}, "40 lowercase hex"), ({"sha": ""}, "40 lowercase hex"),
    ({"sha": SHA + "\n"}, "40 lowercase hex"),
    ({"sha_on_main": False}, "not on main"),
    ({"sha_on_main": None}, "not on main"),                              # unknown is not yes
    ({"allow_migration_to": "head"}, "12 lowercase hex"),
    ({"allow_migration_to": "b4f8a2d6c1e9 "}, "12 lowercase hex"),
    ({"allow_migration_to": "B4F8A2D6C1E9"}, "12 lowercase hex"),
])
def test_anything_else_is_refused_with_a_reason(over, why):
    allowed, reason = ok(**over)
    assert allowed is False and why in reason


def test_the_cli_exits_nonzero_unless_authorized(tmp_path):
    run = lambda *a: subprocess.run(["python3", str(ROOT / "scripts/deploy-authorization.py"), *a],
                                    text=True, capture_output=True)
    good = run("--actor", FOUNDER, "--triggering-actor", FOUNDER, "--founder", FOUNDER, "--sha", SHA,
               "--allow-migration-to", "", "--sha-on-main", "yes")
    assert good.returncode == 0 and good.stdout.startswith("DEPLOY AUTHORIZED")
    bad = run("--actor", "x", "--triggering-actor", FOUNDER, "--founder", FOUNDER, "--sha", SHA,
              "--allow-migration-to", "", "--sha-on-main", "yes")
    assert bad.returncode == 1 and bad.stdout.startswith("DEPLOY REFUSED")
    unknown = run("--actor", FOUNDER, "--triggering-actor", FOUNDER, "--founder", FOUNDER, "--sha", SHA,
                  "--allow-migration-to", "", "--sha-on-main", "maybe")
    assert unknown.returncode == 1


# --------------------------------------------------------------------------- deploy script: flags reach the host

def run_local(tmp_path, env, apply=True):
    """Run the real deploy script's local side; ssh/scp are shims that record what would be sent."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    log = tmp_path / "remote.log"
    (shim / "ssh").write_text(f'#!/bin/sh\nshift\necho "$*" >> "{log}"\ncase "$*" in *"tail -n 1"*) printf "42\\nEXIT 0\\n";; *"cat /var/log/company-os-deploy.pid"*) echo 42;; esac\nexit 0\n')
    (shim / "scp").write_text(f'#!/bin/sh\necho "scp $*" >> "{log}"\nexit 0\n')
    for f in ("ssh", "scp"):
        (shim / f).chmod(0o755)
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True, capture_output=True).stdout.strip()
    base = {k: v for k, v in os.environ.items() if not k.startswith(("COMPANY_OS_", "ACK_"))}
    full = base | {"PATH": f"{shim}:{os.environ['PATH']}", "COMPANY_OS_DEPLOY_SHA": head,
                   "COMPANY_OS_DEPLOY_HOST": "fake-host"} | env
    out = subprocess.run(["bash", str(DEPLOY), *(["--apply"] if apply else [])], text=True, capture_output=True,
                         env=full, cwd=ROOT)
    return out, (log.read_text() if log.exists() else ""), head


def test_automatic_mode_and_migration_authorization_are_carried_across_ssh_and_sudo(tmp_path):
    out, sent, head = run_local(tmp_path, {"COMPANY_OS_AUTOMATIC_DEPLOY": "true",
                                           "COMPANY_OS_ALLOW_MIGRATION": "b4f8a2d6c1e9"})
    assert out.returncode == 0, out.stdout + out.stderr
    apply_line = next(l for l in sent.splitlines() if "APPLY=--apply" in l)
    assert apply_line.startswith("sudo ") and f"SHA={head}" in apply_line
    assert "AUTOMATIC=true" in apply_line and "ALLOW_MIGRATION=b4f8a2d6c1e9" in apply_line
    assert not any("APPLY= " in l for l in sent.splitlines())               # --apply ends in follow
    out, sent, _ = run_local(tmp_path / "ro", {"COMPANY_OS_AUTOMATIC_DEPLOY": "true"}, apply=False)
    read_only = next(l for l in sent.splitlines() if "APPLY= " in l)
    assert "AUTOMATIC=true" in read_only and "ALLOW_MIGRATION= " in read_only


def test_an_attended_deploy_is_explicitly_not_automatic(tmp_path):
    out, sent, _ = run_local(tmp_path, {})
    assert out.returncode == 0, out.stdout + out.stderr
    assert "AUTOMATIC=false" in next(l for l in sent.splitlines() if "APPLY=--apply" in l)


@pytest.mark.parametrize("env, why", [
    ({"COMPANY_OS_AUTOMATIC_DEPLOY": "yes"}, "COMPANY_OS_AUTOMATIC_DEPLOY must be true or false"),
    ({"COMPANY_OS_AUTOMATIC_DEPLOY": "TRUE"}, "COMPANY_OS_AUTOMATIC_DEPLOY must be true or false"),
    ({"COMPANY_OS_ALLOW_MIGRATION": "head"}, "COMPANY_OS_ALLOW_MIGRATION"),
    ({"COMPANY_OS_ALLOW_MIGRATION": "b4f8a2d6c1e9; rm -rf /"}, "COMPANY_OS_ALLOW_MIGRATION"),
    ({"COMPANY_OS_AUTOMATIC_DEPLOY": "true", "ACK_COMPOSE": "abc"}, "contradictory"),
    ({"COMPANY_OS_AUTOMATIC_DEPLOY": "true", "ACK_OVERRIDES": "/x"}, "contradictory"),
    ({"COMPANY_OS_AUTOMATIC_DEPLOY": "true", "COMPANY_OS_DEPLOY_SHA": ""}, "exact commit"),
])
def test_unknown_or_contradictory_modes_stop_before_anything_reaches_the_host(tmp_path, env, why):
    out, sent, _ = run_local(tmp_path, env)
    assert out.returncode != 0 and why in out.stdout + out.stderr
    assert sent == ""                                                        # no scp, no ssh


# --------------------------------------------------------------------------- host side: re-validated, fail closed

def remote_mode_block():
    start = REMOTE.index("\ncase \"${AUTOMATIC:-}\" in\n") + 1
    return REMOTE[start:REMOTE.index("# --- end deploy mode", start)]


@pytest.mark.parametrize("env, ok_", [
    ({"AUTOMATIC": "true"}, True), ({"AUTOMATIC": "false", "ALLOW_MIGRATION": "b4f8a2d6c1e9"}, True),
    ({}, False), ({"AUTOMATIC": ""}, False), ({"AUTOMATIC": "1"}, False),
    ({"AUTOMATIC": "false", "ALLOW_MIGRATION": "x"}, False),
    ({"AUTOMATIC": "true", "ACK_COMPOSE": "abc"}, False),
    # B-7: only the release host (staged RELEASE_DIR) may pass a well-formed ACK_COMPOSE in automatic mode.
    ({"AUTOMATIC": "true", "ACK_COMPOSE": "0123456789ab"}, False),
    ({"AUTOMATIC": "true", "ACK_COMPOSE": "0123456789ab", "RELEASE_DIR": "/run/company-os-deploy/x"}, True),
    ({"AUTOMATIC": "true", "ACK_COMPOSE": "0123456789AB", "RELEASE_DIR": "/run/company-os-deploy/x"}, False),
    ({"AUTOMATIC": "true", "ACK_COMPOSE": "0123456789ab0", "RELEASE_DIR": "/run/company-os-deploy/x"}, False),
    ({"AUTOMATIC": "true", "ACK_OVERRIDES": "/x", "RELEASE_DIR": "/run/company-os-deploy/x"}, False),
])
def test_the_host_rejects_a_missing_unknown_or_contradictory_mode(env, ok_):
    base = {k: v for k, v in os.environ.items()
            if k not in ("AUTOMATIC", "ALLOW_MIGRATION", "ACK_COMPOSE", "ACK_OVERRIDES", "RELEASE_DIR")}
    out = subprocess.run(["bash", "-c", "set -euo pipefail\n" + remote_mode_block() + "echo MODE-OK"],
                         text=True, capture_output=True, env=base | env)
    assert (out.returncode == 0 and "MODE-OK" in out.stdout) is ok_, out.stdout + out.stderr


def migration_gate(rev0, target, allow):
    start = REMOTE.index("\nmigration_authorized() {\n") + 1
    fn = REMOTE[start:REMOTE.index("\n}\n", start) + 3]
    return subprocess.run(["bash", "-c", fn + 'migration_authorized "$1" "$2" "$3"', "_", rev0, target, allow],
                          text=True, capture_output=True)


def alembic_head_gate(output):
    start = REMOTE.index("\nsingle_alembic_head() {\n") + 1
    fn = REMOTE[start:REMOTE.index("\n}\n", start) + 3]
    return subprocess.run(["bash", "-c", fn + 'single_alembic_head "$1"', "_", output],
                          text=True, capture_output=True)


def remote_function(name):
    start = REMOTE.index(f"\n{name}() {{") + 1
    return REMOTE[start:REMOTE.index("\n}\n", start) + 3]


@pytest.mark.parametrize("output, expected", [
    ("e7c3a54d21b0", "e7c3a54d21b0"),
    ("", None),
    ("e7c3a54d21b0\nabc123abc123", None),
    ("e7c3a54d21b0\n", None),
    ("not-a-revision", None),
])
def test_deployment_requires_exactly_one_valid_alembic_head(output, expected):
    result = alembic_head_gate(output)
    if expected is None:
        assert result.returncode != 0
    else:
        assert result.returncode == 0
        assert result.stdout == expected


def test_multihead_or_unknown_migration_plan_uses_fail_closed_restore_path():
    call = REMOTE.index('TARGET_REV=$(single_alembic_head "$HEADS")')
    refusal = REMOTE[call:REMOTE.index("\n}", call) + 2]
    assert 'abort_before_migration "expected exactly one valid Alembic head"' in refusal
    assert "phase ABORTED_RESTORED" not in refusal
    assert "alembic upgrade head" in REMOTE and REMOTE.index("alembic upgrade head") > call


@pytest.mark.parametrize("source_status, tag_status, expected_phase", [
    (0, 0, "ABORTED_RESTORED"),
    (1, 0, "ROLLBACK_FAILED"),
    (0, 1, "ROLLBACK_FAILED"),
])
def test_pre_migration_abort_never_claims_restored_when_restore_fails(tmp_path, source_status, tag_status, expected_phase):
    phases = tmp_path / "phases"
    fn = remote_function("abort_before_migration")
    script = fn + '''
B=/unused
restore_source() { return "$RESTORE_STATUS"; }
restore_tags() { return "$TAGS_STATUS"; }
phase() { printf '%s\\n' "$1" >> "$PHASES"; }
abort_before_migration "test refusal"
'''
    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                            env=os.environ | {"RESTORE_STATUS": str(source_status), "TAGS_STATUS": str(tag_status),
                                              "PHASES": str(phases)})
    recorded = phases.read_text().splitlines()
    assert result.returncode == 1
    assert recorded == [expected_phase]
    if expected_phase == "ROLLBACK_FAILED":
        assert "ROLLBACK FAILED" in result.stderr
        assert "manual recovery required" in result.stderr
        assert "previous files and image tags restored" not in result.stdout
    else:
        assert "previous files and image tags restored" in result.stdout


@pytest.mark.parametrize("phase_failure, expected_attempts, recorded", [
    ("first", ["ABORTED_RESTORED", "ROLLBACK_FAILED"], "ROLLBACK_FAILED"),
    ("all", ["ABORTED_RESTORED", "ROLLBACK_FAILED"], None),
])
def test_pre_migration_abort_reports_journal_write_failure_without_clean_success(
    tmp_path, phase_failure, expected_attempts, recorded
):
    attempts = tmp_path / "attempts"
    state = tmp_path / "state"
    fn = remote_function("abort_before_migration")
    script = fn + '''
B=/unused
restore_source() { return 0; }
restore_tags() { return 0; }
phase() {
  printf '%s\\n' "$1" >> "$ATTEMPTS"
  if [ "$PHASE_FAILURE" = all ] || { [ "$PHASE_FAILURE" = first ] && [ "$1" = ABORTED_RESTORED ]; }; then
    return 1
  fi
  printf '%s\\n' "$1" > "$STATE"
}
abort_before_migration "test refusal"
'''
    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                            env=os.environ | {"ATTEMPTS": str(attempts), "STATE": str(state),
                                              "PHASE_FAILURE": phase_failure})
    assert result.returncode == 1
    assert attempts.read_text().splitlines() == expected_attempts
    if recorded is None:
        assert not state.exists()
    else:
        assert state.read_text().strip() == recorded
    assert "ROLLBACK FAILED" in result.stderr
    assert "manual recovery required" in result.stderr
    assert "previous files and image tags restored" not in result.stdout


@pytest.mark.parametrize("restore_statuses", [(1, 0, 0), (0, 1, 0), (0, 0, 1)])
def test_host_cli_abort_requires_every_restore_to_succeed(tmp_path, restore_statuses):
    attempts = tmp_path / "attempts"
    state = tmp_path / "state"
    cli = tmp_path / "host-cli"
    cli.write_text("#!/bin/sh\nexit \"$CLI_STATUS\"\n")
    cli.chmod(0o755)
    fn = remote_function("abort_host_cli_install")
    script = fn + '''
B=/unused
REV0=aaaaaaaaaaaa
TARGET_REV=aaaaaaaaaaaa
HCLI="$CLI_PATH"
restore_source() { return "$SOURCE_STATUS"; }
restore_tags() { return "$TAGS_STATUS"; }
phase() {
  printf '%s\\n' "$1" >> "$ATTEMPTS"
  printf '%s\\n' "$1" > "$STATE"
}
abort_host_cli_install "test install failure"
'''
    source_status, cli_status, tags_status = restore_statuses
    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                            env=os.environ | {"ATTEMPTS": str(attempts), "STATE": str(state), "CLI_PATH": str(cli),
                                              "SOURCE_STATUS": str(source_status), "CLI_STATUS": str(cli_status),
                                              "TAGS_STATUS": str(tags_status)})
    assert result.returncode == 1
    assert attempts.read_text().splitlines() == ["ROLLBACK_FAILED"]
    assert "ROLLBACK FAILED" in result.stderr and "manual recovery required" in result.stderr
    assert "previous source, host CLI, and image tags restored" not in result.stdout


def test_host_cli_abort_does_not_claim_full_rollback_after_schema_migration(tmp_path):
    attempts = tmp_path / "attempts"
    state = tmp_path / "state"
    cli = tmp_path / "host-cli"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)
    fn = remote_function("abort_host_cli_install")
    script = fn + '''
B=/unused
REV0=aaaaaaaaaaaa
TARGET_REV=bbbbbbbbbbbb
HCLI="$CLI_PATH"
restore_source() { return 0; }
restore_tags() { return 0; }
phase() { printf '%s\\n' "$1" >> "$ATTEMPTS"; printf '%s\\n' "$1" > "$STATE"; }
abort_host_cli_install "test install failure"
'''
    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                            env=os.environ | {"ATTEMPTS": str(attempts), "STATE": str(state), "CLI_PATH": str(cli)})
    assert result.returncode == 1
    assert attempts.read_text().splitlines() == ["ROLLBACK_FAILED"]
    assert state.read_text().strip() == "ROLLBACK_FAILED"
    assert "database migration aaaaaaaaaaaa -> bbbbbbbbbbbb already succeeded" in result.stderr
    assert "no automatic database rollback was attempted" in result.stderr


def test_host_cli_abort_records_clean_state_only_when_no_migration_was_applied(tmp_path):
    attempts = tmp_path / "attempts"
    state = tmp_path / "state"
    cli = tmp_path / "host-cli"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)
    fn = remote_function("abort_host_cli_install")
    script = fn + '''
B=/unused
REV0=aaaaaaaaaaaa
TARGET_REV=aaaaaaaaaaaa
HCLI="$CLI_PATH"
restore_source() { return 0; }
restore_tags() { return 0; }
phase() { printf '%s\\n' "$1" >> "$ATTEMPTS"; printf '%s\\n' "$1" > "$STATE"; }
abort_host_cli_install "test install failure"
'''
    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                            env=os.environ | {"ATTEMPTS": str(attempts), "STATE": str(state), "CLI_PATH": str(cli)})
    assert result.returncode == 1
    assert attempts.read_text().splitlines() == ["ABORTED_RESTORED"]
    assert state.read_text().strip() == "ABORTED_RESTORED"
    assert "no schema migration was applied" in result.stdout


def test_restore_tags_stops_and_reports_a_failed_image_tag(tmp_path):
    calls = tmp_path / "calls"
    fn = remote_function("restore_tags")
    script = fn + '''
docker() {
  printf '%s\\n' "$*" >> "$CALLS"
  [ "$2" != "company-os-worker:rollback-s" ]
}
API_IMG=api:latest
WORKER_IMG=worker:latest
R_OLD=
E_OLD=
S=s
restore_tags
'''
    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                            env=os.environ | {"CALLS": str(calls)})
    assert result.returncode != 0
    assert calls.read_text().splitlines() == [
        "tag company-os-api:rollback-s api:latest",
        "tag company-os-worker:rollback-s worker:latest",
    ]


@pytest.mark.parametrize("failed_images", [
    (), ("api",), ("worker",), ("reasoner",), ("egress",),
    ("api", "worker"), ("api", "worker", "reasoner", "egress"),
])
def test_release_rollback_requires_every_image_tag_restored_before_a_clean_journal(tmp_path, failed_images):
    backup = tmp_path / "backup"
    backup.mkdir()
    for name in ("host-cli.sh", "release-health-gate.sh"):
        path = backup / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    (backup / "cli-files-before.sha256").write_text("fixture\n")
    (backup / "recovery-identity.py").write_text("# recovery identity has separate adversarial tests\n")
    calls = tmp_path / "calls"
    phase_fn = next(line for line in REMOTE.splitlines() if line.startswith("phase() {"))
    clean_fn = next(line for line in REMOTE.splitlines() if line.startswith("phase_is_clean() {"))
    script = "\n".join((phase_fn, remote_function("up_services"), remote_function("fence_live"),
                        remote_function("rollback_core"), remote_function("rollback_release"))) + '''
restore_source() { return 0; }
restore_cron() { return 0; }
compose_restore() { return 0; }  # this unit isolates image-tag restoration, not container startup
docker() {
  case "$1" in
    tag)
      printf '%s\\n' "$*" >> "$CALLS"
      case ",$FAILED_TAGS," in *",$2,"*) return 1 ;; esac ;;
    inspect) printf 'healthy\\n' ;;
    compose) return 0 ;;
    *) return 1 ;;
  esac
}
B=$TEST_BACKUP
S=s
OLD_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
API_IMG=api:latest
WORKER_IMG=worker:latest
REASONER_IMG=reasoner:latest
EGRESS_IMG=egress:latest
R_OLD=old-reasoner
E_OLD=old-egress
CRON_FILE=/unused/cron
false
rollback_release
'''
    env = os.environ | {
        "TEST_BACKUP": str(backup), "CALLS": str(calls),
        "FAILED_TAGS": ",".join(f"company-os-{name}:rollback-s" for name in failed_images),
    }
    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, env=env)
    assert result.returncode == 1  # preserve the original release failure
    assert calls.read_text().splitlines() == [
        f"tag company-os-{name}:rollback-s {name}:latest"
        for name in ("api", "worker", "reasoner", "egress")
    ]
    phase = (backup / "phase").read_text().strip()
    expected = "ROLLED_BACK_PARTIAL" if failed_images else "ROLLED_BACK_VERIFIED"
    assert phase == expected
    clean = subprocess.run(["bash", "-c", clean_fn + '\nphase_is_clean "$1"', "_", phase])
    assert (clean.returncode == 0) is (not failed_images)
    if failed_images:
        assert "ROLLBACK VERIFIED" not in result.stdout + result.stderr
        assert "PARTIALLY ROLLED BACK" in result.stderr


def test_attempted_migration_blocks_before_any_old_code_or_container_is_restored(tmp_path):
    (tmp_path / "migration-attempted").write_text("a -> b\n")
    touched = tmp_path / "unsafe-restore"
    script = remote_function("rollback_core") + '''
restore_source() { touch "$UNSAFE"; }
up_services() { touch "$UNSAFE"; }
B=$BACKUP
rollback_core
rc=$?
printf '%s %s' "$rc" "$ROLLBACK_RESULT"
'''
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                            env=os.environ | {"BACKUP": str(tmp_path), "UNSAFE": str(touched)})
    assert result.stdout == "1 PARTIAL"
    assert not touched.exists()


def test_restore_source_propagates_failure_removing_a_new_release_file(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "files-before.tgz").write_bytes(b"fixture")
    (backup / "files-new.list").write_text("new-file.py\n")
    fn = remote_function("restore_source")
    script = fn + '''
tar() { return 0; }
rm() { return 1; }
restore_source "$1"
'''
    result = subprocess.run(["bash", "-c", script, "_", str(backup)], text=True, capture_output=True)
    assert result.returncode != 0


@pytest.mark.parametrize("rev0, target, allow, authorized", [
    ("c7d2e8a4f1b3", "c7d2e8a4f1b3", "", True),                # no schema change: nothing to authorize
    ("c7d2e8a4f1b3", "b4f8a2d6c1e9", "b4f8a2d6c1e9", True),    # the exact target, independently authorized
    ("c7d2e8a4f1b3", "b4f8a2d6c1e9", "", False),               # the 2026-10-08 incident
    ("c7d2e8a4f1b3", "b4f8a2d6c1e9", "c7d2e8a4f1b3", False),   # authorization for another revision
    ("c7d2e8a4f1b3", "", "", False),                           # unknown target (alembic failed): refuse
    ("", "b4f8a2d6c1e9", "b4f8a2d6c1e9", False),               # unknown current revision: refuse
])
def test_a_schema_change_runs_only_with_authorization_for_that_exact_target(rev0, target, allow, authorized):
    assert (migration_gate(rev0, target, allow).returncode == 0) is authorized


def test_the_migration_gate_applies_to_every_deploy_and_restores_files_before_aborting():
    assert "COMPANY_OS_AUTOMATIC_DEPLOY" not in REMOTE                        # the host never reads the unforwarded name
    call = REMOTE.index('if ! migration_authorized "$REV0" "$TARGET_REV" "$ALLOW_MIGRATION"; then')
    block = REMOTE[call:REMOTE.index("\nfi\n", call)]
    assert 'abort_before_migration "schema change' in block
    assert "phase ABORTED_RESTORED" not in block
    assert REMOTE.index('TARGET_REV=$(') < call < REMOTE.index("alembic upgrade head")
    assert call < REMOTE.index("trap rollback_release ERR")


def test_release_abort_paths_use_checked_recovery_before_a_clean_terminal_phase():
    assert 'if ! check_release_permissions; then\n  abort_before_migration ' in REMOTE
    assert 'if ! run_compose config -q; then\n  abort_before_migration ' in REMOTE
    assert '*) abort_before_migration "unexpected compose images:' in REMOTE
    assert 'abort_before_migration "build failed: $1" "$cleanup_ok"' in REMOTE
    assert 'abort_migration_failure "Alembic upgrade failed' in REMOTE
    assert 'abort_host_cli_install "could not install the release companyctl' in REMOTE
    assert REMOTE.count("phase ABORTED_RESTORED") == 2              # only checked recovery helpers may mark clean


@pytest.mark.parametrize("source_status, tag_status, journal_status", [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)])
def test_failed_migration_never_records_clean_state(source_status, tag_status, journal_status, tmp_path):
    phases = tmp_path / "phases"
    fn = remote_function("abort_migration_failure")
    script = fn + '''
B=/unused
restore_source() { return "$SOURCE_STATUS"; }
restore_tags() { return "$TAGS_STATUS"; }
phase() { printf '%s\\n' "$1" >> "$PHASES"; return "$JOURNAL_STATUS"; }
abort_migration_failure "test migration failure"
'''
    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                            env=os.environ | {"SOURCE_STATUS": str(source_status), "TAGS_STATUS": str(tag_status),
                                              "JOURNAL_STATUS": str(journal_status), "PHASES": str(phases)})
    assert result.returncode == 1
    assert phases.read_text().splitlines() == ["ROLLBACK_FAILED"]
    assert "ABORTED_RESTORED" not in result.stdout + result.stderr
    assert "database state requires verification" in result.stderr


# --------------------------------------------------------------------------- P1: a historical SHA never brings its own driver

def test_the_readiness_check_runs_mains_own_code_never_the_requested_commits():
    assert "github.ref == 'refs/heads/main'" in CI["jobs"]["deploy-authorization"]["if"]
    for name, job in CI["jobs"].items():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout"):
                assert "ref" not in step["with"] and "inputs.sha" not in str(step), name


def _git(cwd, *args, **env):
    return subprocess.run(["git", "-C", str(cwd), *args], text=True, capture_output=True, check=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", **env}).stdout.strip()


def test_an_older_commit_is_only_a_payload_it_cannot_replace_the_host_script(tmp_path):
    """The old commit carries a driver WITHOUT the controls; deploying it must still send today's host script."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "app").mkdir()
    _git(repo.parent, "init", "-q", str(repo))
    for name in ("migrations", "agents", "skills", "tests", "docs"):
        (repo / name).mkdir()
        (repo / name / "keep").write_text("x\n")
    for name in ("alembic.ini", "requirements.txt", "Dockerfile", "docker-compose.yml", "docs/PERMANENT_FACTS.md"):
        (repo / name).write_text("x\n")
    (repo / "app/x.py").write_text("old payload\n")
    (repo / "scripts/deploy-company-os.sh").write_text("#!/bin/sh\n# OLD DRIVER: no authorization controls\n")
    (repo / "scripts/company-os.cron").write_text("# old schedule\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "old")
    old = _git(repo, "rev-parse", "HEAD")
    (repo / "app/x.py").write_text("new payload\n")
    (repo / "scripts/deploy-company-os.sh").write_text(SOURCE)             # main's driver
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "new")
    shim = tmp_path / "shim"
    shim.mkdir()
    up = tmp_path / "uploaded"
    up.mkdir()
    (shim / "ssh").write_text(f'#!/bin/sh\nshift\ncase "$*" in *"tail -n 1"*) printf "42\\nEXIT 0\\n";; *"cat /var/log/company-os-deploy.pid"*) echo 42;; esac\nexit 0\n')
    (shim / "scp").write_text(f'#!/bin/sh\nfor f in "$@"; do case "$f" in /*) cp "$f" "{up}/" 2>/dev/null;; esac; done\nexit 0\n')
    for f in ("ssh", "scp"):
        (shim / f).chmod(0o755)
    base = {k: v for k, v in os.environ.items() if not k.startswith(("COMPANY_OS_", "ACK_"))}
    env = base | {"PATH": f"{shim}:{os.environ['PATH']}", "COMPANY_OS_DEPLOY_SHA": old,
                  "COMPANY_OS_DEPLOY_HOST": "fake-host", "COMPANY_OS_AUTOMATIC_DEPLOY": "true"}
    out = subprocess.run(["bash", str(DEPLOY), "--apply"], text=True, capture_output=True, env=env, cwd=repo)
    assert out.returncode == 0, out.stdout + out.stderr
    host_script = (up / "company-os-deploy.sh").read_text()
    assert host_script.strip() == REMOTE.split("\n", 1)[1].strip()             # byte-for-byte today's host script
    assert "migration_authorized() {" in host_script and "OLD DRIVER" not in host_script
    import tarfile
    with tarfile.open(up / "src.tgz") as t:                                  # the payload IS the old commit
        assert t.extractfile("app/x.py").read() == b"old payload\n"
        assert b"OLD DRIVER" in t.extractfile("scripts/deploy-company-os.sh").read()


def test_no_dispatch_ever_runs_a_deploy_driver():
    """The legacy SSH deploy is gone from the workflow: no step runs any driver, old or new."""
    for job, spec in CI["jobs"].items():
        for step in spec["steps"]:
            assert "deploy-company-os.sh" not in str(step.get("run", "")), job


# --------------------------------------------------------------------------- P1: the schedule never outlives a rejected deploy

CRON_FNS = REMOTE[REMOTE.index("CRON_FILE=${CRON_FILE:-/etc/cron.d/company-os}"):REMOTE.index('\nif [ "${DETACH:-}" = 1 ]; then')]
OLD, NEW = "# old schedule\n17 * * * * root old\n", "# new schedule\n17 * * * * root new\n"


def manifest(release, with_cron):
    """The payload manifest the driver ships (main.sha256): '<64 hex>  <path>' per released file."""
    lines = ["a" * 64 + "  app/main.py"] + (["b" * 64 + "  scripts/company-os.cron"] if with_cron else [])
    (release / "main.sha256").write_text("\n".join(lines) + "\n")


def cron_env(tmp_path, old=OLD):
    cron_d = tmp_path / "cron.d"
    cron_d.mkdir()
    release = tmp_path / "release"
    (release / "scripts").mkdir(parents=True)
    (release / "scripts/company-os.cron").write_text(NEW)
    manifest(release, with_cron=True)
    backup = tmp_path / "backup"
    backup.mkdir()
    live = cron_d / "company-os"
    if old is not None:
        live.write_text(old)
    return live, release, backup


def bash(tmp_path, release, live, body, path_prefix=""):
    env = {**os.environ, "CRON_FILE": str(live), "REL": str(release)}
    if path_prefix:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + CRON_FNS + "\n" + body], cwd=release, env=env,
                          text=True, capture_output=True)


def test_the_schedule_is_installed_last_only_for_a_verified_release():
    """Order in the host script: gate -> migrate -> switch -> health gate -> install_cron -> trap cleared."""
    gate = REMOTE.index('if ! migration_authorized "$REV0" "$TARGET_REV" "$ALLOW_MIGRATION"; then')
    migrate = REMOTE.index("alembic upgrade head")
    health = REMOTE.index('"$GATE" "$SHA"\n')
    install = REMOTE.index("\ninstall_cron ")
    assert gate < migrate < health < install < REMOTE.index("trap - ERR\nphase VERIFIED\necho \"RELEASE VERIFIED")
    assert REMOTE.index("trap rollback_release ERR") < install                 # a failed install rolls the release back
    assert REMOTE.count('install_cron "$REL/main.sha256"') == 1 and "install -m 0644 scripts/company-os.cron" not in REMOTE   # only via the function
    code = [l for l in REMOTE.replace(CRON_FNS, "").splitlines() if not l.lstrip().startswith("#")]
    assert not [l for l in code if "/etc/cron.d" in l or "company-os.cron" in l]   # no other writer of the live file
    assert REMOTE.index('snapshot_cron "$B"') < REMOTE.index("== install source")  # the old schedule is saved first
    rollback = REMOTE[REMOTE.index("rollback_release() {"):REMOTE.index("\n}\n", REMOTE.index("rollback_release() {"))]
    core = REMOTE[REMOTE.index("rollback_core() {"):REMOTE.index("# Recovery after an interrupted deploy")]
    assert 'restore_cron "$B" || cli_restored=no' in core and "cron schedule could NOT be verified" in rollback


def test_a_rejected_migration_leaves_the_live_schedule_untouched_end_to_end(tmp_path):
    """Run the real host steps from the snapshot to the gate with the schedule as the only moving part."""
    live, release, backup = cron_env(tmp_path)
    before = live.read_bytes()
    script = (f'snapshot_cron "{backup}"\n'
              + REMOTE[REMOTE.index("migration_authorized() {"):REMOTE.index("\n}\n", REMOTE.index("migration_authorized() {")) + 3]
              + 'if ! migration_authorized "c7d2e8a4f1b3" "b4f8a2d6c1e9" ""; then echo REFUSED; exit 1; fi\ninstall_cron \"$REL/main.sha256\"\n')
    out = bash(tmp_path, release, live, script)
    assert out.returncode == 1 and "REFUSED" in out.stdout
    assert live.read_bytes() == before and sorted(p.name for p in live.parent.iterdir()) == ["company-os"]


def test_install_replaces_the_schedule_atomically_and_leaves_no_temp_file(tmp_path):
    live, release, backup = cron_env(tmp_path)
    assert bash(tmp_path, release, live, f'snapshot_cron "{backup}"\ninstall_cron \"$REL/main.sha256\"').returncode == 0
    assert live.read_text() == NEW and oct(live.stat().st_mode & 0o777) == "0o644"
    assert (backup / "cron-before").read_text() == OLD
    assert sorted(p.name for p in live.parent.iterdir()) == ["company-os"]     # no dotted temp left behind


def test_an_interrupted_install_never_leaves_a_partial_or_changed_schedule(tmp_path):
    live, release, backup = cron_env(tmp_path)
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "mv").write_text("#!/bin/sh\nexit 1\n")                           # the rename is the commit point; it fails
    (shim / "mv").chmod(0o755)
    out = bash(tmp_path, release, live, f'snapshot_cron "{backup}"\ninstall_cron "$REL/main.sha256"', path_prefix=str(shim))
    assert out.returncode != 0
    assert live.read_text() == OLD                                            # untouched
    assert [p.name for p in live.parent.iterdir()] == ["company-os"]           # the temp file was cleaned up


def test_a_failed_copy_leaves_the_schedule_untouched(tmp_path):
    live, release, backup = cron_env(tmp_path)
    (release / "scripts/company-os.cron").unlink()                            # the release has no schedule file
    assert bash(tmp_path, release, live, f'snapshot_cron "{backup}"\ninstall_cron \"$REL/main.sha256\"').returncode != 0
    assert live.read_text() == OLD


def test_rollback_restores_the_previous_schedule_and_verifies_it(tmp_path):
    live, release, backup = cron_env(tmp_path)
    out = bash(tmp_path, release, live, f'snapshot_cron "{backup}"\ninstall_cron \"$REL/main.sha256\"\nrestore_cron "{backup}"\necho RESTORED')
    assert out.returncode == 0 and "RESTORED" in out.stdout and live.read_text() == OLD


def test_rollback_removes_a_schedule_that_did_not_exist_before(tmp_path):
    live, release, backup = cron_env(tmp_path, old=None)
    out = bash(tmp_path, release, live, f'snapshot_cron "{backup}"\ninstall_cron \"$REL/main.sha256\"\nrestore_cron "{backup}"\necho RESTORED')
    assert out.returncode == 0 and (backup / "cron-absent").exists() and not live.exists()


def test_an_unverifiable_restore_is_reported_not_assumed(tmp_path):
    live, release, backup = cron_env(tmp_path)
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "cmp").write_text("#!/bin/sh\nexit 1\n")                          # verification cannot confirm the bytes
    (shim / "cmp").chmod(0o755)
    out = bash(tmp_path, release, live, f'snapshot_cron "{backup}"\ninstall_cron \"$REL/main.sha256\" || true\nrestore_cron "{backup}"\necho OK', path_prefix=str(shim))
    assert out.returncode != 0 and "OK" not in out.stdout
    assert bash(tmp_path, release, live, f'restore_cron "{tmp_path}/empty"').returncode != 0   # no snapshot: refuse, don't guess


def test_a_cron_path_that_is_not_a_regular_file_is_never_managed(tmp_path):
    live, release, backup = cron_env(tmp_path, old=None)
    target = tmp_path / "elsewhere"
    target.write_text("keep\n")
    live.symlink_to(target)
    out = bash(tmp_path, release, live, f'snapshot_cron "{backup}"')
    assert out.returncode != 0 and "not a regular file" in out.stdout and target.read_text() == "keep\n"


def test_a_release_that_removes_the_schedule_removes_it_instead_of_reinstalling_the_stale_copy(tmp_path):
    """Codex P2: the previous release's scripts/company-os.cron stays on disk after extraction."""
    live, release, backup = cron_env(tmp_path)
    manifest(release, with_cron=False)                                       # the target commit has no schedule file
    assert (release / "scripts/company-os.cron").exists()                    # ...but the stale copy is still there
    out = bash(tmp_path, release, live, f'snapshot_cron "{backup}"\ninstall_cron \"$REL/main.sha256\"\necho DONE')
    assert out.returncode == 0 and "DONE" in out.stdout and not live.exists()
    assert bash(tmp_path, release, live, f'restore_cron "{backup}"').returncode == 0 and live.read_text() == OLD


def test_a_missing_or_unreadable_manifest_fails_closed_and_changes_nothing(tmp_path):
    live, release, backup = cron_env(tmp_path)
    (release / "main.sha256").unlink()
    out = bash(tmp_path, release, live, f'snapshot_cron "{backup}"\ninstall_cron \"$REL/main.sha256\"')
    assert out.returncode != 0 and live.read_text() == OLD


@pytest.mark.parametrize("line", ["b" * 64 + "  scripts/company-os.cron.bak", "b" * 64 + "  docs/scripts/company-os.cron",
                                  "b" * 64 + " scripts/company-os.cron", "short  scripts/company-os.cron"])
def test_only_the_exact_manifest_entry_counts_as_the_schedule(tmp_path, line):
    live, release, backup = cron_env(tmp_path)
    (release / "main.sha256").write_text("a" * 64 + "  app/main.py\n" + line + "\n")
    assert bash(tmp_path, release, live, f'snapshot_cron "{backup}"\ninstall_cron \"$REL/main.sha256\"').returncode == 0
    assert not live.exists()                                                  # a look-alike is not the schedule


# --------------------------------------------------------------------------- helper contract and runbook accuracy

RUNBOOK = (ROOT / "docs/runbooks/deploy-authorization.md").read_text()


def _auth_run():
    step = next(s for s in CI["jobs"]["deploy-authorization"]["steps"] if "deploy-authorization.py" in str(s.get("run", "")))
    return step["run"]


def test_only_the_dispatched_main_tip_can_be_deployed_so_a_payload_is_never_historical():
    """Codex P1: the host overlays the archive without deleting files an older commit lacks (a mixed release)."""
    run = _auth_run()
    assert '"$DEPLOY_SHA"' in run and '"$GITHUB_SHA"' in run
    assert 'if [ "$DEPLOY_SHA" != "$GITHUB_SHA" ]; then' in run
    assert run.index('"$DEPLOY_SHA" != "$GITHUB_SHA"') < run.index("python scripts/deploy-authorization.py")
    assert "HOST-ONLY (kept on the host" in REMOTE                           # the host keeps files on purpose: never prune them


@pytest.mark.parametrize("deploy_sha, github_sha, ok_", [("a" * 40, "a" * 40, True), ("a" * 40, "b" * 40, False),
                                                        ("", "b" * 40, False), ("b" * 39, "b" * 40, False)])
def test_the_tip_only_check_refuses_anything_but_the_dispatched_commit(deploy_sha, github_sha, ok_):
    line = 'test "$DEPLOY_SHA" = "$GITHUB_SHA"'
    out = subprocess.run(["bash", "-c", "set -euo pipefail\n" + line], env={**os.environ, "DEPLOY_SHA": deploy_sha,
                         "GITHUB_SHA": github_sha}, capture_output=True)
    assert (out.returncode == 0) is ok_
    refuse = _auth_run()[_auth_run().index('if [ "$DEPLOY_SHA"'):_auth_run().index("python scripts/deploy-authorization.py")]
    out = subprocess.run(["bash", "-c", "set -euo pipefail\n" + refuse], env={**os.environ, "DEPLOY_SHA": deploy_sha,
                         "GITHUB_SHA": github_sha}, capture_output=True, text=True)
    assert (out.returncode == 0) is ok_ and (ok_ or "DEPLOY REFUSED" in out.stdout)


def test_the_runbook_never_asks_an_operator_to_run_the_deploy_transport():
    commands = [l for l in RUNBOOK.splitlines() if l.startswith("|")]
    assert not [l for l in commands if "deploy-company-os.sh" in l]          # even without --apply it writes to /tmp
    assert "--apply" not in "\n".join(commands) and "sudo tee" not in RUNBOOK and "alembic upgrade" not in RUNBOOK
    assert "does not run `scripts/deploy-company-os.sh`" in RUNBOOK


def test_the_runbook_treats_a_deliberately_absent_schedule_as_a_valid_state():
    assert "| 6 | Does `$SHA` ship a schedule?" in RUNBOOK
    assert "7c | If absent" in RUNBOOK and "a missing live file is the expected result" in RUNBOOK
    assert "test ! -e /etc/cron.d/company-os" in RUNBOOK


# --------------------------------------------------------------------------- rollback preserves the previous schedule's metadata

def test_rollback_restores_the_previous_schedules_mode_not_a_normalized_one(tmp_path):
    live, release, backup = cron_env(tmp_path)
    live.chmod(0o640)                                                          # not the 0644 the installer would write
    out = bash(tmp_path, release, live, f'snapshot_cron "{backup}"\ninstall_cron "$REL/main.sha256"\n'
                                         f'test "$(cron_meta "$CRON_FILE")" != "$(cat {backup}/cron-meta)"\nrestore_cron "{backup}"\necho RESTORED')
    assert out.returncode == 0 and "RESTORED" in out.stdout, out.stdout + out.stderr    # the install really changed the mode
    assert live.read_text() == OLD and oct(live.stat().st_mode & 0o777) == "0o640"


def test_a_schedule_that_was_never_changed_is_not_rewritten_by_rollback(tmp_path):
    live, release, backup = cron_env(tmp_path)
    live.chmod(0o640)
    before = (live.stat().st_ino, live.stat().st_mtime_ns)
    out = bash(tmp_path, release, live, f'snapshot_cron "{backup}"\nrestore_cron "{backup}"\necho OK')   # install never ran
    assert out.returncode == 0 and (live.stat().st_ino, live.stat().st_mtime_ns) == before


def test_a_restore_that_cannot_reproduce_the_recorded_metadata_is_reported(tmp_path):
    live, release, backup = cron_env(tmp_path)
    out = bash(tmp_path, release, live, f'snapshot_cron "{backup}"\ninstall_cron "$REL/main.sha256"\n'
                                         f'echo "0:0:600" > "{backup}/cron-meta"\nrestore_cron "{backup}"\necho OK')
    assert out.returncode != 0 and "OK" not in out.stdout                      # bytes match, recorded metadata does not


def test_a_snapshot_without_recorded_metadata_cannot_be_trusted_for_restore(tmp_path):
    live, release, backup = cron_env(tmp_path)
    assert bash(tmp_path, release, live, f'snapshot_cron "{backup}"').returncode == 0
    (backup / "cron-meta").unlink()
    assert bash(tmp_path, release, live, f'restore_cron "{backup}"').returncode != 0


def test_the_runbooks_migration_head_command_cannot_succeed_with_an_empty_result():
    row = next(l for l in RUNBOOK.splitlines() if l.startswith("| 5 |"))
    assert "git show $SHA:migrations/versions/*.py" not in RUNBOOK              # exits 0 and prints nothing
    assert "python3 -m alembic heads" in row and "never connects" in row and "empty result" in row
