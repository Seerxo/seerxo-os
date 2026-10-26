"""Stage C preflight: deterministic, zero model calls, unknown is never a pass."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app import models, stage_c_preflight as pf
from tests.test_worker import _db

SHA = "a" * 40
CHECKS_COMMAND = ["python", "-m", "pytest", "-q"]
COMMAND_FINGERPRINT = hashlib.sha256(json.dumps(CHECKS_COMMAND, separators=(",", ":")).encode()).hexdigest()
CHECKS = dict.fromkeys(pf.ACCEPTANCE_CHECKS, True)
HOST = {"api_sha": SHA, "worker_sha": SHA, "launcher_path_active": True, "launcher_config_ok": True,
        "coder_image_present": True, "checks_image_present": True, "egress_running": True,
        "claude_env_ok": True, "claude_auth_ok": True, "codex_reviewer_volume_mounted": True,
        "checks_command_fingerprints": {"sez-company": COMMAND_FINGERPRINT}}
HEALTH = {"claude": "CREDENTIALS_CONFIGURED", "codex": "CREDENTIALS_CONFIGURED"}


def world(tmp_path, db=None, **over):
    """A fully ready world; each test then breaks exactly one thing."""
    jobs = tmp_path / "jobs"
    (jobs / ".control/sez-company.git").mkdir(parents=True)
    db = db or _db()
    db.add(models.ControlState(key="stage-c-host-acceptance", updated_by="founder",
                               value={"source_sha": SHA, "checks": CHECKS}))
    db.commit()
    args = dict(db=db, host=dict(HOST), health=dict(HEALTH), jobs=jobs, uid=os.getuid(), flag=False,
                environ={"COMPANY_OS_GIT_SHA": SHA, "GH_TOKEN": "x"}, binaries=lambda b: f"/bin/{b}",
                run=readable)
    args.update(over)
    return args


def whoami(login, scopes="repo, workflow"):
    """`gh api -i user`: headers, blank line, body. The worker token's own identity."""
    return SimpleNamespace(returncode=0, stdout=f"HTTP/2 200\r\nX-OAuth-Scopes: {scopes}\r\n\r\n"
                                                f'{{"login": "{login}"}}')


READ_ONLY = {"admin": False, "maintain": False, "push": False, "triage": False, "pull": True}


def role(**perms):
    """`gh api repos/seerxo/seerxo-os`: the worker token's effective upstream role, as GitHub reports it."""
    return SimpleNamespace(returncode=0, stdout=json.dumps({"full_name": "seerxo/seerxo-os",
                                                            "permissions": READ_ONLY | perms}))


def readable(argv, **kwargs):
    """GitHub answers the worker's identity and whichever CI evidence sources the test policy requires."""
    if argv[-1] == "user":
        return whoami("seerxo-agent")
    if argv[-1] == "repos/seerxo/seerxo-os":
        return role()
    return SimpleNamespace(returncode=0, stdout='{"workflow_runs": []}' if "/actions/" in argv[-1] else "[]")


def status(report, name):
    return next(c["status"] for c in report["checks"] if c["name"] == name)


def ci_policy(tmp_path, **changes):
    policy = yaml.safe_load(pf.stage_c_worker.POLICY.read_text())
    policy.update(changes)
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy))
    return path


@pytest.mark.parametrize("unused_statuses", [None, "not JSON", '[null]'])
def test_actions_only_preflight_does_not_demand_commit_status_permission(tmp_path, unused_statuses):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[-1])
        if "/statuses" in argv[-1]:
            return SimpleNamespace(returncode=1 if unused_statuses is None else 0,
                                   stdout=unused_statuses or "", stderr="HTTP 403")
        return readable(argv)

    report = pf.evaluate(**world(tmp_path, run=run))
    assert report["ready"] is True
    assert status(report, "github_actions") == "PASS"
    assert status(report, "github_commit_statuses") == "PASS"
    assert "not required" in next(c["detail"] for c in report["checks"] if c["name"] == "github_commit_statuses")
    assert calls == ["user", "repos/seerxo/seerxo-os",                           # identity and role, then Actions
                     "repos/seerxo/seerxo-os/actions/runs?per_page=1"]          # no statuses


@pytest.mark.parametrize("contexts", [None, "", ["ci", "ci"]])
def test_preflight_rejects_malformed_required_status_policy_before_any_read(tmp_path, contexts):
    path = ci_policy(tmp_path, required_statuses=contexts)
    report = pf.evaluate(**world(tmp_path, policy=path,
                                 run=lambda *args, **kwargs: pytest.fail("invalid policy must not query GitHub")))
    assert report["ready"] is False and status(report, "guardian_policy") == "FAIL"
    assert status(report, "worker_identity") == "UNKNOWN"   # not even the identity read runs


def test_preflight_requires_at_least_one_ci_source(tmp_path):
    path = ci_policy(tmp_path, required_statuses=[], workflows=[])
    report = pf.evaluate(**world(tmp_path, policy=path))
    assert report["ready"] is False and status(report, "guardian_policy") == "FAIL"


def test_everything_proven_is_ready_and_the_flag_is_reported_not_required(tmp_path):
    report = pf.evaluate(**world(tmp_path))
    assert report["ready"] is True and report["code_write_flag"] is False
    assert {c["status"] for c in report["checks"]} == {"PASS"}


def test_no_host_facts_means_unknown_never_pass(tmp_path):
    report = pf.evaluate(**world(tmp_path, host=None))
    assert report["ready"] is False
    for name in ("api_worker_sha_agree", "launcher", "coder_image", "checks_image", "egress_proxy",
                 "coder_logins", "claude_auth", "codex_reviewer_volume", "checks_command"):
        assert status(report, name) == "UNKNOWN", name


def test_missing_acceptance_fails_acceptance_and_every_attested_check(tmp_path):
    args = world(tmp_path)
    args["db"].query(models.ControlState).delete()
    args["db"].commit()
    report = pf.evaluate(**args)
    assert status(report, "acceptance_recorded") == "FAIL"
    assert status(report, "acceptance_source_sha") == "FAIL"
    assert all(status(report, f"acceptance:{k}") == "UNKNOWN" for k in pf.ACCEPTANCE_CHECKS)
    assert report["ready"] is False


@pytest.mark.parametrize("who", ["claude", "worker", "agent"])
def test_only_the_founder_can_attest(tmp_path, who):
    args = world(tmp_path)
    args["db"].get(models.ControlState, "stage-c-host-acceptance").updated_by = who
    args["db"].commit()
    assert status(pf.evaluate(**args), "acceptance_recorded") == "FAIL"


def test_acceptance_for_another_source_sha_fails(tmp_path):
    args = world(tmp_path)
    args["db"].get(models.ControlState, "stage-c-host-acceptance").value = {
        "source_sha": "b" * 40, "checks": CHECKS}
    args["db"].commit()
    assert status(pf.evaluate(**args), "acceptance_source_sha") == "FAIL"


@pytest.mark.parametrize("value,expected", [(False, "FAIL"), ("true", "FAIL"), (None, "FAIL"), ("absent", "UNKNOWN")])
def test_each_security_acceptance_check_is_reported_individually(tmp_path, value, expected):
    args = world(tmp_path)
    row = args["db"].get(models.ControlState, "stage-c-host-acceptance")
    checks = dict(CHECKS)
    checks.pop("github_blocked") if value == "absent" else checks.update(github_blocked=value)
    row.value = {"source_sha": SHA, "checks": checks}
    args["db"].commit()
    report = pf.evaluate(**args)
    assert status(report, "acceptance:github_blocked") == expected
    assert status(report, "acceptance:egress") == "PASS" and report["ready"] is False


def test_api_and_worker_sha_must_agree_with_each_other_and_this_process(tmp_path):
    for i, host in enumerate(({**HOST, "api_sha": "b" * 40}, {**HOST, "worker_sha": "b" * 40},
                              {**HOST, "api_sha": "b" * 40, "worker_sha": "b" * 40})):
        assert status(pf.evaluate(**world(tmp_path / str(i), host=host)), "api_worker_sha_agree") == "FAIL"
    assert status(pf.evaluate(**world(tmp_path / "x", host={**HOST, "api_sha": None})), "api_worker_sha_agree") == "UNKNOWN"


def test_invalid_own_sha_fails(tmp_path):
    report = pf.evaluate(**world(tmp_path, environ={"COMPANY_OS_GIT_SHA": "main", "GH_TOKEN": "x"}))
    assert status(report, "source_sha") == "FAIL" and report["ready"] is False


@pytest.mark.parametrize("fact,check", [
    ("launcher_path_active", "launcher"), ("launcher_config_ok", "launcher"),
    ("coder_image_present", "coder_image"), ("checks_image_present", "checks_image"),
    ("egress_running", "egress_proxy"), ("claude_env_ok", "coder_logins"),
    ("claude_auth_ok", "claude_auth"),
    ("codex_reviewer_volume_mounted", "codex_reviewer_volume")])
def test_host_fact_false_fails_and_null_is_unknown(tmp_path, fact, check):
    assert status(pf.evaluate(**world(tmp_path / "a", host={**HOST, fact: False})), check) == "FAIL"
    assert status(pf.evaluate(**world(tmp_path / "b", host={**HOST, fact: None})), check) == "UNKNOWN"
    assert status(pf.evaluate(**world(tmp_path / "c", host={k: v for k, v in HOST.items() if k != fact})), check) == "UNKNOWN"


@pytest.mark.parametrize("proof", [None, {}, {"sez-company": None}, "not a mapping"])
def test_missing_checks_command_proof_is_unknown(tmp_path, proof):
    report = pf.evaluate(**world(tmp_path, host={**HOST, "checks_command_fingerprints": proof}))
    assert status(report, "checks_command") == "UNKNOWN"
    assert report["ready"] is False


@pytest.mark.parametrize("fingerprint", [123, "not-a-hash", [], False, {},
    "A" * 64, "a" * 63, "a" * 65, "a" * 64 + "\n"])
def test_malformed_checks_command_fingerprint_is_unknown(tmp_path, fingerprint):
    report = pf.evaluate(**world(tmp_path, host={**HOST, "checks_command_fingerprints": {
        "sez-company": fingerprint}}))
    assert status(report, "checks_command") == "UNKNOWN"
    assert report["ready"] is False


def test_checks_command_from_another_profile_cannot_authorize_this_writer(tmp_path):
    report = pf.evaluate(**world(tmp_path, host={**HOST, "checks_command_fingerprints": {
        "other-profile": COMMAND_FINGERPRINT}}))
    assert status(report, "checks_command") == "UNKNOWN"
    assert report["ready"] is False


def test_jobs_directory_and_mirror_ownership_and_permissions(tmp_path):
    args = world(tmp_path)
    assert status(pf.evaluate(**args), "jobs_directory") == "PASS"
    assert status(pf.evaluate(**{**args, "uid": os.getuid() + 1}), "jobs_directory") == "FAIL"
    assert status(pf.evaluate(**{**args, "uid": os.getuid() + 1}), "repository_mirror") == "FAIL"
    args["jobs"].chmod(0o777)
    assert status(pf.evaluate(**args), "jobs_directory") == "FAIL"
    args["jobs"].chmod(0o755)
    assert status(pf.evaluate(**{**args, "jobs": tmp_path / "missing"}), "jobs_directory") == "FAIL"
    (args["jobs"] / ".control/sez-company.git").rmdir()
    assert status(pf.evaluate(**args), "repository_mirror") == "FAIL"


def test_github_credential_presence_is_reported_without_revealing_it(tmp_path):
    secret = "ghp_" + "S" * 36
    report = pf.evaluate(**world(tmp_path, environ={"COMPANY_OS_GIT_SHA": SHA, "GH_TOKEN": secret}))
    assert status(report, "github_credential") == "PASS" and secret not in json.dumps(report)
    missing = pf.evaluate(**world(tmp_path / "n", environ={"COMPANY_OS_GIT_SHA": SHA}))
    assert status(missing, "github_credential") == "FAIL"
    assert "not set" in next(c["detail"] for c in missing["checks"] if c["name"] == "github_credential")
    assert status(pf.evaluate(**world(tmp_path / "b", binaries=lambda b: None)), "github_credential") == "FAIL"


@pytest.mark.parametrize("code", [401, 403, 404])
def test_a_token_that_cannot_read_ci_statuses_is_not_ready(tmp_path, code):
    report = pf.evaluate(**world(tmp_path, policy=ci_policy(tmp_path, required_statuses=["ci/test"]),
                                run=lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="secret body", stderr=f"secret token (HTTP {code})")))

    assert report["ready"] is False
    assert status(report, "github_commit_statuses") == "FAIL"
    assert "secret" not in json.dumps(report)
    assert "Commit statuses: Read-only" in pf.render(report)


def test_ci_access_checks_the_deployed_commit_using_only_the_worker_environment(tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return readable(argv)

    args = world(tmp_path, run=run, policy=ci_policy(tmp_path, required_statuses=["ci/test"]))
    report = pf.evaluate(**args)
    assert report["ready"] is True  # empty statuses prove read access, not green CI
    assert calls[0][0] == ["gh", "api", "--hostname", "github.com", "-i", "user"]       # whose token, first
    assert calls[1][0] == ["gh", "api", "--hostname", "github.com", "repos/seerxo/seerxo-os"]  # then its upstream role
    identity, calls = calls[:2], calls[2:]
    assert all(c[1]["env"] == args["environ"] for c in identity)
    assert calls[0][0] == ["gh", "api", "--method", "GET",
                          f"repos/seerxo/seerxo-os/commits/{SHA}/statuses?per_page=1"]
    # The test policy also requires Actions workflows, so their read access is proven too.
    assert [c[0] for c in calls[1:]] == [["gh", "api", "--method", "GET",
                                          "repos/seerxo/seerxo-os/actions/runs?per_page=1"]]
    assert all(c[1]["env"] == args["environ"] and c[1]["timeout"] == 10 for c in calls)
    assert "does not assert CI passed" in pf.render(report)


@pytest.mark.parametrize("result", [
    SimpleNamespace(returncode=1, stdout="", stderr="network error with secret"),
    SimpleNamespace(returncode=0, stdout="not json: secret"),
    SimpleNamespace(returncode=0, stdout='{"message": "secret"}'),
    SimpleNamespace(returncode=0, stdout='[null]'),
    SimpleNamespace(returncode=0, stdout='["invalid"]'),
    SimpleNamespace(returncode=0, stdout='[{"id": 1, "context": "ci", "state": [], "creator": {}}]'),
    pf.subprocess.TimeoutExpired(["gh"], 10, stderr="secret"),
])
def test_unknown_ci_access_is_never_ready_and_does_not_echo_output(tmp_path, result):
    def run(*args, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    report = pf.evaluate(**world(tmp_path, run=run, policy=ci_policy(tmp_path, required_statuses=["ci/test"])))
    assert report["ready"] is False and status(report, "github_commit_statuses") == "UNKNOWN"
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize("response,expected", [
    (SimpleNamespace(returncode=1, stdout="", stderr="HTTP 403"), "FAIL"),
    (SimpleNamespace(returncode=0, stdout='{"workflow_runs": [null]}'), "UNKNOWN"),
    (SimpleNamespace(returncode=0, stdout='{"workflow_runs": []}'), "PASS"),
])
def test_configured_actions_workflows_also_require_read_access(tmp_path, response, expected):
    import yaml

    policy = yaml.safe_load(pf.stage_c_worker.POLICY.read_text())
    policy["workflows"] = [".github/workflows/ci.yml"]
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy))

    def run(argv, **kwargs):
        if "/actions/" in argv[-1]:
            return response
        return readable(argv)

    report = pf.evaluate(**world(tmp_path, policy=path, run=run))
    assert status(report, "github_commit_statuses") == "PASS"
    assert status(report, "github_actions") == expected
    assert report["ready"] is (expected == "PASS")


@pytest.mark.parametrize("contexts", [[], ["ci/test"]])
def test_no_token_never_attempts_a_github_read(tmp_path, contexts):
    def run(*args, **kwargs):
        pytest.fail("a missing credential must not make a network request")

    report = pf.evaluate(**world(tmp_path, environ={"COMPANY_OS_GIT_SHA": SHA}, run=run,
                                 policy=ci_policy(tmp_path, required_statuses=contexts)))
    assert status(report, "github_commit_statuses") == ("UNKNOWN" if contexts else "PASS")
    assert status(report, "github_actions") == "UNKNOWN" and report["ready"] is False


def test_two_independent_usable_providers_are_required(tmp_path):
    for i, health in enumerate(({"claude": "CREDENTIALS_CONFIGURED", "codex": "AUTH_REQUIRED"},
                                {"claude": "CREDENTIALS_CONFIGURED"},
                                {"claude": "QUOTA_EXHAUSTED", "codex": "RATE_LIMITED"})):
        assert status(pf.evaluate(**world(tmp_path / str(i), health=health)), "independent_providers") == "FAIL"
    assert status(pf.evaluate(**world(tmp_path / "u", health=None)), "independent_providers") == "UNKNOWN"


def test_provider_credentials_are_not_model_access_evidence(tmp_path):
    args = world(tmp_path, health={"claude": "CREDENTIALS_CONFIGURED", "codex": "CREDENTIALS_CONFIGURED"})
    row = args["db"].get(models.ControlState, "stage-c-host-acceptance")
    row.value = {**row.value, "checks": {**row.value["checks"], "codex_review_probe": False}}
    args["db"].commit()
    report = pf.evaluate(**args)
    assert status(report, "independent_providers") == "PASS"
    assert status(report, "acceptance:codex_review_probe") == "FAIL"
    assert report["ready"] is False
    assert "live access" in next(
        c["detail"] for c in report["checks"] if c["name"] == "independent_providers").lower()


def test_guardian_policy_must_name_exact_repository_and_branch(tmp_path):
    ok = pf.evaluate(**world(tmp_path / "ok"))
    assert status(ok, "allowed_repository") == status(ok, "allowed_branch") == status(ok, "guardian_policy") == "PASS"
    bad = ci_policy(tmp_path, repositories=["semihbugrasezer/other"], branches={"semihbugrasezer/other": "main"},
                    publish_forks={})   # a fork for a repository no longer listed would fail parsing instead
    report = pf.evaluate(**world(tmp_path / "bad", policy=bad))
    assert status(report, "allowed_repository") == "FAIL" and report["ready"] is False
    bad.write_text("not: [valid")
    assert status(pf.evaluate(**world(tmp_path / "junk", policy=bad)), "guardian_policy") == "FAIL"
    assert status(pf.evaluate(**world(tmp_path / "gone", policy=tmp_path / "nope.yaml")), "guardian_policy") == "FAIL"


def test_project_profiles_require_one_supported_writer_and_keep_other_repos_read_only(tmp_path):
    import yaml

    raw = yaml.safe_load(pf.project_profiles.PROFILE_PATH.read_text())
    youtube = raw["projects"]["youtube-auto"]["repositories"]["semihbugrasezer/youtube-auto"]
    youtube.update(write_authorized=True, allowed_paths=["tests/test_metrics.py"],
                   execution_profile="sez-company")
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    report = pf.evaluate(**world(tmp_path, profiles=path))
    assert status(report, "project_profiles") == "FAIL"
    assert report["ready"] is False

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("version: 1\nprojects: [invalid")
    report = pf.evaluate(**world(tmp_path / "invalid", profiles=invalid))
    assert status(report, "project_profiles") == "FAIL"


def test_flag_state_is_surfaced(tmp_path):
    assert pf.evaluate(**world(tmp_path, flag=True))["code_write_flag"] is True


def test_evaluation_never_raises_and_never_makes_a_model_call(tmp_path, monkeypatch):
    from app import providers
    monkeypatch.setattr(providers.Provider, "run", lambda *a, **k: pytest.fail("model call"))
    args = world(tmp_path, host={"api_sha": 5, "coder_image_present": "yes"})
    report = pf.evaluate(**args)
    assert report["ready"] is False and status(report, "coder_image") == "UNKNOWN"


# --- host collector: pure functions over an injected runner, same pattern as the sandbox probe ---

SPEC = importlib.util.spec_from_file_location(
    "stage_c_host_facts", Path(__file__).parents[1] / "scripts" / "stage-c-host-facts.py")
host_facts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host_facts)
IMAGE = "sha256:" + "c" * 64
LAUNCHER_CFG = {"coder_image": IMAGE, "checks_images": {"sez-company": IMAGE},
                "checks_commands": {"sez-company": CHECKS_COMMAND},
                "network": "company-coder-net", "proxy": "http://company-egress:8888"}


def runner(answers):
    def run(argv):
        for prefix, (rc, out) in answers.items():
            if " ".join(argv).startswith(prefix):
                return rc, out
        return 127, ""
    return run


def test_collector_reports_only_booleans_shas_and_fingerprints_and_unknown_on_missing_tools(tmp_path):
    cfg = tmp_path / "launcher.json"
    cfg.write_text(json.dumps(LAUNCHER_CFG))
    claude = tmp_path / "claude.env"
    claude.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-SECRET\n")
    claude.chmod(0o600)
    facts = host_facts.collect(runner({
        "docker exec company-api": (0, SHA + "\n"), "docker exec company-worker": (0, SHA + "\n"),
        "systemctl is-active company-coder-launch.timer": (0, "active\n"),
        "docker image inspect": (0, "[]"),
        "docker inspect -f {{.State.Running}} company-egress": (0, "true\n"),
        'docker inspect -f {{range .Mounts}}{{if eq .Destination "/home/worker/.codex"}}{{.Name}}{{end}}{{end}} company-reasoner':
            (0, "company-worker-codex\n"),
        "docker network inspect": (0, '{"Name": "company-coder-net", "Internal": true}'),
        "docker run": (0, '{"loggedIn": true}')}), config=cfg, claude_env=claude,
        owner=os.getuid())
    assert facts == {**HOST} | {"claude_env_ok": True, "claude_auth_ok": True}
    assert "SECRET" not in json.dumps(facts)
    nothing = host_facts.collect(runner({}), config=tmp_path / "none", claude_env=tmp_path / "none", owner=0)
    assert nothing["api_sha"] is None and nothing["coder_image_present"] is None
    assert nothing["launcher_config_ok"] is False and nothing["claude_env_ok"] is False
    assert nothing["checks_command_fingerprints"] is None


@pytest.mark.parametrize("command", [
    ["true"], ["python", "-m", "pytest", "-q", "tests/test_packaging.py"],
    ["python", "-m", "pytest", "-q "]])
def test_host_checks_command_must_run_the_complete_supported_suite(tmp_path, command):
    cfg = tmp_path / "launcher.json"
    cfg.write_text(json.dumps({"coder_image": IMAGE, "checks_images": {"sez-company": IMAGE},
                               "checks_commands": {"sez-company": command}}))
    facts = host_facts.collect(runner({}), config=cfg, claude_env=tmp_path / "missing")
    report = pf.evaluate(**world(tmp_path, host={**HOST,
        "checks_command_fingerprints": facts["checks_command_fingerprints"]}))
    assert status(report, "checks_command") == "FAIL"
    assert report["ready"] is False
    assert command not in facts.values()


@pytest.mark.parametrize("commands", [None, [], {}, {"sez-company": []},
    {"sez-company": "python -m pytest -q"}, {"sez-company": ["python", None]},
    {"sez-company": ["python", ""]}, {"sez-company": ["python", "\0secret"]}])
def test_unobservable_or_malformed_launcher_command_never_proves_readiness(tmp_path, commands):
    cfg = tmp_path / "launcher.json"
    cfg.write_text(json.dumps({"coder_image": IMAGE, "checks_images": {"sez-company": IMAGE},
                               "checks_commands": commands}))
    facts = host_facts.collect(runner({}), config=cfg, claude_env=tmp_path / "missing")
    report = pf.evaluate(**world(tmp_path, host={**HOST,
        "checks_command_fingerprints": facts["checks_command_fingerprints"]}))
    assert status(report, "checks_command") == "UNKNOWN"
    assert report["ready"] is False
    assert "secret" not in json.dumps(facts)


def test_collector_requires_the_reasoner_to_mount_the_persistent_reviewer_volume():
    expected = 'docker inspect -f {{range .Mounts}}{{if eq .Destination "/home/worker/.codex"}}{{.Name}}{{end}}{{end}} company-reasoner'
    assert host_facts.codex_reviewer_volume_mounted(runner({expected: (0, "company-worker-codex\n")})) is True
    assert host_facts.codex_reviewer_volume_mounted(runner({expected: (0, "company-coder-codex\n")})) is False
    assert host_facts.codex_reviewer_volume_mounted(runner({})) is None


def test_collector_fails_a_logged_out_claude_status_without_exposing_its_output(tmp_path):
    cfg = tmp_path / "launcher.json"
    cfg.write_text(json.dumps({"coder_image": IMAGE, "checks_images": {"sez-company": IMAGE}}))
    assert host_facts.claude_auth_ok(runner({"docker run": (0, '{"loggedIn": false}')}), cfg, tmp_path / "x") is False
    assert host_facts.claude_auth_ok(runner({"docker run": (127, "")}), cfg, tmp_path / "x") is None


def test_collector_rejects_loose_or_metered_claude_env(tmp_path):
    claude = tmp_path / "claude.env"
    claude.write_text("ANTHROPIC_API_KEY=x\n")
    claude.chmod(0o600)
    assert host_facts.claude_env_ok(claude, os.getuid()) is False
    claude.write_text("CLAUDE_CODE_OAUTH_TOKEN=x\n")
    claude.chmod(0o644)
    assert host_facts.claude_env_ok(claude, os.getuid()) is False
    claude.chmod(0o600)
    assert host_facts.claude_env_ok(claude, os.getuid() + 1) is False
    assert host_facts.claude_env_ok(claude, os.getuid()) is True


# --------------------------------------------------------------------------- the worker's GitHub identity

def answering_user_with(response):
    def run(argv, **kwargs):
        return response(argv) if argv[-1] == "user" else readable(argv, **kwargs)
    return run


def test_a_worker_holding_the_founders_token_is_never_ready(tmp_path):
    # Break caught: preflight never asked whose token the worker holds; the founder's token passed.
    report = pf.evaluate(**world(tmp_path, run=answering_user_with(lambda a: whoami("SemihBugraSezer"))))
    assert status(report, "worker_identity") == "FAIL" and report["ready"] is False


@pytest.mark.parametrize("response", [
    lambda a: SimpleNamespace(returncode=1, stdout=""),                       # gh failed or token rejected
    lambda a: SimpleNamespace(returncode=0, stdout="HTTP/2 200\r\n\r\n{}"),   # no login in the answer
])
def test_an_unknown_worker_identity_is_never_ready(tmp_path, response):
    report = pf.evaluate(**world(tmp_path, run=answering_user_with(response)))
    assert status(report, "worker_identity") == "UNKNOWN" and report["ready"] is False


def test_the_agent_identity_with_a_read_only_upstream_role_passes(tmp_path):
    report = pf.evaluate(**world(tmp_path))
    check = next(c for c in report["checks"] if c["name"] == "worker_identity")
    assert check["status"] == "PASS" and report["ready"] is True
    assert "seerxo-agent" in check["detail"] and "read-only on seerxo/seerxo-os" in check["detail"]
    assert "x" not in check["detail"].split()                                 # the token value is never echoed


def answering_role_with(response):
    def run(argv, **kwargs):
        return response(argv) if argv[-1] == "repos/seerxo/seerxo-os" else readable(argv, **kwargs)
    return run


@pytest.mark.parametrize("granted", [{"push": True}, {"maintain": True}, {"admin": True, "push": True}])
def test_an_agent_later_granted_upstream_write_is_never_ready(tmp_path, granted):
    # Break caught (#192): "not the founder" alone passed, so seerxo-agent with Write could merge upstream.
    report = pf.evaluate(**world(tmp_path, run=answering_role_with(lambda a: role(**granted))))
    assert status(report, "worker_identity") == "FAIL" and report["ready"] is False


@pytest.mark.parametrize("response, expected", [
    (lambda a: SimpleNamespace(returncode=1, stdout="", stderr="HTTP 401 secret"), "UNKNOWN"),
    (lambda a: SimpleNamespace(returncode=1, stdout="", stderr="HTTP 403 secret"), "UNKNOWN"),
    (lambda a: SimpleNamespace(returncode=0, stdout='{"full_name": "seerxo/seerxo-os"}'), "UNKNOWN"),
    (lambda a: SimpleNamespace(returncode=0, stdout="not json: secret"), "FAIL"),
    (lambda a: role(push="false"), "FAIL"),
    (lambda a: SimpleNamespace(returncode=0, stdout='{"full_name": "seerxo/seerxo-os", "permissions": {"pull": true}}'),
     "FAIL"),
    (lambda a: SimpleNamespace(returncode=0, stdout=json.dumps(
        {"full_name": "other/repo", "permissions": READ_ONLY})), "FAIL"),            # wrong repository
    (lambda a: role(pull=False), "FAIL"),
])
def test_an_unproven_upstream_role_is_never_ready(tmp_path, response, expected):
    report = pf.evaluate(**world(tmp_path, run=answering_role_with(response)))
    assert status(report, "worker_identity") == expected and report["ready"] is False
    assert "secret" not in json.dumps(report)


def test_a_classic_repo_scope_is_bounded_by_the_verified_read_role(tmp_path):
    # The deployed worker token is classic `repo`; GitHub's Read role, not the scope, decides what it can do.
    run = lambda argv, **k: whoami("seerxo-agent", "repo") if argv[-1] == "user" else readable(argv, **k)
    report = pf.evaluate(**world(tmp_path, run=run))
    assert status(report, "worker_identity") == "PASS" and report["ready"] is True


def test_no_token_never_asks_github_who_it_is(tmp_path):
    calls = []
    report = pf.evaluate(**world(tmp_path, environ={"COMPANY_OS_GIT_SHA": SHA},
                                 run=lambda argv, **k: calls.append(argv)))
    assert calls == [] and status(report, "worker_identity") == "UNKNOWN"


def _launcher():
    import importlib.machinery
    path = Path(__file__).parents[1] / "scripts" / "company-coder-launch"
    loader = importlib.machinery.SourceFileLoader("company_coder_launch", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_host_facts_and_the_launcher_share_one_config_contract():
    # Review finding: host facts passed a launcher.json the launcher refuses, so preflight said ready while
    # every launch exited. The duplicated constants must stay equal.
    launcher = _launcher()
    for name in ("CODER_NETWORK", "CODER_PROXY", "CHECKS_COMMAND", "CONFIG_KEYS"):
        assert getattr(host_facts, name) == getattr(launcher, name), name


@pytest.mark.parametrize("over", [{"network": "bridge"}, {"proxy": "http://evil:8888"}, {"extra": 1},
                                  {"checks_commands": {"sez-company": ["sh", "-c", "true"]}}])
def test_host_facts_refuse_a_config_the_launcher_would_refuse(tmp_path, over):
    cfg = tmp_path / "launcher.json"
    cfg.write_text(json.dumps({**LAUNCHER_CFG, **over}))
    internal = runner({"docker network inspect": (0, '{"Internal": true}')})
    assert host_facts.collect(internal, config=cfg, claude_env=tmp_path / "x")["launcher_config_ok"] is False


@pytest.mark.parametrize("answer, expected", [
    ((0, '{"Internal": true}'), True), ((0, '{"Internal": false}'), False), ((1, ""), False),
    ((0, "garbage"), False), ((127, ""), None)])
def test_host_facts_report_whether_the_coder_network_is_really_internal(tmp_path, answer, expected):
    cfg = tmp_path / "launcher.json"
    cfg.write_text(json.dumps(LAUNCHER_CFG))
    facts = host_facts.collect(runner({"docker network inspect": answer}), config=cfg, claude_env=tmp_path / "x")
    assert facts["launcher_config_ok"] is expected
