import json
from types import SimpleNamespace

import pytest

from app import repo_guardian_github as github

REPO = "seerxo/seerxo-os"
SHA = "a" * 40


def api_fixture(monkeypatch, runs=None):
    calls = []
    def api(path, expression):
        calls.append(path)
        if path == f"repos/{REPO}":
            return {"default_branch": "main"}
        if "/commits/" in path:
            return {"sha": SHA}
        if "/compare/" in path:
            return {"status": "ahead", "files": ["app/desk.py"], "total_commits": 1}
        return {"total_count": len(runs or []), "workflow_runs": runs or []}
    monkeypatch.setattr(github, "api", api)
    return calls


def workflow(conclusion="failure", attempt=1):
    return {"id": attempt, "workflow_id": 4, "name": "ci", "path": ".github/workflows/ci.yml",
            "head_sha": SHA, "head_branch": "main", "status": "completed", "conclusion": conclusion,
            "run_number": 1, "run_attempt": attempt, "event": "push"}


def test_collects_changed_scope_and_latest_attempt_only(monkeypatch):
    calls = api_fixture(monkeypatch, [workflow(), workflow("success", 2)])
    out = github.collect(REPO, "b" * 40, {".github/workflows/ci.yml"})
    assert out["changed_files"] == ["app/desk.py"] and out["diff_complete"]
    assert len(out["checks"]) == 1 and out["checks"][0]["conclusion"] == "success"
    assert out["checks_complete"] and len(calls) == 4


def test_no_main_ci_is_unknown_and_no_compare_without_change(monkeypatch):
    calls = api_fixture(monkeypatch)
    out = github.collect(REPO, SHA, {".github/workflows/ci.yml"})
    assert out["checks"] == [] and out["checks_complete"] is False
    assert not any("/compare/" in path for path in calls)


@pytest.mark.parametrize("total", [-1, 0, 2, True, 100])
def test_guardian_workflow_pages_must_be_complete_and_bounded(monkeypatch, total):
    rows = [workflow("success")]
    if total == 100:
        rows += [{**rows[0], "path": ".github/workflows/other.yml"} for _ in range(99)]
    monkeypatch.setattr(github, "api", lambda path, expression: {"sha": SHA} if "/commits/" in path else {
        "total_count": total, "workflow_runs": rows})
    assert github.collect(REPO, SHA, {".github/workflows/ci.yml"}, "main")["checks_complete"] is False


def test_unapproved_workflow_or_other_branch_is_not_evidence(monkeypatch):
    wrong = {**workflow(), "head_branch": "feature"}
    api_fixture(monkeypatch, [wrong, {**workflow(), "path": ".github/workflows/deploy.yml"}])
    assert github.collect(REPO, None, {".github/workflows/ci.yml"})["checks"] == []


def test_api_is_read_only_bounded_and_does_not_print_error_body(monkeypatch):
    def subprocess_run(argv, **kwargs):
        assert argv[:3] == ["gh", "api", "--method"] and argv[3] == "GET"
        assert kwargs["timeout"] == 20 and kwargs["capture_output"] is True
        return SimpleNamespace(returncode=1, stderr="secret error body", stdout="")
    monkeypatch.setattr(github.subprocess, "run", subprocess_run)
    with pytest.raises(RuntimeError, match="GitHub read failed") as error:
        github.api("repos/owner/repo", ".")
    assert "secret" not in str(error.value)


def test_disabled_policy_is_default_and_requires_literal_boolean(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text('enabled: "false"\nrepositories: []\n')
    with pytest.raises(ValueError):
        github.load_policy(path)


def test_invalid_checkpoint_is_rejected_before_network(monkeypatch):
    calls = api_fixture(monkeypatch)
    with pytest.raises(ValueError):
        github.collect(REPO, "not-a-sha", {".github/workflows/ci.yml"})
    assert calls == []


def test_tracked_branch_is_required_per_repository_and_used_instead_of_default(monkeypatch, tmp_path):
    path = tmp_path / "policy.yaml"
    base = "enabled: false\nrepositories:\n  - semihbugrasezer/youtube-auto\nworkflows:\n  - .github/workflows/ci.yml\n"
    path.write_text(base)
    # Break caught: Guardian silently watching GitHub's default branch (main) for youtube-auto.
    with pytest.raises(ValueError, match="tracked branch"):
        github.load_policy(path)
    path.write_text(base + "branches:\n  semihbugrasezer/youtube-auto: ../main\n")
    with pytest.raises(ValueError):
        github.load_policy(path)
    path.write_text(base + "branches:\n  semihbugrasezer/youtube-auto: server-master\n")
    policy = github.load_policy(path)
    assert github.tracked_branch(policy, "semihbugrasezer/youtube-auto") == "server-master"
    calls = api_fixture(monkeypatch)
    github.collect(REPO, None, {".github/workflows/ci.yml"}, "server-master")
    assert not any(c == f"repos/{REPO}" for c in calls)  # default_branch never asked
    assert any("/commits/server-master" in c for c in calls) and any("branch=server-master" in c for c in calls)


def test_repository_policy_in_git_names_a_branch_for_every_repository():
    from pathlib import Path
    policy = github.load_policy(Path(github.__file__).resolve().parents[1] / "agents/repository_guardian.yaml")
    assert policy["branches"] == {"seerxo/seerxo-os": "main"}


def test_merge_statuses_is_required_and_strictly_shaped():
    publisher = {"login": "circleci-app[bot]", "id": 127350680}
    contexts = ["ci/circleci: python-tests", "ci/circleci: frontend-tests"]
    assert github.merge_statuses({"status_publisher": publisher, "required_statuses": contexts}) == (
        publisher, tuple(contexts))
    refused = [
        {}, {"required_statuses": contexts},  # missing declaration
        {"status_publisher": publisher, "required_statuses": []},  # nothing required
        {"status_publisher": publisher, "required_statuses": contexts + [contexts[0]]},  # duplicate
        {"status_publisher": {"login": "circleci-app[bot]", "id": True}, "required_statuses": contexts},  # bool id
        {"status_publisher": {"login": "", "id": 127350680}, "required_statuses": contexts},
        {"status_publisher": {**publisher, "extra": 1}, "required_statuses": contexts},
        {"status_publisher": publisher, "required_statuses": "ci/circleci: python-tests"},
    ]
    for policy in refused:
        with pytest.raises(ValueError):
            github.merge_statuses(policy)


def test_checked_in_policy_requires_the_frontend_and_python_workflows():
    from pathlib import Path
    policy = github.load_policy(Path(__file__).resolve().parents[1] / "agents/repository_guardian.yaml")
    assert policy["workflows"] == [".github/workflows/frontend.yml", ".github/workflows/python-tests.yml"]
    # CircleCI does not report on Seerxo/seerxo-os: no commit status is required, so the workflows are the evidence.
    assert github.merge_statuses(policy) == ({"login": "circleci-app[bot]", "id": 127350680}, ())


def test_a_policy_with_neither_workflows_nor_statuses_is_refused():
    policy = {"status_publisher": {"login": "circleci-app[bot]", "id": 127350680}, "required_statuses": [],
              "workflows": []}
    with pytest.raises(ValueError, match="required workflows or required statuses"):
        github.merge_statuses(policy)


def test_frontend_tests_run_in_exactly_one_place():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert not (root / ".circleci").exists()            # the only CI is GitHub Actions (frontend.yml, python-tests.yml)
    frontend = (root / ".github/workflows/frontend.yml").read_text()
    for step in ("npm ci --prefix desk", "npm run build --prefix desk", "git diff --exit-code -- app/static",
                 "npm test --prefix desk", "npm run lint --prefix desk"):
        assert step in frontend


CIRCLE = {"login": "circleci-app[bot]", "id": 127350680}
CONTEXTS = ("ci/circleci: python-tests", "ci/circleci: frontend-tests")


def _status(context, state, id, creator=CIRCLE):
    return {"id": id, "context": context, "state": state, "creator": {"id": creator["id"], "login": creator["login"]}}


def statuses_fixture(monkeypatch, statuses):
    def api(path, expression):
        if path == f"repos/{REPO}":
            return {"default_branch": "main"}
        if "/statuses" in path:
            return statuses
        if "/commits/" in path:
            return {"sha": SHA}
        return {"total_count": 1, "workflow_runs": [workflow("success", 1)]}
    monkeypatch.setattr(github, "api", api)


def test_trusted_circleci_statuses_join_the_observation(monkeypatch):
    statuses_fixture(monkeypatch, [_status(CONTEXTS[0], "success", 1), _status(CONTEXTS[1], "success", 2)])
    out = github.collect(REPO, None, {".github/workflows/ci.yml"}, "main", publisher=CIRCLE, status_contexts=CONTEXTS)
    assert out["checks_complete"] is True
    status_checks = [c for c in out["checks"] if c["name"] in CONTEXTS]
    assert {c["id"] for c in status_checks} == {1, 2} and all(c["head_sha"] == SHA for c in status_checks)


def test_circleci_only_observation_never_requests_unused_actions_permission(monkeypatch):
    statuses_fixture(monkeypatch, [_status(CONTEXTS[0], "success", 1), _status(CONTEXTS[1], "success", 2)])
    read = github.api

    def api(path, expression):
        assert "/actions/" not in path
        return read(path, expression)

    monkeypatch.setattr(github, "api", api)
    result = github.collect(REPO, SHA, set(), "main", publisher=CIRCLE, status_contexts=CONTEXTS)
    assert result["checks_complete"] is True


def test_missing_pending_or_spoofed_statuses_keep_main_unknown(monkeypatch):
    cases = [
        [_status(CONTEXTS[0], "success", 1)],  # one required context missing
        [_status(CONTEXTS[0], "success", 1), _status(CONTEXTS[1], "pending", 2)],
        [_status(CONTEXTS[0], "success", 1), _status(CONTEXTS[1], "success", 2),
         _status(CONTEXTS[1], "failure", 3, creator={"id": 1, "login": "spoof"})],
    ]
    for statuses in cases:
        statuses_fixture(monkeypatch, statuses)
        out = github.collect(REPO, None, {".github/workflows/ci.yml"}, "main", publisher=CIRCLE,
                             status_contexts=CONTEXTS)
        assert out["checks_complete"] is False, statuses


def test_without_status_contexts_the_observation_is_unchanged(monkeypatch):
    api_fixture(monkeypatch, [workflow("success", 1)])
    out = github.collect(REPO, None, {".github/workflows/ci.yml"}, "main")
    assert out["checks_complete"] is True and all(c["id"] != 1 or c["name"] == "ci" for c in out["checks"])


def test_status_contexts_need_a_trusted_publisher(monkeypatch):
    api_fixture(monkeypatch, [workflow("success", 1)])
    with pytest.raises(ValueError):
        github.collect(REPO, None, {".github/workflows/ci.yml"}, "main", status_contexts=CONTEXTS)


def test_b5_publish_fork_is_optional_exact_and_canonical(tmp_path):
    path = tmp_path / "policy.yaml"
    base = ("enabled: false\nrepositories:\n  - seerxo/seerxo-os\nworkflows:\n  - .github/workflows/ci.yml\n"
            "branches:\n  seerxo/seerxo-os: main\n")
    path.write_text(base)
    assert github.publish_repository(github.load_policy(path), "seerxo/seerxo-os") == "seerxo/seerxo-os"   # unset: same repo
    path.write_text(base + "publish_forks:\n  seerxo/seerxo-os: seerxo-agent/seerxo-os\n")
    assert github.publish_repository(github.load_policy(path), "seerxo/seerxo-os") == "seerxo-agent/seerxo-os"
    for bad in ("  seerxo/seerxo-os: Seerxo-Agent/os\n", "  seerxo/seerxo-os: seerxo/seerxo-os\n", "  other/repo: seerxo-agent/seerxo-os\n",
                "  seerxo/seerxo-os: https://evil/x\n", "  seerxo/seerxo-os: [a/b]\n"):
        path.write_text(base + "publish_forks:\n" + bad)
        with pytest.raises(ValueError, match="publish_forks"):
            github.load_policy(path)


def test_b5_repository_policy_in_git_publishes_from_the_agent_fork():
    from pathlib import Path
    policy = github.load_policy(Path(github.__file__).resolve().parents[1] / "agents/repository_guardian.yaml")
    assert github.publish_repository(policy, "seerxo/seerxo-os") == "seerxo-agent/seerxo-os"


@pytest.mark.parametrize("declaration", [
    "publish_forks:\n  seerxo/seerxo-os: seerxo-agent/seerxo-os\npublish_forks:\n  seerxo/seerxo-os: other/os\n",
    "publish_forks:\n  seerxo/seerxo-os: seerxo-agent/seerxo-os\n  seerxo/seerxo-os: other/os\n",
])
def test_b5_duplicate_fork_policy_is_rejected_instead_of_last_value_winning(tmp_path, declaration):
    from pathlib import Path
    source = Path(github.__file__).resolve().parents[1] / "agents/repository_guardian.yaml"
    path = tmp_path / "policy.yaml"
    path.write_text(source.read_text() + declaration)
    with pytest.raises(ValueError, match="duplicate"):
        github.load_policy(path)
