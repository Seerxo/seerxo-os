"""Stage C may open PRs; it must not merge. Code can pin the inventory of merge-capable call sites and
the worker token's identity and its upstream role as GitHub reports it. Anything unproven stays UNKNOWN."""
import json
import re
import runpy
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from app import stage_c_credential_boundary as cb

ROOT = Path(__file__).resolve().parents[1]
# Literal merge-capable calls; this inventory cannot prove absence of computed runtime calls.
MERGE_CAPABLE = re.compile(
    r'''["']pr["']\s*,\s*["'](merge|ready|review)["']|gh pr (merge|ready|review)|/pulls/[^"\s]*/(merge|reviews)\b|'''
    r'mergePullRequest|markPullRequestReadyForReview|enablePullRequestAutoMerge|addPullRequestReview|'
    r'--admin\b|--auto\b.*merge|push[^\n]*\b(origin|--force)[^\n]*\bmain\b')
# The only file that may merge: the founder-run gate. Tests and docs describe forbidden calls.
ALLOWED = {"scripts/merge-pr"}


def _tracked(*roots):
    if (ROOT / ".git").exists() or (ROOT / ".git").is_symlink():
        out = subprocess.run(["git", "ls-files", *roots], cwd=ROOT, capture_output=True, text=True, check=True)
        return [p for p in out.stdout.splitlines() if (ROOT / p).is_file()]
    # Stage C checks have exported sources, not Git metadata; inspect the same fixed roots.
    files = []
    for root in roots:
        base = ROOT / root
        assert not base.is_symlink(), f"source inventory refuses symlink: {root}"
        for path in base.rglob("*"):
            assert not path.is_symlink(), f"source inventory refuses symlink: {path.relative_to(ROOT)}"
            rel = path.relative_to(ROOT)
            if (path.is_file() and not {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}.intersection(rel.parts)
                    and path.suffix not in {".pyc", ".pyo"} and path.name != ".DS_Store"):
                files.append(str(rel))
    return sorted(files)


def test_only_the_founder_run_gate_can_merge():
    offenders = []
    for rel in _tracked("app", "scripts", "agents", ".github", ".circleci", "skills"):
        if rel in ALLOWED or rel.endswith((".md", ".png", ".ico", ".woff2")):
            continue
        try:
            text = (ROOT / rel).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        offenders += [f"{rel}: {m.group(0)}" for m in MERGE_CAPABLE.finditer(text)]
    assert not offenders, "merge-capable call outside scripts/merge-pr: " + "; ".join(offenders)


def test_actions_workflows_hold_no_write_token_for_pull_requests_merge():
    for rel in _tracked(".github/workflows"):
        workflow = yaml.safe_load((ROOT / rel).read_text())
        assert "permissions" in workflow, rel
        for scope in [workflow, *(workflow.get("jobs") or {}).values()]:
            permissions = scope.get("permissions", workflow["permissions"])
            assert permissions == "read-all" or isinstance(permissions, dict), rel
            if isinstance(permissions, dict):
                assert all(value in ("read", "write", "none") for value in permissions.values()), rel
                assert not any(permissions.get(p) == "write"
                               for p in ("contents", "administration", "actions")), rel


def test_coder_sandbox_environments_never_inherit_the_github_token():
    launcher = runpy.run_path(str(ROOT / "scripts/company-coder-launch"))
    image = "sha256:" + "a" * 64
    cfg = {"coder_image": image, "checks_images": {"sez-company": image},
           "checks_commands": {"sez-company": ["python", "-m", "pytest", "-q"]},
           "network": "company-coder-net", "proxy": "http://company-egress:8888"}
    with tempfile.TemporaryDirectory() as directory:
        env = Path(directory) / "claude.env"
        env.write_text("CLAUDE_CODE_OAUTH_TOKEN=boundary-test-only\n")
        with patch.dict(launcher["docker_argv"].__globals__, {"CLAUDE_ENV": env}), patch.dict(
                "os.environ", {"GH_TOKEN": "boundary-gh-sentinel", "GITHUB_TOKEN": "boundary-github-sentinel"}):
            for request in ({"kind": "code", "provider": "claude"}, {"kind": "checks", "repo": "sez-company"}):
                argv = launcher["docker_argv"](request, cfg, Path(directory) / "workspace", "boundary-test")
                environments = [Path(argv[i + 1] if arg == "--env-file" else arg.split("=", 1)[1]).read_text()
                                for i, arg in enumerate(argv) if arg == "--env-file" or arg.startswith("--env-file=")]
                text = "\n".join([*argv, *environments])
                assert not any(value in text for value in (
                    "GH_TOKEN", "GITHUB_TOKEN", "boundary-gh-sentinel", "boundary-github-sentinel"))


@pytest.mark.parametrize("command", [
    "subprocess.run(['gh', 'pr', 'merge', '1'])",
    'subprocess.run(["gh", "pr", "merge", "1"])',
    "subprocess.run(['gh',\n 'pr',\n 'review', '--approve'])",
])
@pytest.mark.parametrize("rel", ["merge.py", "app/latest.py", "app/docs.py", ".github/workflows/test.yml"])
def test_merge_inventory_detects_literal_argv_with_either_quote_style(tmp_path, monkeypatch, command, rel):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(command)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    monkeypatch.setitem(globals(), "_tracked", lambda *roots: [rel])
    with pytest.raises(AssertionError, match="merge-capable call"):
        test_only_the_founder_run_gate_can_merge()


@pytest.mark.parametrize("permissions", [
    "permissions: write-all\njobs: {}",
    "permissions: {contents: write}\njobs: {}",
    "permissions: {actions: write}\njobs: {}",
    "permissions: {administration: write}\njobs: {}",
    "permissions: read-all\njobs:\n  ci:\n    permissions: write-all",
    "permissions: read-all\njobs:\n  ci:\n    permissions: {contents: write}",
])
def test_workflow_inventory_detects_shorthand_and_job_write_permissions(tmp_path, monkeypatch, permissions):
    (tmp_path / "workflow.yml").write_text(permissions)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    monkeypatch.setitem(globals(), "_tracked", lambda *roots: ["workflow.yml"])
    with pytest.raises(AssertionError, match="workflow.yml"):
        test_actions_workflows_hold_no_write_token_for_pull_requests_merge()


@pytest.mark.parametrize("permissions", [
    "", "permissions: null", "permissions: unknown", "permissions: []", "permissions: true",
    "permissions: 1", "permissions: {contents: unknown}",
])
def test_workflow_inventory_rejects_unknown_permission_defaults(tmp_path, monkeypatch, permissions):
    (tmp_path / "workflow.yml").write_text(permissions + "\njobs:\n  ci:\n    runs-on: ubuntu-latest\n")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    monkeypatch.setitem(globals(), "_tracked", lambda *roots: ["workflow.yml"])
    with pytest.raises(AssertionError, match="workflow.yml"):
        test_actions_workflows_hold_no_write_token_for_pull_requests_merge()


@pytest.mark.parametrize("permissions", ["read-all", "{}"])
def test_workflow_jobs_can_inherit_explicit_read_or_empty_permissions(tmp_path, monkeypatch, permissions):
    (tmp_path / "workflow.yml").write_text(f"permissions: {permissions}\njobs:\n  ci:\n    runs-on: ubuntu-latest\n")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    monkeypatch.setitem(globals(), "_tracked", lambda *roots: ["workflow.yml"])
    test_actions_workflows_hold_no_write_token_for_pull_requests_merge()


@pytest.mark.parametrize("kind", ["code", "checks"])
@pytest.mark.parametrize("variable", ["GH_TOKEN", "GITHUB_TOKEN"])
@pytest.mark.parametrize("delivery", ["variable", "env_file_equals", "env_file_separate"])
def test_github_token_forwarding_in_the_actual_launcher_is_detected(tmp_path, monkeypatch, kind, variable, delivery):
    launcher = (ROOT / "scripts/company-coder-launch").read_text()
    assert launcher.count('argv = ["docker", "run",') == 1
    leaked_env = tmp_path / "config.env"
    leaked_env.write_text(f"{variable}=boundary-gh-sentinel\n")
    forwarded = {"variable": [f"--env={variable}"], "env_file_equals": [f"--env-file={leaked_env}"],
                 "env_file_separate": ["--env-file", str(leaked_env)]}[delivery]
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/company-coder-launch").write_text(
        launcher.replace('argv = ["docker", "run",',
                         f'argv = ["docker", "run", *({forwarded!r} '
                         f'if req["kind"] == "{kind}" else []),', 1))
    (tmp_path / "app").mkdir()
    for name in ("coder_source.py", "coder_diff.py"):
        (tmp_path / "app" / name).write_text("")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    with pytest.raises(AssertionError):
        test_coder_sandbox_environments_never_inherit_the_github_token()


def test_git_free_export_is_still_inventoried(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/autonomous.py").write_text("subprocess.run(['gh', 'pr', 'merge', '1'])")
    (tmp_path / "app/__pycache__").mkdir()
    (tmp_path / "app/__pycache__/ignored.pyc").write_bytes(b"cache")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    assert _tracked("app", "scripts") == ["app/autonomous.py"]
    with pytest.raises(AssertionError, match="merge-capable call"):
        test_only_the_founder_run_gate_can_merge()


@pytest.mark.parametrize("directory", [False, True])
def test_git_free_export_refuses_symlinks(tmp_path, monkeypatch, directory):
    (tmp_path / "app").mkdir()
    target = tmp_path / "external"
    target.mkdir() if directory else target.write_text("outside")
    (tmp_path / "app/link").symlink_to(target, target_is_directory=directory)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    with pytest.raises(AssertionError, match="symlink"):
        _tracked("app")


def test_git_command_failure_does_not_fall_back_to_an_export_scan(tmp_path, monkeypatch):
    (tmp_path / ".git").write_text("gitdir: unavailable")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        _tracked("app")


# --- credential boundary probe: UNKNOWN is never a pass --------------------------------------------

def gh(stdout="", rc=0):
    return lambda argv, **kw: SimpleNamespace(returncode=rc, stdout=stdout, stderr="")


def response(login, scopes=None):
    head = "HTTP/2.0 200 OK\r\n" + (f"X-Oauth-Scopes: {scopes}\r\n" if scopes is not None else "")
    return head + f'\r\n{{"login": "{login}"}}'


def status(report, name):
    return next(c["status"] for c in report["checks"] if c["name"] == name)


def test_a_worker_token_that_is_the_founder_fails():
    report = cb.evaluate({"GH_TOKEN": "x"}, gh(response("SemihBugraSezer")))
    assert status(report, "worker_token_not_founder") == "FAIL" and not report["separated"]


def test_missing_token_fails():
    assert status(cb.evaluate({}, gh(response("bot"))), "worker_token_present") == "FAIL"


def test_unreachable_github_is_unknown_not_pass():
    for run in (gh("", rc=1), gh("garbage")):
        assert status(cb.evaluate({"GH_TOKEN": "x"}, run), "worker_token_not_founder") == "UNKNOWN"


def test_classic_token_with_repo_scope_fails():
    report = cb.evaluate({"GH_TOKEN": "x"}, gh(response("bot", "repo, read:org")))
    assert status(report, "worker_token_scopes") == "FAIL"


def test_fine_grained_token_permissions_are_unknown():
    report = cb.evaluate({"GH_TOKEN": "x"}, gh(response("bot")))
    assert status(report, "worker_token_not_founder") == "PASS"
    assert status(report, "worker_token_scopes") == "UNKNOWN"


def test_inability_to_merge_is_never_reported_as_proven():
    report = cb.evaluate({"GH_TOKEN": "x"}, gh(response("bot")))
    assert status(report, "worker_token_cannot_merge") == "UNKNOWN" and report["separated"] is False


def role_run(login="seerxo-agent", scopes="repo", role_stdout=None, role_rc=0):
    """`gh api -i user` answers the identity; `gh api repos/seerxo/os` answers the upstream role."""
    perms = {"admin": False, "maintain": False, "push": False, "triage": False, "pull": True}
    body = role_stdout if role_stdout is not None else json.dumps({"full_name": "seerxo/os", "permissions": perms})

    def run(argv, **kw):
        if argv == ["gh", "api", "--hostname", "github.com", "-i", "user"]:
            return SimpleNamespace(returncode=0, stdout=response(login, scopes), stderr="")
        assert argv == ["gh", "api", "--hostname", "github.com", "repos/seerxo/os"]
        return SimpleNamespace(returncode=role_rc, stdout=body, stderr="secret")
    return run


def test_a_verified_read_only_upstream_role_proves_the_token_cannot_merge():
    report = cb.evaluate({"GH_TOKEN": "x"}, role_run())
    assert status(report, "worker_upstream_role") == "PASS"
    assert status(report, "worker_token_cannot_merge") == "PASS"
    assert status(report, "worker_token_scopes") == "FAIL"        # classic `repo` is still reported, not hidden


@pytest.mark.parametrize("perms", [{"push": True}, {"maintain": True}, {"admin": True}])
def test_upstream_write_or_higher_fails_and_merge_stays_unproven(perms):
    base = {"admin": False, "maintain": False, "push": False, "pull": True}
    report = cb.evaluate({"GH_TOKEN": "x"},
                         role_run(role_stdout=json.dumps({"full_name": "seerxo/os", "permissions": base | perms})))
    assert status(report, "worker_upstream_role") == "FAIL"
    assert status(report, "worker_token_cannot_merge") == "UNKNOWN" and not report["separated"]


@pytest.mark.parametrize("kwargs, expected", [
    ({"role_rc": 1}, "UNKNOWN"),                                             # 401/403/404/network
    ({"role_stdout": '{"full_name": "seerxo/os"}'}, "UNKNOWN"),              # no permissions object
    ({"role_stdout": "garbage secret"}, "FAIL"),
    ({"role_stdout": '{"full_name": "seerxo/os", "permissions": []}'}, "FAIL"),
    ({"role_stdout": '{"full_name": "seerxo/os", "permissions": {"admin": 0, "maintain": 0, "push": 0, '
                     '"pull": 1}}'}, "FAIL"),
    # Read-only, but for another repository: a rename redirect or a wrong answer proves nothing about seerxo/os.
    ({"role_stdout": '{"full_name": "other/repo", "permissions": {"admin": false, "maintain": false, '
                     '"push": false, "pull": true}}'}, "FAIL"),
    ({"role_stdout": '{"permissions": {"admin": false, "maintain": false, "push": false, "pull": true}}'}, "FAIL"),
])
def test_an_unproven_upstream_role_never_passes_and_never_echoes(kwargs, expected):
    report = cb.evaluate({"GH_TOKEN": "x"}, role_run(**kwargs))
    assert status(report, "worker_upstream_role") == expected
    assert status(report, "worker_token_cannot_merge") == "UNKNOWN"
    assert "secret" not in json.dumps(report)


def test_no_token_makes_no_role_read():
    report = cb.evaluate({}, lambda *a, **k: pytest.fail("no token must not query GitHub"))
    assert status(report, "worker_upstream_role") == "UNKNOWN"


def test_the_owner_case_github_returns_is_the_same_repository():
    body = json.dumps({"full_name": "Seerxo/os", "permissions": {"admin": False, "maintain": False,
                                                                 "push": False, "pull": True}})
    assert status(cb.evaluate({"GH_TOKEN": "x"}, role_run(role_stdout=body)), "worker_upstream_role") == "PASS"


def test_gh_host_cannot_redirect_the_probes():
    # Break caught: GH_HOST pointed the probes at another host while git kept sending GH_TOKEN to github.com.
    seen = []
    run = role_run()
    cb.evaluate({"GH_TOKEN": "x", "GH_HOST": "ghe.example"}, lambda argv, **kw: seen.append(argv) or run(argv, **kw))
    assert all(argv[2:4] == ["--hostname", "github.com"] for argv in seen) and len(seen) == 2
