"""P0 trust-boundary inventory: what a repository change can and cannot reach. Run with pytest.

These tests read the repository. Anyone who can push to it can also edit them, so they are a tripwire and an
inventory, NOT a security boundary (docs/security/THREAT_MODEL.md). Two kinds:

- Invariants that hold today. A change that widens the exposure (a second workflow touching the deploy secrets,
  a privileged trigger, an unpinned action, a new write scope) fails here and shows up in review.
- Known P0 gaps, marked xfail(strict=True). Each states the property the release must eventually have. When a
  change really closes the gap the test passes, strict xfail turns that into a failure, and the marker has to be
  removed on purpose: the gap cannot be closed or reopened silently.
"""
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.y*ml"))
# Every secret a workflow may name, and where. Adding one is a reviewed change to this table.
SECRET_ALLOWLIST: dict[str, set[str]] = {}                       # public repository: no workflow names any secret
# Write scopes a workflow's GITHUB_TOKEN may hold. None: every workflow is read-only; nothing may write code.
WRITE_ALLOWLIST: dict[str, set[str]] = {}
# Triggers that run with secrets or write access on behalf of code the workflow did not choose.
PRIVILEGED_TRIGGERS = {"pull_request_target", "workflow_run", "issue_comment", "issues", "discussion_comment",
                       "pull_request_review_comment", "repository_dispatch"}
SECRET_REF = re.compile(r"""secrets(?:\.|\[\s*['"])([A-Za-z0-9_]+)""")   # secrets.X and secrets['X']
PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
P0 = "known gap (docs/security/THREAT_MODEL.md); remove this marker only with the change that closes it"


def load(path: Path):
    text = path.read_text()
    workflow = yaml.safe_load(text)
    return text, workflow, workflow.get("on", workflow.get(True))   # YAML 1.1 reads the bare key `on` as True


def module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def jobs(workflow):
    return (workflow.get("jobs") or {}).items()


def runs_on(job) -> list[str]:
    value = job.get("runs-on", [])
    return [value] if isinstance(value, str) else list(value)


def secret_names(node) -> set[str]:
    return set(SECRET_REF.findall(yaml.safe_dump(node)))


CI_TEXT, CI, _ = load(ROOT / ".github/workflows/ci.yml")


# --------------------------------------------------------------------------- invariants that hold today

def test_the_workflow_set_is_known():
    # A new workflow file is new code that runs with this repository's secrets: it must be added here on purpose.
    assert {p.name for p in WORKFLOWS} == {"ci.yml", "dependency-scan.yml", "frontend.yml",
                                           "prowler.yml", "python-tests.yml", "secret-scan.yml", "workflow-lint.yml"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_secrets_are_named_only_where_allowed(path):
    text, workflow, _ = load(path)
    assert "secrets: inherit" not in text and "toJSON(secrets)" not in text.replace(" ", "")
    assert not re.search(r"""secrets\s*\[\s*[^'"\s]""", text), path.name   # a computed name escapes this table
    assert set(SECRET_REF.findall(text)) <= SECRET_ALLOWLIST.get(path.name, set()), path.name


def test_the_deploy_workflow_has_no_deploy_job_and_never_runs_the_driver():
    assert "deploy" not in dict(jobs(CI))
    for name, job in jobs(CI):
        assert not secret_names(job), name
        for step in job.get("steps", []):
            run = str(step.get("run", ""))
            assert "deploy-company-os.sh" not in run and "ssh" not in run.split(), f"{name}: {run[:60]}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_runs_on_a_github_hosted_runner(path):
    # Public repository: code from a pull request must never run on a persistent runner connected to company
    # infrastructure. Every job uses a fresh GitHub-hosted VM, and no workflow may name a self-hosted label.
    text, workflow, _ = load(path)
    for name, job in jobs(workflow):
        assert job.get("runs-on") == "ubuntu-24.04", f"{path.name}:{name}"
    assert "self-hosted" not in text.lower() and "sez-deploy" not in text, path.name


def test_backend_database_is_disposable_loopback_and_bounded():
    _, workflow, _ = load(ROOT / ".github/workflows/python-tests.yml")
    job = workflow["jobs"]["python-tests"]
    postgres = job["services"]["postgres"]
    assert postgres["image"] == "postgres:18-alpine"
    assert postgres["env"]["POSTGRES_DB"] == "company_external_release_test"
    assert postgres["ports"] == ["127.0.0.1::5432"]
    for limit in ("--cpus=1", "--memory=1g", "--pids-limit=128"):
        assert limit in postgres["options"]
    wait = next(step for step in job["steps"] if step.get("name") == "Wait for test PostgreSQL")
    assert "${{ job.services.postgres.ports['5432'] }}" in wait["env"]["EXTERNAL_RELEASE_TEST_DATABASE_URL"]
    assert '"$GITHUB_ENV"' in wait["run"]


def test_real_release_recovery_runs_on_pr_and_main_ci():
    _, workflow, triggers = load(ROOT / ".github/workflows/python-tests.yml")
    assert triggers == {"pull_request": {"branches": ["main"]}, "push": {"branches": ["main"]}}
    job = workflow["jobs"]["python-tests"]
    rehearsal = next((step for step in job["steps"]
                      if step.get("run") == "python -u -m pytest -q -s tests/test_release_recovery_docker.py"), None)
    assert rehearsal is not None
    assert "if" not in rehearsal
    assert rehearsal["env"]["SEERXO_REAL_RECOVERY"] == "1"


def test_ci_has_no_provider_action_dependency():
    assert not (ROOT / ".github/actionlint.yaml").exists(), "custom runner labels are no longer needed"
    for path in WORKFLOWS:
        text = path.read_text().lower()
        assert "blacksmith" not in text and "sengi" not in text, path.name


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_privileged_trigger(path):
    _, _, on = load(path)
    triggers = {on} if isinstance(on, str) else set(on or ())
    assert not triggers & PRIVILEGED_TRIGGERS, path.name


def test_the_deploy_workflow_runs_only_on_a_dispatch():
    _, _, on = load(ROOT / ".github/workflows/ci.yml")
    assert set(on) == {"workflow_dispatch"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_full_commit(path):
    _, workflow, _ = load(path)
    for name, job in jobs(workflow):
        if "uses" in job:                                         # reusable workflow call
            assert job["uses"].startswith("./") or PINNED.match(job["uses"]), f"{path.name}:{name}"
        for step in job.get("steps", []):
            uses = step.get("uses")
            if uses and not uses.startswith("./"):
                assert PINNED.match(uses), f"{path.name}:{name}: {uses}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_write_scopes_are_only_the_allowed_ones(path):
    _, workflow, _ = load(path)
    scopes = [workflow.get("permissions")] + [job.get("permissions") for _, job in jobs(workflow)]
    granted = set()
    for scope in scopes:
        if scope is None:
            continue
        assert scope != "write-all", path.name
        if isinstance(scope, dict):
            granted |= {k for k, v in scope.items() if v == "write"}
    assert granted <= WRITE_ALLOWLIST.get(path.name, set()), path.name
    assert workflow.get("permissions") is not None, f"{path.name}: default token scope is implicit"


def test_checkouts_in_the_deploy_workflow_keep_no_credentials():
    for name, job in jobs(CI):
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step.get("with", {}).get("persist-credentials") is False, name


# --------------------------------------------------------------------------- known P0 gaps (strict xfail)

def test_no_repository_workflow_names_a_deploy_secret():
    # Strict xfail removed for the B6 source cut-over: the code condition is met, no workflow names any
    # COMPANY_OS_DEPLOY_* secret. That does NOT close P0-A. Repository secrets reach any workflow a branch push or
    # same-repo PR runs, including one a writer adds, so the secrets must also be deleted on GitHub and the key
    # revoked on the host; this repository cannot observe either (an owner-run acceptance step). This test only stops
    # the secrets from being named again.
    for path in WORKFLOWS:
        text = path.read_text()
        assert "COMPANY_OS_DEPLOY" not in text, path.name   # matches cutover acceptance A4, including comments


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=P0)
def test_deploy_authorization_does_not_rest_on_the_github_actor_alone():
    # The readiness workflow's only authorization is a dispatch by the founder login. Whoever drives that login (or a
    # token for it) can dispatch it, so it must never be the release authority: production requires an approval
    # GitHub cannot mint (a founder signature bound to the SHA, verified on the production host).
    authz = module("deploy_authorization", "scripts/deploy-authorization.py")
    allowed, _ = authz.evaluate(actor="semihbugrasezer", triggering_actor="semihbugrasezer",
                                founder="semihbugrasezer", sha="a" * 40, allow_migration_to="", sha_on_main=True)
    assert not allowed, "a GitHub login alone authorized a production deploy"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=P0)
def test_provenance_requires_an_approval_independent_of_the_merger():
    # deploy-provenance treats "merged by the founder login" as approval, so a merge without any independent
    # approval would still count as release provenance.
    prov = module("deploy_provenance", "scripts/deploy-provenance.py")
    sha = "b" * 40
    unreviewed = {"state": "closed", "merged": True, "base": {"ref": "main"}, "merged_by": {"login": "semihbugrasezer"},
                  "merge_commit_sha": sha}
    allowed, _ = prov.evaluate([unreviewed], sha, "semihbugrasezer")
    assert not allowed, "a founder-login merge with no independent approval was accepted as release provenance"


def test_the_production_host_verifies_the_release_itself():
    # The final gate must run on the host, outside anything a repository change controls. Step 8: on a bootstrapped
    # host the root-side driver applies only what release_host verified (ledger pending_sha + its staged RELEASE_DIR;
    # tests/test_release_acceptance.py). The string is payload-controlled: a tripwire; release_host is the gate.
    source = (ROOT / "scripts/deploy-company-os.sh").read_text()
    remote = source[source.index("<<'REMOTE'\n"):source.index("\nREMOTE\n")]   # the part that runs on the host
    assert "verify-release-manifest" in remote, "the host applies a release without verifying a founder approval"
