"""Regression tests for scripts/merge-pr: CI gate, exact-head merge and post-merge branch cleanup.

A fake `gh` on PATH records every call and answers from environment variables, so no network
is used. Real git runs in a temporary repository, so the worktree check is exercised for real.
"""
import json
import os
import pty
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "merge-pr"
OWNER_REPO = "owner/repo"
HEAD = "a" * 40
GREEN = [{"name": "test", "state": "SUCCESS"}, {"name": "lint", "state": "SUCCESS"}]
COMPANY_REPO = "seerxo/seerxo-os"
COMPANY_CONTEXTS = ["ci/circleci: python-tests", "ci/circleci: frontend-tests"]
COMPANY_WORKFLOW = ".github/workflows/frontend.yml"
COMPANY_POLICY = '''branches:\n  seerxo/seerxo-os: main\nworkflows:\n  - ".github/workflows/frontend.yml"\nstatus_publisher:\n  login: "circleci-app[bot]"\n  id: 127350680\nrequired_statuses:\n  - "ci/circleci: python-tests"\n  - "ci/circleci: frontend-tests"\n'''
CIRCLECI_ONLY_POLICY = COMPANY_POLICY.replace('workflows:\n  - ".github/workflows/frontend.yml"\n', "workflows: []\n")

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["GH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
def count(name):
    path = os.environ["GH_LOG"]
    return sum(1 for line in open(path) if json.loads(line)[:2] == name)
if args[:2] == ["pr", "view"]:
    field = args[args.index("--json") + 1]
    if field == "state":
        print(os.environ.get("FAKE_PR_STATE", "MERGED"))
    elif field == "headRefOid":
        # second read simulates a push during the CI wait
        second = os.environ.get("FAKE_HEAD_AFTER_WAIT")
        n = count(["pr", "view"])
        print(second if (second and n > 1) else os.environ["FAKE_HEAD"])
    elif field == "headRefName":
        print(os.environ["FAKE_BRANCH"])
    elif field == "baseRefName":
        print(os.environ.get("FAKE_BASE_BRANCH", "main"))
    elif field == "nameWithOwner":
        print(os.environ["FAKE_OWNER_REPO"])
    elif field == "headRepositoryOwner,headRepository":
        print(os.environ.get("FAKE_HEAD_REPO", os.environ["FAKE_OWNER_REPO"]))
elif args[:2] == ["pr", "checks"]:
    if "--watch" in args:
        sys.exit(0)
    print(os.environ.get("FAKE_CHECKS", "[]"))
elif args[:2] == ["pr", "merge"]:
    if os.environ.get("FAKE_ALREADY_MERGED"):
        print("! Pull request seerxo/seerxo-os#7 was already merged", file=sys.stderr)
    sys.exit(int(os.environ.get("FAKE_MERGE_RC", "0")))
elif args[:2] == ["repo", "view"]:
    print(os.environ["FAKE_OWNER_REPO"])
elif args[0] == "api" and "-X" in args:
    if os.environ.get("FAKE_DELETE_FAIL"):
        sys.exit(1)
    sys.exit(0)
elif args[:2] == ["api", "user"]:
    print(os.environ.get("FAKE_ACTOR", "semihbugrasezer"))
elif args[0] == "api":
    if "contents/agents/repository_guardian.yaml" in args[1]:
        import base64
        encoded = base64.b64encode(os.environ.get("FAKE_POLICY", "").encode()).decode()
        if os.environ.get("FAKE_POLICY_WRAP"):
            encoded = "\n".join(encoded[i:i + 60] for i in range(0, len(encoded), 60))
        print(encoded)
        sys.exit(0)
    if "/statuses?per_page=100" in args[1]:
        if os.environ.get("FAKE_STATUSES_FAIL"):
            sys.exit(1)
        print(os.environ.get("FAKE_STATUSES", "[]"))
        sys.exit(0)
    if "/actions/runs?" in args[1]:
        if os.environ.get("FAKE_RUNS_FAIL"):
            sys.exit(1)
        print(os.environ.get("FAKE_RUNS", '{"total_count": 0, "workflow_runs": []}'))
        sys.exit(0)
    if "commits" in args[1]:
        print(os.environ.get("FAKE_COMMIT_MESSAGE", "Update example"))
        sys.exit(0)
    remote = os.environ.get("FAKE_REMOTE_SHA", "gone")
    if remote == "gone":
        print("Not Found", file=sys.stderr); sys.exit(1)
    print(remote)
else:
    print("unexpected gh call " + repr(args), file=sys.stderr); sys.exit(2)
'''

FAKE_SSH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["SSH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if os.environ.get("FAKE_SSH_FAIL"):
    sys.exit(1)
if "identify-pr" in args:
    stage_c = os.environ.get("FAKE_IS_STAGE_C") == "1"
    pr_number = json.loads(os.environ.get("FAKE_IDENTIFY_PR_NUMBER", "7"))
    print(json.dumps({"stage_c": stage_c, "pr_number": pr_number,
                      **({"task_key": "TASK-0070"} if stage_c else {})}))
    sys.exit(0)
if "merge-complete" in args and os.environ.get("FAKE_SSH_FAIL_COMPLETE_ONCE"):
    count_file = os.environ["SSH_LOG"] + ".complete-count"
    count = int(open(count_file).read()) if os.path.exists(count_file) else 0
    open(count_file, "w").write(str(count + 1))
    if count == 0:
        sys.exit(1)
if "merge-authorize" in args:
    if os.environ.get("FAKE_BRANCH") != "task/TASK-0070":
        sys.exit(1)
    authorization_id = json.loads(os.environ.get("FAKE_AUTHORIZATION_ID", "71"))
    pr_number = json.loads(os.environ.get("FAKE_AUTHORIZATION_PR_NUMBER", "7"))
    print(json.dumps({"authorization_id": authorization_id, "pr_number": pr_number,
                      "head_sha": os.environ["FAKE_HEAD"]}))
elif "merge-complete" in args:
    print(json.dumps({"completed": True, "authorization_id": 71, "head_sha": os.environ["FAKE_HEAD"],
                      "merge_commit": "d" * 40, "merged_by": "semihbugrasezer"}))
else:
    sys.exit(2)
'''


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    base = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_LOG": str(tmp_path / "gh.log"),
        "SSH_LOG": str(tmp_path / "ssh.log"),
        "FAKE_OWNER_REPO": OWNER_REPO,
        "FAKE_HEAD": HEAD,
        "FAKE_BRANCH": "feat/example",
        "FAKE_REMOTE_SHA": HEAD,
        "GREEN_JSON": json.dumps(GREEN),
        "FAKE_CHECKS": json.dumps(GREEN),
        "FAKE_POLICY": COMPANY_POLICY,
        "FAKE_STATUSES": company_statuses(),
        "FAKE_RUNS": company_runs(),
    }
    return {"base": base, "repo": repo, "log": tmp_path / "gh.log"}


def run(env, extra=None, typed=HEAD):
    """Runs merge-pr with a pseudo-terminal as stdin, typing `typed` as the confirmed head SHA."""
    proc_env = {**env["base"], **(extra or {})}
    if extra and "FAKE_BRANCH" in extra and "FAKE_RUNS" not in extra:
        proc_env["FAKE_RUNS"] = company_runs(company_run(head_branch=extra["FAKE_BRANCH"]))
    master, slave = pty.openpty()
    proc = subprocess.Popen(["bash", str(SCRIPT), "7"], cwd=env["repo"], env=proc_env, stdin=slave,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.close(slave)
    os.write(master, (typed + "\n").encode())  # buffered by the terminal until the script reads it
    out, err = proc.communicate(timeout=120)
    os.close(master)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out.decode(), err.decode())


def run_without_terminal(env, extra=None):
    proc_env = {**env["base"], **(extra or {})}
    return subprocess.run(["bash", str(SCRIPT), "7"], cwd=env["repo"], env=proc_env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


def calls(env):
    if not env["log"].exists():
        return []
    return [json.loads(line) for line in env["log"].read_text().splitlines()]


def deleted(env):
    return [c for c in calls(env) if "-X" in c and "DELETE" in c]


def merged(env):
    return [c for c in calls(env) if c[:2] == ["pr", "merge"]]


def test_success_merges_then_deletes_the_merged_branch(env):
    res = run(env)
    assert res.returncode == 0, res.stderr
    assert len(merged(env)) == 1
    assert len(deleted(env)) == 1
    assert f"repos/{OWNER_REPO}/git/refs/heads/feat/example" in deleted(env)[0]
    assert "deleted merged branch feat/example" in res.stdout


def test_protected_branch_is_never_deleted(env):
    res = run(env, {"FAKE_BRANCH": "main"})
    assert res.returncode == 0
    assert merged(env) and not deleted(env)
    assert "keeping protected branch main" in res.stdout


@pytest.mark.parametrize("name", ["develop", "release/1.2", "hotfix/x"])
def test_other_protected_names_are_never_deleted(env, name):
    run(env, {"FAKE_BRANCH": name})
    assert not deleted(env)


def test_stale_tip_is_kept_when_branch_moved_after_merge(env):
    res = run(env, {"FAKE_REMOTE_SHA": "b" * 40})
    assert res.returncode == 0
    assert not deleted(env)
    assert "moved after merge" in res.stdout


def test_branch_checked_out_in_a_worktree_is_kept(env):
    wt = env["repo"].parent / "wt"
    subprocess.run(["git", "-C", str(env["repo"]), "worktree", "add", "-q", "-b", "feat/example",
                    str(wt)], check=True)
    res = run(env)
    assert res.returncode == 0
    assert not deleted(env)
    assert "checked out in a local worktree" in res.stdout
    shutil.rmtree(wt)


def test_failed_delete_warns_but_the_merge_still_succeeds(env):
    res = run(env, {"FAKE_DELETE_FAIL": "1"})
    assert res.returncode == 0
    assert merged(env)
    assert "WARNING could not delete feat/example" in res.stderr


def test_already_deleted_branch_is_success_and_idempotent(env):
    res = run(env, {"FAKE_REMOTE_SHA": "gone"})
    assert res.returncode == 0
    assert not deleted(env)
    assert "already gone" in res.stdout


def test_cleanup_can_be_disabled(env):
    res = run(env, {"MERGE_PR_DELETE_BRANCH": "0"})
    assert res.returncode == 0
    assert merged(env) and not deleted(env)


def test_red_ci_refuses_merge_and_deletes_nothing(env):
    red = json.dumps([{"name": "test", "state": "FAILURE"}])
    res = run(env, {"FAKE_CHECKS": red})
    assert res.returncode != 0
    assert "CI is not green" in res.stderr
    assert not merged(env) and not deleted(env)


def test_head_moved_during_wait_refuses_merge(env):
    res = run(env, {"FAKE_HEAD_AFTER_WAIT": "c" * 40})
    assert res.returncode != 0
    assert "head moved during the wait" in res.stderr
    assert not merged(env) and not deleted(env)


def test_failed_merge_never_reaches_cleanup(env):
    # An interrupted or refused merge must not delete a branch whose work is not in main.
    res = run(env, {"FAKE_MERGE_RC": "1", "FAKE_PR_STATE": "OPEN"})
    assert res.returncode != 0
    assert not deleted(env)


def test_repository_script_is_executable_and_parses():
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_already_merged_pr_is_reported_honestly_not_as_this_run_merging_it(env):
    res = run(env, {"FAKE_ALREADY_MERGED": "1", "MERGE_PR_DELETE_BRANCH": "0"})
    assert res.returncode == 0, res.stderr
    assert "already merged outside this gate" in res.stdout
    assert "merged at" not in res.stdout


def test_merge_that_leaves_pr_open_fails_the_gate(env):
    res = run(env, {"FAKE_PR_STATE": "OPEN", "MERGE_PR_DELETE_BRANCH": "0"})
    assert res.returncode != 0
    assert "not merged" in res.stderr


def test_merge_command_failure_is_not_reported_as_success(env):
    res = run(env, {"FAKE_MERGE_RC": "1", "FAKE_PR_STATE": "OPEN", "MERGE_PR_DELETE_BRANCH": "0"})
    assert res.returncode != 0
    assert "merged at" not in res.stdout


def company_statuses(states=None, publisher_id=127350680, publisher_login="circleci-app[bot]"):
    states = states or ["success", "success"]
    return json.dumps([{"id": i + 1, "context": context, "state": state,
                        "creator": {"id": publisher_id, "login": publisher_login}}
                       for i, (context, state) in enumerate(zip(COMPANY_CONTEXTS, states))])


def test_company_merge_requires_both_trusted_circleci_statuses_on_the_exact_head(env):
    res = run(env, {"FAKE_OWNER_REPO": COMPANY_REPO, "FAKE_POLICY": COMPANY_POLICY,
                    "FAKE_STATUSES": company_statuses(), "MERGE_PR_DELETE_BRANCH": "0"})
    assert res.returncode == 0, res.stderr
    assert len(merged(env)) == 1
    api_calls = [call for call in calls(env) if call[0] == "api"]
    assert any(f"commits/{HEAD}/statuses?per_page=100" in call[1] for call in api_calls)
    assert any("repository_guardian.yaml?ref=main" in call[1] for call in api_calls)


def test_company_merge_accepts_wrapped_base64_policy_content(env):
    res = run(env, {"FAKE_OWNER_REPO": COMPANY_REPO, "FAKE_POLICY": COMPANY_POLICY,
                    "FAKE_POLICY_WRAP": "1", "FAKE_STATUSES": company_statuses(),
                    "MERGE_PR_DELETE_BRANCH": "0"})
    assert res.returncode == 0, res.stderr


def test_company_merge_refuses_a_missing_circleci_status(env):
    statuses = json.loads(company_statuses())
    statuses.pop()
    res = run(env, {"FAKE_OWNER_REPO": COMPANY_REPO, "FAKE_POLICY": COMPANY_POLICY,
                    "FAKE_STATUSES": json.dumps(statuses), "MERGE_PR_DELETE_BRANCH": "0"})
    assert res.returncode != 0
    assert "missing required status" in res.stderr
    assert not merged(env)


def test_company_merge_strict_path_applies_to_github_casing(env):
    """GitHub reports the repository as "Seerxo/seerxo-os"; the strict CI path must still apply."""
    statuses = json.loads(company_statuses())
    statuses.pop()
    res = run(env, {"FAKE_OWNER_REPO": "Seerxo/seerxo-os", "FAKE_POLICY": COMPANY_POLICY,
                    "FAKE_STATUSES": json.dumps(statuses), "MERGE_PR_DELETE_BRANCH": "0"})
    assert res.returncode != 0
    assert "missing required status" in res.stderr
    assert not merged(env)


def test_company_merge_refuses_a_spoofed_circleci_status(env):
    res = run(env, {"FAKE_OWNER_REPO": COMPANY_REPO, "FAKE_POLICY": COMPANY_POLICY,
                    "FAKE_STATUSES": company_statuses(publisher_id=1, publisher_login="attacker"),
                    "MERGE_PR_DELETE_BRANCH": "0"})
    assert res.returncode != 0
    assert "untrusted publisher" in res.stderr
    assert not merged(env)


def test_company_merge_uses_the_latest_status_and_refuses_pending(env):
    statuses = json.loads(company_statuses())
    statuses.append({"id": 9, "context": COMPANY_CONTEXTS[0], "state": "pending",
                     "creator": {"id": 127350680, "login": "circleci-app[bot]"}})
    res = run(env, {"FAKE_OWNER_REPO": COMPANY_REPO, "FAKE_POLICY": COMPANY_POLICY,
                    "FAKE_STATUSES": json.dumps(statuses), "MERGE_PR_DELETE_BRANCH": "0"})
    assert res.returncode != 0
    assert "pending" in res.stderr
    assert not merged(env)


def company_run(**overrides):
    return {"id": 1, "path": COMPANY_WORKFLOW, "head_sha": HEAD, "event": "pull_request", "status": "completed",
            "conclusion": "success", "run_number": 1, "run_attempt": 1, "head_repo": COMPANY_REPO,
            "run_repo": COMPANY_REPO, "head_branch": "feat/example", **overrides}


def company_runs(*runs, total=None):
    runs = list(runs) if runs else [company_run()]
    return json.dumps({"total_count": len(runs) if total is None else total, "workflow_runs": runs})


def company_merge(env, **extra):
    return run(env, {"FAKE_OWNER_REPO": COMPANY_REPO, "FAKE_POLICY": COMPANY_POLICY,
                     "FAKE_STATUSES": company_statuses(), "MERGE_PR_DELETE_BRANCH": "0", **extra})


def test_company_merge_requires_the_sengi_frontend_run_on_the_exact_head(env):
    res = company_merge(env)
    assert res.returncode == 0, res.stderr
    assert len(merged(env)) == 1
    assert any(f"actions/runs?head_sha={HEAD}&event=pull_request" in c[1] for c in calls(env) if c[0] == "api")


@pytest.mark.parametrize("runs, why", [
    (company_runs(company_run(head_sha="b" * 40)), "missing required workflow run"),         # another commit
    (company_runs(company_run(event="push")), "missing required workflow run"),             # wrong trigger
    (company_runs(company_run(path=".github/workflows/other.yml")), "missing required workflow run"),
    (company_runs(company_run(head_repo="attacker/fork")), "missing required workflow run"),
    (json.dumps({"total_count": 0, "workflow_runs": []}), "missing required workflow run"),
    (company_runs(company_run(status="in_progress", conclusion=None)), "not green"),
    (company_runs(company_run(conclusion="failure")), "not green"),
    (company_runs(company_run(conclusion="cancelled")), "not green"),
    (company_runs(company_run(), company_run(id=2, run_attempt=2, conclusion="failure")), "not green"),  # rerun wins
    (company_runs(company_run(), total=150), "incomplete"),
    (company_runs(company_run(), total=-1), "incomplete"),
    (company_runs(company_run(), total=0), "incomplete"),
    (json.dumps({"workflow_runs": "x"}), "malformed"),
])
def test_company_merge_refuses_anything_short_of_a_green_sengi_run_on_the_head(env, runs, why):
    res = company_merge(env, FAKE_RUNS=runs)
    assert res.returncode != 0
    assert why in res.stderr
    assert not merged(env)


def test_company_merge_uses_the_latest_sengi_attempt(env):
    runs = company_runs(company_run(conclusion="failure"), company_run(id=2, run_attempt=2))
    assert company_merge(env, FAKE_RUNS=runs).returncode == 0


def test_company_merge_refuses_when_workflow_runs_are_unreadable(env):
    res = company_merge(env, FAKE_RUNS_FAIL="1")
    assert res.returncode != 0 and "workflow runs unavailable" in res.stderr
    assert not merged(env)


def test_company_merge_still_requires_circleci_when_sengi_is_green(env):
    res = company_merge(env, FAKE_STATUSES=company_statuses(["success", "failure"]))
    assert res.returncode != 0 and "not green" in res.stderr
    assert not merged(env)


def test_circleci_only_base_policy_needs_no_workflow_run(env):
    res = company_merge(env, FAKE_POLICY=CIRCLECI_ONLY_POLICY, FAKE_RUNS_FAIL="1")
    assert res.returncode == 0, res.stderr
    assert not any("actions/runs" in c[1] for c in calls(env) if c[0] == "api")


@pytest.mark.parametrize("policy", [
    COMPANY_POLICY.replace('workflows:\n  - ".github/workflows/frontend.yml"\n', ""),            # absent
    COMPANY_POLICY.replace('".github/workflows/frontend.yml"', '"../evil.yml"'),
    COMPANY_POLICY.replace('  - ".github/workflows/frontend.yml"\n',
                           '  - ".github/workflows/frontend.yml"\n  - ".github/workflows/frontend.yml"\n'),
])
def test_company_merge_refuses_a_malformed_workflow_policy(env, policy):
    res = company_merge(env, FAKE_POLICY=policy)
    assert res.returncode != 0
    assert not merged(env)


def test_company_merge_fails_closed_when_trusted_policy_is_unavailable(env):
    res = run(env, {"FAKE_OWNER_REPO": COMPANY_REPO, "FAKE_POLICY": "",
                    "FAKE_STATUSES": company_statuses(), "MERGE_PR_DELETE_BRANCH": "0"})
    assert res.returncode != 0
    assert not merged(env)


def test_stage_c_task_merges_require_control_plane_receipts_before_and_after_merge(env):
    res = run(env, {"FAKE_OWNER_REPO": "seerxo/seerxo-os", "FAKE_BRANCH": "task/TASK-0070",
                    "FAKE_IS_STAGE_C": "1", "MERGE_PR_DELETE_BRANCH": "0"})
    assert res.returncode == 0, res.stderr
    ssh_calls = [json.loads(line) for line in Path(env["base"]["SSH_LOG"]).read_text().splitlines()]
    assert any("merge-authorize" in call for call in ssh_calls)
    assert any("merge-complete" in call for call in ssh_calls)
    assert len(merged(env)) == 1
    assert "merge verified by the control plane" in res.stdout


def test_stage_c_task_refuses_merge_when_control_plane_is_unavailable(env):
    res = run(env, {"FAKE_OWNER_REPO": "seerxo/seerxo-os", "FAKE_BRANCH": "task/TASK-0070",
                    "FAKE_SSH_FAIL": "1"})
    assert res.returncode != 0
    assert "PR classification unavailable" in res.stderr
    assert not merged(env) and not deleted(env)


@pytest.mark.parametrize("receipt", ["true", "7.0", "8"])
def test_malformed_stage_c_classification_receipt_refuses_before_merge(env, receipt):
    res = run(env, {"FAKE_OWNER_REPO": "seerxo/seerxo-os", "FAKE_BRANCH": "feat/example",
                    "FAKE_IDENTIFY_PR_NUMBER": receipt})
    assert res.returncode != 0
    assert "invalid Stage C PR classification" in res.stderr
    assert not merged(env) and not deleted(env)


def test_stage_c_task_merge_is_founder_only_before_the_merge_call(env):
    res = run(env, {"FAKE_OWNER_REPO": "seerxo/seerxo-os", "FAKE_BRANCH": "task/TASK-0070",
                    "FAKE_IS_STAGE_C": "1", "FAKE_ACTOR": "another-maintainer"})
    assert res.returncode != 0
    assert "founder-only" in res.stderr
    assert not merged(env) and not deleted(env)


@pytest.mark.parametrize("extra", [
    {"FAKE_AUTHORIZATION_ID": "true"},
    {"FAKE_AUTHORIZATION_ID": "0"},
    {"FAKE_AUTHORIZATION_ID": "-1"},
    {"FAKE_AUTHORIZATION_PR_NUMBER": "true"},
])
def test_malformed_stage_c_authorization_receipt_refuses_before_merge(env, extra):
    res = run(env, {"FAKE_OWNER_REPO": "seerxo/seerxo-os", "FAKE_BRANCH": "task/TASK-0070",
                    "FAKE_IS_STAGE_C": "1", "MERGE_PR_DELETE_BRANCH": "0", **extra})
    assert res.returncode != 0
    assert "invalid Stage C authorization receipt" in res.stderr
    assert not merged(env) and not deleted(env)


def test_malformed_task_branch_cannot_fall_through_to_generic_merge(env):
    res = run(env, {"FAKE_OWNER_REPO": "seerxo/seerxo-os", "FAKE_BRANCH": "task/TASK-0070-extra"})
    assert res.returncode != 0
    assert "task-prefixed branch has no durable Stage C publication" in res.stderr
    assert not merged(env) and not deleted(env)


def test_stage_c_commit_without_committed_pr_record_cannot_use_generic_merge(env):
    # Stage C creates its GitHub PR before committing PR_OPEN; the commit identity closes that gap.
    res = run(env, {"FAKE_OWNER_REPO": "seerxo/seerxo-os", "FAKE_BRANCH": "feat/renamed",
                    "FAKE_COMMIT_MESSAGE": "TASK-0070: task title\n\nFingerprint: abc"})
    assert res.returncode != 0
    assert "no durable publication record" in res.stderr
    assert not merged(env) and not deleted(env)


def test_renamed_stage_c_branch_still_uses_control_plane_and_fails_closed(env):
    res = run(env, {"FAKE_OWNER_REPO": "seerxo/seerxo-os", "FAKE_BRANCH": "feat/renamed",
                    "FAKE_IS_STAGE_C": "1"})
    assert res.returncode != 0
    assert "control-plane merge authorization" in res.stderr
    assert not merged(env) and not deleted(env)


def test_stage_c_merge_completion_can_be_retried_after_github_merge(env):
    common = {"FAKE_OWNER_REPO": "seerxo/seerxo-os", "FAKE_BRANCH": "task/TASK-0070",
              "FAKE_IS_STAGE_C": "1", "FAKE_SSH_FAIL_COMPLETE_ONCE": "1",
              "MERGE_PR_DELETE_BRANCH": "0"}
    first = run(env, common)
    assert first.returncode != 0
    assert len(merged(env)) == 1

    second = run(env, common)
    assert second.returncode == 0, second.stderr
    ssh_calls = [json.loads(line) for line in Path(env["base"]["SSH_LOG"]).read_text().splitlines()]
    assert sum("merge-authorize" in call for call in ssh_calls) == 2
    assert sum("merge-complete" in call for call in ssh_calls) == 2
    assert "merge verified by the control plane" in second.stdout


def test_merge_error_that_still_merged_is_reported_not_as_a_gate_failure(env):
    # A timeout or a racing merge can return an error after the PR was merged on the server.
    res = run(env, {"FAKE_MERGE_RC": "1", "MERGE_PR_DELETE_BRANCH": "0"})
    assert res.returncode == 0, res.stderr
    assert "already merged outside this gate" in res.stdout
    assert "merged at" not in res.stdout


def test_merge_is_refused_without_an_interactive_terminal(env):
    res = run_without_terminal(env)
    assert res.returncode != 0 and "interactive terminal is required" in res.stderr
    assert not any(c[:2] == ["pr", "merge"] for c in calls(env))


def test_merge_is_refused_when_the_typed_sha_does_not_match_the_checked_head(env):
    res = run(env, typed="b" * 40)
    assert res.returncode != 0 and "typed SHA does not match" in res.stderr
    assert not any(c[:2] == ["pr", "merge"] for c in calls(env))


def test_merge_is_refused_on_an_empty_confirmation(env):
    res = run(env, typed="")
    assert res.returncode != 0 and "typed SHA does not match" in res.stderr
    assert not any(c[:2] == ["pr", "merge"] for c in calls(env))


# The checked-in policy since the move to Seerxo/seerxo-os: both Actions workflows are the CI evidence, no statuses.
PYTHON_WORKFLOW = ".github/workflows/python-tests.yml"
ACTIONS_POLICY = ('branches:\n  seerxo/seerxo-os: main\nworkflows:\n  - ".github/workflows/frontend.yml"\n'
                  '  - ".github/workflows/python-tests.yml"\nstatus_publisher:\n  login: "circleci-app[bot]"\n'
                  '  id: 127350680\nrequired_statuses: []\n')


def actions_merge(env, *runs, statuses="[]", policy=ACTIONS_POLICY, **extra):
    rows = list(runs) if runs else [company_run(), company_run(id=2, path=PYTHON_WORKFLOW)]
    return run(env, {"FAKE_OWNER_REPO": COMPANY_REPO, "FAKE_POLICY": policy, "FAKE_STATUSES": statuses,
                     "FAKE_RUNS": json.dumps({"total_count": len(rows), "workflow_runs": rows}),
                     "MERGE_PR_DELETE_BRANCH": "0", **extra})


@pytest.mark.parametrize("extra", [
    {"FAKE_STATUSES_FAIL": "1"}, {"FAKE_STATUSES": "not JSON"}, {"FAKE_STATUSES": "[null]"},
])
def test_actions_only_merge_does_not_read_unused_statuses(env, extra):
    result = actions_merge(env, **extra)
    assert result.returncode == 0, result.stderr
    assert not any("/statuses" in call[1] for call in calls(env) if call[0] == "api")


def test_actions_policy_merges_when_frontend_and_python_are_green_on_the_head(env):
    res = actions_merge(env)
    assert res.returncode == 0, res.stderr
    assert len(merged(env)) == 1


@pytest.mark.parametrize("python_run, why", [
    (None, "missing required workflow run"),
    ({"conclusion": "failure"}, "not green"),
    ({"conclusion": "cancelled"}, "not green"),
    ({"conclusion": "skipped"}, "not green"),
    ({"status": "in_progress", "conclusion": None}, "not green"),
    ({"head_sha": "b" * 40}, "missing required workflow run"),
    ({"event": "push"}, "missing required workflow run"),
    ({"head_repo": "attacker/fork"}, "missing required workflow run"),
])
def test_actions_policy_refuses_without_a_green_python_run_on_the_head(env, python_run, why):
    rows = [company_run()]
    if python_run is not None:
        rows.append(company_run(id=2, path=PYTHON_WORKFLOW, **python_run))
    res = actions_merge(env, *rows)
    assert res.returncode != 0 and why in res.stderr
    assert not merged(env)


def test_a_spoofed_python_status_cannot_replace_the_python_workflow_run(env):
    spoof = json.dumps([{"id": 1, "context": "ci/circleci: python-tests", "state": "success",
                         "creator": {"id": 127350680, "login": "circleci-app[bot]"}}])
    res = actions_merge(env, company_run(), statuses=spoof)
    assert res.returncode != 0 and "missing required workflow run" in res.stderr
    assert not merged(env)


def test_a_policy_with_neither_workflows_nor_statuses_refuses(env):
    policy = ACTIONS_POLICY.replace('workflows:\n  - ".github/workflows/frontend.yml"\n  - ".github/workflows/python-tests.yml"\n',
                                    "workflows: []\n")
    res = actions_merge(env, policy=policy)
    assert res.returncode != 0 and "policy" in res.stderr
    assert not merged(env)


def test_actions_policy_accepts_githubs_casing_of_the_head_repository(env):
    """GitHub reports the run's head repository as "Seerxo/seerxo-os"; the policy identity is lowercase."""
    res = actions_merge(env, company_run(head_repo="Seerxo/seerxo-os"), company_run(id=2, path=PYTHON_WORKFLOW, head_repo="Seerxo/seerxo-os"))
    assert res.returncode == 0, res.stderr
    assert len(merged(env)) == 1


def test_a_run_without_a_head_repository_is_not_evidence(env):
    res = actions_merge(env, company_run(head_repo=None), company_run(id=2, path=PYTHON_WORKFLOW))
    assert res.returncode != 0 and "missing required workflow run" in res.stderr
    assert not merged(env)


FORK = "seerxo-agent/seerxo-os"
FORK_POLICY = ACTIONS_POLICY + f"publish_forks:\n  seerxo/seerxo-os: {FORK}\n"


def fork_runs(head_repo=FORK, head_branch="feat/example"):
    return (company_run(head_repo=head_repo, head_branch=head_branch),
            company_run(id=2, path=PYTHON_WORKFLOW, head_repo=head_repo, head_branch=head_branch))


STAGE_C_FORK_PR = {"FAKE_IS_STAGE_C": "1", "FAKE_BRANCH": "task/TASK-0070"}


def test_b5_stage_c_pr_from_the_trusted_publish_fork_merges_through_merge_authorize(env):
    res = actions_merge(env, *fork_runs("Seerxo-Agent/seerxo-os", "task/TASK-0070"), policy=FORK_POLICY,
                        FAKE_HEAD_REPO="Seerxo-Agent/seerxo-os", **STAGE_C_FORK_PR)
    assert res.returncode == 0, res.stderr
    assert len(merged(env)) == 1
    ssh_calls = [json.loads(line) for line in Path(env["base"]["SSH_LOG"]).read_text().splitlines()]
    assert any("merge-authorize" in call for call in ssh_calls)


def test_b5_fork_pr_without_a_stage_c_publication_never_takes_the_generic_path(env):
    """Codex P1 on #387: an agent-fork PR with an ordinary branch and subject, but no durable Stage C record,
    used to merge on green CI alone, skipping merge-authorize and the independent review."""
    res = actions_merge(env, *fork_runs(), policy=FORK_POLICY, FAKE_HEAD_REPO=FORK)   # branch feat/example
    assert res.returncode != 0 and "no durable Stage C publication" in res.stderr
    assert not merged(env)


@pytest.mark.parametrize("policy, head_repo", [
    (FORK_POLICY, "attacker/os"),                 # any other fork
    (ACTIONS_POLICY, FORK),                       # no fork declared in the trusted base policy
])
def test_b5_pr_from_an_untrusted_head_repository_is_refused_before_ci_is_judged(env, policy, head_repo):
    res = actions_merge(env, *fork_runs(head_repo), policy=policy, FAKE_HEAD_REPO=head_repo)
    assert res.returncode != 0 and "is not trusted" in res.stderr
    assert not merged(env)


def test_b5_upstream_runs_do_not_count_for_a_fork_pr(env):
    res = actions_merge(env, *fork_runs(COMPANY_REPO), policy=FORK_POLICY, FAKE_HEAD_REPO=FORK)
    assert res.returncode != 0 and "missing required workflow run" in res.stderr
    assert not merged(env)


@pytest.mark.parametrize("changed", [
    {"head_branch": "task/TASK-9999"}, {"head_branch": None},
    {"run_repo": "attacker/os"}, {"run_repo": None},
])
def test_b5_merge_refuses_same_sha_runs_with_wrong_or_missing_upstream_or_branch(env, changed):
    rows = [company_run(head_repo=FORK, **changed), company_run(id=2, path=PYTHON_WORKFLOW, head_repo=FORK)]
    res = actions_merge(env, *rows, policy=FORK_POLICY, FAKE_HEAD_REPO=FORK)
    assert res.returncode != 0
    assert not merged(env)


def test_b5_a_fork_merge_never_deletes_a_matching_upstream_branch(env):
    res = actions_merge(env, *fork_runs(FORK, "task/TASK-0070"), policy=FORK_POLICY, FAKE_HEAD_REPO=FORK,
                        MERGE_PR_DELETE_BRANCH="1", **STAGE_C_FORK_PR)
    assert res.returncode == 0, res.stderr
    assert len(merged(env)) == 1
    deletions = deleted(env)
    assert not deletions


def test_b5_founder_branch_prs_still_merge_when_a_fork_is_declared(env):
    res = actions_merge(env, policy=FORK_POLICY)
    assert res.returncode == 0, res.stderr


def test_b5_a_second_or_self_publish_fork_makes_the_policy_untrusted(env):
    for bad in (FORK_POLICY + "  seerxo/seerxo-os: other/os\n", ACTIONS_POLICY + "publish_forks:\n  seerxo/seerxo-os: seerxo/seerxo-os\n"):
        res = actions_merge(env, policy=bad)
        assert res.returncode != 0 and "policy" in res.stderr


@pytest.mark.parametrize("declaration", [
    "publish_forks:\n  seerxo/seerxo-os: Seerxo-Agent/seerxo-os\n",
    "publish_forks:\n  other/os: seerxo-agent/seerxo-os\n",
    "publish_forks: {seerxo/seerxo-os: seerxo-agent/seerxo-os}\n",
    "publish_forks: false\n",
    "publish_forks:\n  seerxo/seerxo-os: seerxo-agent/seerxo-os\npublish_forks:\n  seerxo/seerxo-os: other/os\n",
    "publish_forks:\n  seerxo/seerxo-os: seerxo-agent/seerxo-os\n  seerxo/seerxo-os: Other/os\n",
    "\"publish_forks\":\n  seerxo/seerxo-os: seerxo-agent/seerxo-os\n",
])
def test_b5_present_malformed_fork_policy_never_falls_back_to_upstream(env, declaration):
    result = actions_merge(env, policy=ACTIONS_POLICY + declaration)
    assert result.returncode != 0 and "policy" in result.stderr
    merge_calls = merged(env)
    assert not merge_calls
