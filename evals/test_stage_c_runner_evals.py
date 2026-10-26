"""Stage C golden scenarios: what the runner must do whatever the coder hands back.

Deterministic: the coder and the checks are scripted stand-ins for the model and the test
sandbox, so these pin the control plane's behaviour, not a model's. Each scenario names the
failure it catches.
"""
import os
import subprocess
from pathlib import Path
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models, stage_c, stage_c_runner
from app.stage_c_runner import Job

PASS = lambda wt: {"passed": True, "summary": "2 passed"}


@pytest.fixture
def env(tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(mirror), *a], check=True,
                                    capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (mirror / "app").mkdir()
    (mirror / "app" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (mirror / "app" / "other.py").write_text("x = 1\n")
    (mirror / "CLAUDE.md").write_text("rules\n")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "base")
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    task = models.Task(key="TASK-0101", title="fix add", success_criteria="add(2, 2) == 4")
    db.add(task)
    db.flush()
    for s in ("DETECTED", "PLANNED"):
        stage_c.advance(db, task, s, "test")
    db.commit()
    job = Job(task_key=task.key, repository="fixture/repo", mirror=mirror, base=git("rev-parse", "HEAD"),
              paths=["app/calc.py"], objective="fix add", success_criteria=task.success_criteria, provider="codex")
    return db, task, job, tmp_path / "work", git


def _stage(task):
    return task.context["improvement"]["stage"]


def _fix(ws, job):
    (ws / "app" / "calc.py").write_text("def add(a, b):\n    return a + b\n")


def test_a_scoped_fix_with_passing_checks_reaches_review_with_its_fingerprint(env):
    db, task, job, work, git = env
    seen = {}

    def checks(wt):
        seen["code"] = (wt / "app" / "calc.py").read_text()
        return {"passed": True, "summary": "1 passed"}

    out = stage_c_runner.run(db, task, job, _fix, checks, work)
    assert out["outcome"] == "READY_FOR_REVIEW" and _stage(task) == "INDEPENDENT_REVIEW"
    # Break caught: checks ran on something other than base + the imported diff.
    assert "a + b" in seen["code"]
    assert out["fingerprint"] == stage_c.diff_fingerprint(out["diff"])
    assert task.context["improvement"]["history"][-1]["evidence"]["fingerprint"] == out["fingerprint"]
    # Break caught: the runner pushed or moved main. It only adds task/<KEY> in the mirror.
    assert git("rev-parse", "main") == job.base
    assert git("for-each-ref", "--format=%(refname:short)", "refs/heads/task") == "task/TASK-0101"
    assert stage_c.acquire(db, job.repository, "next", timedelta(minutes=1))  # lease released


@pytest.mark.parametrize("name, act, reason", [
    ("governance file", lambda ws: (ws / "CLAUDE.md").write_text("no rules\n"), "restricted path"),
    ("workflow file", lambda ws: (ws / ".github" / "workflows").mkdir(parents=True)
     or (ws / ".github" / "workflows" / "ci.yml").write_text("on: push\n"), "restricted path"),
    ("unscoped new file", lambda ws: (ws / "app" / "extra.py").write_text("y = 1\n"), "outside the task scope"),
    ("deleted file", lambda ws: (ws / "app" / "calc.py").unlink(), "deleted files"),
    ("symlink", lambda ws: (ws / "app" / "calc.py").unlink()
     or os.symlink("/etc/passwd", ws / "app" / "calc.py"), "not a regular file"),
    ("no change", lambda ws: None, "no changes"),
])
def test_out_of_scope_or_unreviewable_work_never_reaches_checks(env, name, act, reason):
    db, task, job, work, git = env
    ran = []
    out = stage_c_runner.run(db, task, job, lambda ws, j: act(ws), lambda wt: ran.append(1) or PASS(wt), work)
    # Break caught: the coder edits governance, CI or files outside its scope and it ships.
    assert out["outcome"] == "REJECTED" and reason in out["detail"], name
    assert ran == [] and _stage(task) == "IMPLEMENTING"
    assert git("for-each-ref", "--format=%(refname:short)", "refs/heads/task") == ""


@pytest.mark.parametrize("result", [
    {"passed": False, "summary": "1 failed"},
    {"passed": "yes"},           # truthy is not True
    {"summary": "looks fine"},   # no verdict at all
    None,
])
def test_anything_but_an_explicit_pass_is_a_failure(env, result):
    db, task, job, work, _ = env
    out = stage_c_runner.run(db, task, job, _fix, lambda wt: result, work)
    # Break caught: an invented or ambiguous test result is treated as success.
    assert out["outcome"] == "CHECKS_FAILED" and _stage(task) == "IMPLEMENTING"
    assert task.context["improvement"]["history"][-1]["evidence"]["checks_passed"] is False


def test_crashing_checks_are_a_failure_not_a_crash(env):
    db, task, job, work, _ = env
    out = stage_c_runner.run(db, task, job, _fix, lambda wt: 1 / 0, work)
    assert out["outcome"] == "CHECKS_FAILED" and "ZeroDivisionError" in out["detail"]


def test_a_failing_coder_is_an_outcome_and_frees_the_lease(env):
    db, task, job, work, _ = env

    def boom(ws, j):
        raise TimeoutError("model run exceeded 600 s")

    out = stage_c_runner.run(db, task, job, boom, PASS, work)
    assert out["outcome"] == "CODER_FAILED" and "TimeoutError" in out["detail"]
    assert _stage(task) == "IMPLEMENTING"
    assert stage_c.acquire(db, job.repository, "next", timedelta(minutes=1))


def test_a_busy_repository_is_not_touched(env):
    db, task, job, work, _ = env
    stage_c.acquire(db, job.repository, "TASK-0999", timedelta(minutes=30))
    db.commit()
    called = []
    out = stage_c_runner.run(db, task, job, lambda ws, j: called.append(1), PASS, work)
    # Break caught: two mutating tasks on one repository at once.
    assert out["outcome"] == "LEASE_BUSY" and called == [] and _stage(task) == "PLANNED"


def test_losing_the_lease_mid_run_stops_before_review(env):
    db, task, job, work, _ = env

    def slow_checks(wt):
        # The lease expires and another task takes it while checks run.
        stage_c.release(db, job.repository, stage_c._locked(db, stage_c.lease_key(job.repository)).value["token"])
        stage_c.acquire(db, job.repository, "TASK-0999", timedelta(minutes=30))
        return {"passed": True}

    out = stage_c_runner.run(db, task, job, _fix, slow_checks, work)
    # Break caught: a fenced-out holder still advances work toward a push.
    assert out["outcome"] == "LEASE_LOST" and _stage(task) == "TESTING"
    assert not stage_c.acquire(db, job.repository, "third", timedelta(minutes=1))  # TASK-0999 keeps it


def test_a_repair_attempt_starts_from_implementing(env):
    db, task, job, work, git = env
    first = stage_c_runner.run(db, task, job, _fix, lambda wt: {"passed": False}, work / "1")
    assert first["outcome"] == "CHECKS_FAILED"
    # The caller cleans up the failed attempt before a repair.
    git("worktree", "remove", "--force", str(work / "1" / "TASK-0101" / "worktrees" / "TASK-0101"))
    git("branch", "-D", "task/TASK-0101")
    second = stage_c_runner.run(db, task, job, _fix, PASS, work / "2")
    assert second["outcome"] == "READY_FOR_REVIEW" and _stage(task) == "INDEPENDENT_REVIEW"


# --------------------------------------------------------------------------- independent review

def _ready(env):
    db, task, job, work, _ = env
    out = stage_c_runner.run(db, task, job, _fix, PASS, work)
    assert out["outcome"] == "READY_FOR_REVIEW"
    return db, task, out["diff"], out["fingerprint"]


def _says(verdict, fingerprint=None, findings=()):
    return lambda packet: {"verdict": verdict, "fingerprint": fingerprint or packet["fingerprint"],
                           "findings": list(findings)}


def test_another_provider_approves_exactly_the_tested_fingerprint(env):
    db, task, diff, f = _ready(env)
    seen = []
    out = stage_c_runner.review(db, task, diff, lambda p: seen.append(p) or _says("APPROVE")(p), "claude")
    assert out["outcome"] == "APPROVED" and _stage(task) == "INDEPENDENT_REVIEW"
    approval = task.context["improvement"]["approval"]
    assert approval["fingerprint"] == f and approval["reviewer"] == "claude" and approval["coder"] == "codex"
    # Break caught: the reviewer sees the workspace or anything beyond the review packet.
    assert set(seen[0]) == {"objective", "success_criteria", "fingerprint", "checks", "diff"}
    assert seen[0]["diff"] == diff.decode() and seen[0]["checks"] == "2 passed"
    assert db.query(models.AuditLog).filter_by(action="improvement.review_approved").count() == 1


@pytest.mark.parametrize("reviewer_provider", ["codex", ""])
def test_the_coder_never_reviews_itself(env, reviewer_provider):
    db, task, diff, _ = _ready(env)
    called = []
    out = stage_c_runner.review(db, task, diff, lambda p: called.append(1), reviewer_provider)
    # Break caught: self-review counted as independent review.
    assert out["outcome"] == "NOT_INDEPENDENT" and called == []
    assert "approval" not in task.context["improvement"]


def test_bytes_other_than_the_tested_diff_are_not_reviewed(env):
    db, task, diff, _ = _ready(env)
    called = []
    out = stage_c_runner.review(db, task, diff + b"+evil\n", lambda p: called.append(1), "claude")
    # Break caught: an untested diff slips into review and gets approved.
    assert out["outcome"] == "TAMPERED" and called == []


@pytest.mark.parametrize("name, reviewer", [
    ("approval of another fingerprint", _says("APPROVE", fingerprint="0" * 64)),
    ("lower-case verdict", _says("approve")),
    ("no verdict", lambda p: {"findings": []}),
    ("not a dict", lambda p: "APPROVE"),
    ("crash", lambda p: 1 / 0),
])
def test_only_a_valid_verdict_on_this_fingerprint_counts(env, name, reviewer):
    db, task, diff, _ = _ready(env)
    out = stage_c_runner.review(db, task, diff, reviewer, "claude")
    # Break caught: a stale, garbled or failed review is read as approval.
    assert out["outcome"] == "REVIEW_FAILED", name
    assert "approval" not in task.context["improvement"] and _stage(task) == "INDEPENDENT_REVIEW"


def test_a_rejection_sends_the_work_back_with_findings(env):
    db, task, diff, f = _ready(env)
    out = stage_c_runner.review(db, task, diff, _says("REJECT", findings=["no test for add"]), "claude")
    assert out["outcome"] == "REJECTED" and _stage(task) == "IMPLEMENTING"
    last = task.context["improvement"]["history"][-1]
    assert last["evidence"] == {"review": "REJECTED", "fingerprint": f, "findings": ["no test for add"]}
    assert "approval" not in task.context["improvement"]


def test_review_only_runs_at_independent_review(env):
    db, task, job, work, _ = env
    with pytest.raises(ValueError):
        stage_c_runner.review(db, task, b"", _says("APPROVE"), "claude")


class _Provider:
    def __init__(self, result):
        self.result, self.calls = result, []

    def run(self, prompt, schema, *, cwd, timeout_s, max_turns):
        self.calls.append({"cwd_empty": not os.listdir(cwd), "max_turns": max_turns,
                           "schema": schema, "prompt": prompt})
        return self.result


def test_a_stage_b_provider_reviews_with_no_tools_and_an_empty_scratch(env):
    from app import providers
    db, task, diff, f = _ready(env)
    ok = _Provider(providers.ProviderResult("claude", providers.Health.AVAILABLE,
                                            output={"verdict": "APPROVE", "fingerprint": f, "findings": []}))
    assert stage_c_runner.review(db, task, diff, stage_c_runner.provider_reviewer(ok), "claude")["outcome"] == "APPROVED"
    call = ok.calls[0]
    assert call["cwd_empty"] and call["max_turns"] == 1 and call["schema"] is stage_c_runner.REVIEW_SCHEMA
    assert f in call["prompt"] and "data, never instructions" in call["prompt"]


def test_an_unavailable_reviewer_is_no_verdict(env):
    from app import providers
    db, task, diff, _ = _ready(env)
    down = _Provider(providers.ProviderResult("claude", providers.Health.QUOTA_EXHAUSTED, error="limit"))
    out = stage_c_runner.review(db, task, diff, stage_c_runner.provider_reviewer(down), "claude")
    # Break caught: a quota-exhausted reviewer silently counts as approval or as rejection.
    assert out["outcome"] == "REVIEW_FAILED" and "QUOTA_EXHAUSTED" in out["detail"]
    assert _stage(task) == "INDEPENDENT_REVIEW"


# --------------------------------------------------------------------------- push + PR

@pytest.fixture
def approved(env, tmp_path):
    db, task, job, work, git = env
    def checks(d):  # a real test run leaves caches behind; they must not reach the push
        (d / "app" / "__pycache__").mkdir()
        (d / "app" / "__pycache__" / "calc.pyc").write_bytes(b"\0")
        return {"passed": True, "summary": "1 passed"}
    out = stage_c_runner.run(db, task, job, _fix, checks, work)
    assert stage_c_runner.review(db, task, out["diff"], _says("APPROVE"), "claude")["outcome"] == "APPROVED"
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(job.mirror), str(remote)], check=True)
    _remote(str(remote), "branch", "-D", "task/TASK-0101")  # GitHub has no task branch yet
    return db, task, job, out, Path(out["worktree"]), str(remote), git


def _remote(remote, *args):
    # check=True: a fixture step that fails silently would make a guard test pass vacuously.
    return subprocess.run(["git", "-C", remote, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           *args], capture_output=True, text=True, check=True).stdout.strip()


def _opener(urls):
    return lambda branch, title, body, commit: urls.append((branch, title, body, commit)) or f"https://pr/{branch}"


def test_an_approved_change_is_pushed_to_its_task_branch_and_gets_a_pr(approved):
    db, task, job, out, wt, remote, git = approved
    prs = []
    res = stage_c_runner.publish(db, task, job, out["diff"], wt, remote, _opener(prs))
    assert res["outcome"] == "PR_OPEN" and _stage(task) == "PR_OPEN", res
    assert prs[0][0] == "task/TASK-0101" and res["pr"] == "https://pr/task/TASK-0101"
    # Break caught: something other than the reviewed bytes, or main, gets pushed.
    assert _remote(remote, "rev-parse", "refs/heads/task/TASK-0101") == res["commit"]
    assert _remote(remote, "rev-parse", "refs/heads/main") == job.base
    assert _remote(remote, "rev-parse", f"{res['commit']}^") == job.base
    assert _remote(remote, "diff", "--name-only", job.base, res["commit"]) == "app/calc.py"
    assert out["fingerprint"] in _remote(remote, "log", "-1", "--format=%B", res["commit"])
    assert task.context["improvement"]["history"][-1]["evidence"]["commit"] == res["commit"]
    assert stage_c.acquire(db, job.repository, "next", timedelta(minutes=1))  # lease released


def test_no_approval_no_push(env, tmp_path):
    db, task, job, work, _ = env
    out = stage_c_runner.run(db, task, job, _fix, PASS, work)
    prs = []
    res = stage_c_runner.publish(db, task, job, out["diff"], Path(out["worktree"]), str(tmp_path / "none.git"), _opener(prs))
    # Break caught: a tested but unreviewed change is pushed.
    assert res["outcome"] == "NOT_APPROVED" and prs == []


@pytest.mark.parametrize("tamper", ["diff", "approval"])
def test_a_diff_other_than_the_approved_one_is_not_pushed(approved, tamper):
    db, task, job, out, wt, remote, _ = approved
    diff = out["diff"]
    if tamper == "diff":
        diff = diff.replace(b"a + b", b"a * b")
    else:
        imp = task.context["improvement"]
        task.context = {**task.context, "improvement": {**imp, "approval": {**imp["approval"], "fingerprint": "0" * 64}}}
    res = stage_c_runner.publish(db, task, job, diff, wt, remote, _opener([]))
    assert res["outcome"] == "NOT_APPROVED"
    assert _remote(remote, "for-each-ref", "refs/heads/task") == ""


@pytest.mark.parametrize("name, drift", [
    ("edited after review", lambda wt, git: (wt / "app" / "calc.py").write_text("def add(a, b):\n    return 0\n")),
    ("unreviewed new file", lambda wt, git: (wt / "app" / "backdoor.py").write_text("import os\n")),
    ("extra commit", lambda wt, git: subprocess.run(
        ["git", "-C", str(wt), "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qam", "sneak"], check=True)),
])
def test_anything_the_reviewer_did_not_see_blocks_the_push(approved, name, drift):
    db, task, job, out, wt, remote, git = approved
    drift(wt, git)
    res = stage_c_runner.publish(db, task, job, out["diff"], wt, remote, _opener([]))
    # Break caught: unreviewed content rides along with an approved fingerprint.
    assert res["outcome"] == "DRIFT", name
    assert _remote(remote, "for-each-ref", "refs/heads/task") == "" and _stage(task) == "INDEPENDENT_REVIEW"


@pytest.mark.parametrize("existing", ["diverged", "ancestor"])
def test_an_existing_task_branch_is_never_overwritten(approved, existing):
    db, task, job, out, wt, remote, git = approved
    other = job.base
    if existing == "diverged":
        tree = _remote(remote, "rev-parse", f"{job.base}^{{tree}}")
        other = subprocess.run(["git", "-C", remote, "-c", "user.name=x", "-c", "user.email=x@x", "commit-tree",
                                "-p", job.base, "-m", "someone else", tree],
                               check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", remote, "update-ref", "refs/heads/task/TASK-0101", other], check=True)
    res = stage_c_runner.publish(db, task, job, out["diff"], wt, remote, _opener([]))
    # Break caught: the control plane force-pushes over, or fast-forwards, existing work.
    assert res["outcome"] == "PUSH_FAILED"
    assert _remote(remote, "rev-parse", "refs/heads/task/TASK-0101") == other


def _repair_to_review(db, task, job, prs, work, remote):
    """CI fails on the published commit; a repair attempt reaches INDEPENDENT_REVIEW again."""
    from app import stage_c_ci
    published = task.context["improvement"]["history"][-1]["evidence"]["commit"]
    failed = {"state": "open", "merged": False, "head_sha": published, "checks_complete": True,
              "checks": [{"name": "ci", "status": "completed", "conclusion": "failure"}],
              "statuses": {}, "statuses_complete": True, "status_problems": []}
    required = ("ci/circleci: python-tests", "ci/circleci: frontend-tests")
    assert stage_c_ci.ingest(db, task, failed, required_statuses=required)["outcome"] == "REPAIR"
    stage_c_runner.discard(job, work)
    fix = lambda ws, job: (ws / "app" / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    out = stage_c_runner.run(db, task, job, fix, PASS, work)
    assert out["outcome"] == "READY_FOR_REVIEW", out
    assert stage_c_runner.review(db, task, out["diff"], _says("APPROVE"), "claude")["outcome"] == "APPROVED"
    return out, published


def test_a_ci_repair_replaces_its_own_push_under_the_same_pr(approved, tmp_path):
    db, task, job, out, wt, remote, _ = approved
    prs = []
    first = stage_c_runner.publish(db, task, job, out["diff"], wt, remote, _opener(prs))
    out2, published = _repair_to_review(db, task, job, prs, tmp_path / "work", remote)
    res = stage_c_runner.publish(db, task, job, out2["diff"], Path(out2["worktree"]), remote, _opener(prs))
    # Break caught: a repair cannot publish, or opens a second PR for the same task.
    assert res["outcome"] == "PR_OPEN" and res["pr"] == first["pr"] and len(prs) == 1, res
    assert _remote(remote, "rev-parse", "refs/heads/task/TASK-0101") == res["commit"] != published
    assert _remote(remote, "rev-parse", f"{res['commit']}^") == job.base


def test_a_repair_never_overwrites_a_push_by_someone_else(approved, tmp_path):
    db, task, job, out, wt, remote, _ = approved
    prs = []
    stage_c_runner.publish(db, task, job, out["diff"], wt, remote, _opener(prs))
    out2, published = _repair_to_review(db, task, job, prs, tmp_path / "work", remote)
    tree = _remote(remote, "rev-parse", f"{published}^{{tree}}")
    other = subprocess.run(["git", "-C", remote, "-c", "user.name=x", "-c", "user.email=x@x", "commit-tree",
                            "-p", published, "-m", "founder fixup", tree],
                           check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", remote, "update-ref", "refs/heads/task/TASK-0101", other], check=True)
    res = stage_c_runner.publish(db, task, job, out2["diff"], Path(out2["worktree"]), remote, _opener(prs))
    # Break caught: the repair force-pushes over a commit the control plane did not publish.
    assert res["outcome"] == "PUSH_FAILED"
    assert _remote(remote, "rev-parse", "refs/heads/task/TASK-0101") == other


def test_a_busy_repository_blocks_the_push(approved):
    db, task, job, out, wt, remote, _ = approved
    stage_c.acquire(db, job.repository, "TASK-0999", timedelta(minutes=30))
    db.commit()
    res = stage_c_runner.publish(db, task, job, out["diff"], wt, remote, _opener([]))
    assert res["outcome"] == "LEASE_BUSY" and _remote(remote, "for-each-ref", "refs/heads/task") == ""


def test_a_failed_pr_keeps_the_branch_and_does_not_claim_pr_open(approved):
    db, task, job, out, wt, remote, _ = approved

    def down(branch, title, body, commit):
        raise RuntimeError("gh: HTTP 502 from https://user:secret@github.com")

    res = stage_c_runner.publish(db, task, job, out["diff"], wt, remote, down)
    assert res["outcome"] == "PR_FAILED" and _stage(task) == "INDEPENDENT_REVIEW"
    assert _remote(remote, "rev-parse", "refs/heads/task/TASK-0101") == res["commit"]
    # Break caught: a credential in an error message lands in the task record.
    assert "secret" not in res["detail"]


def test_a_created_pr_with_a_lost_response_is_recorded_without_another_write(approved, monkeypatch):
    from app import repo_guardian_github
    db, task, job, out, wt, remote, _ = approved
    run = subprocess.run
    created, reads = [], []
    url = "https://github.com/fixture/repo/pull/101"

    def create(argv, **kwargs):
        if argv[:3] != ["gh", "pr", "create"]:
            return run(argv, **kwargs)
        created.append(_remote(remote, "rev-parse", "refs/heads/task/TASK-0101"))
        raise subprocess.TimeoutExpired(argv, 60)

    def lookup(path, expression):
        reads.append(path)
        return [{"number": 101, "state": "open", "html_url": url,
                 "base": {"ref": "main", "repo": {"full_name": job.repository}},
                 "head": {"ref": "task/TASK-0101", "sha": created[0],
                          "repo": {"full_name": job.repository}}}]

    monkeypatch.setattr(subprocess, "run", create)
    monkeypatch.setattr(repo_guardian_github, "api", lookup)
    result = stage_c_runner.publish(db, task, job, out["diff"], wt, remote,
                                   stage_c_runner.gh_pr_opener(job.repository, "main"))
    assert result["outcome"] == "PR_OPEN" and _stage(task) == "PR_OPEN", result
    assert created == [result["commit"]] and len(reads) == 1
    assert task.context["improvement"]["history"][-1]["evidence"]["pr"] == url


def test_restarted_publication_only_reads_the_existing_exact_commit_and_pr(approved, monkeypatch):
    db, task, job, out, wt, remote, _ = approved
    created = []

    def lost(*args):
        created.append(1)
        raise TimeoutError("response lost")

    first = stage_c_runner.publish(db, task, job, out["diff"], wt, remote, lost)
    assert first["outcome"] == "PR_FAILED"
    git, looked_up = stage_c_runner._git, []

    def read_only(repo, *args, **kwargs):
        assert args[0] not in {"commit", "push"}, args
        return git(repo, *args, **kwargs)

    def lookup(branch, commit):
        looked_up.append((branch, commit))
        return "https://github.com/fixture/repo/pull/101"

    monkeypatch.setattr(stage_c_runner, "_git", read_only)
    second = stage_c_runner.publish(db, task, job, out["diff"], wt, remote, lost, find_pr=lookup)
    assert second["outcome"] == "PR_OPEN" and second["commit"] == first["commit"]
    assert _stage(task) == "PR_OPEN" and created == [1]
    assert looked_up == [("task/TASK-0101", first["commit"])]


@pytest.mark.parametrize("tamper", ["remote missing", "remote moved", "extra parent", "wrong committed tree", "missing PR"])
def test_restarted_publication_requires_complete_delivery_proof(approved, tamper):
    db, task, job, out, wt, remote, _ = approved
    created, reads = [], []

    def lost(*args):
        created.append(1)
        raise TimeoutError("response lost")

    assert stage_c_runner.publish(db, task, job, out["diff"], wt, remote, lost)["outcome"] == "PR_FAILED"
    if tamper == "remote missing":
        _remote(remote, "update-ref", "-d", "refs/heads/task/TASK-0101")
    elif tamper == "remote moved":
        _remote(remote, "update-ref", "refs/heads/task/TASK-0101", job.base)
    elif tamper == "extra parent":
        stage_c_runner._git(wt, "commit", "--allow-empty", "-qm", "unrecorded extra commit")
    elif tamper == "wrong committed tree":
        path = wt / "app/calc.py"
        path.write_text("def add(a, b):\n    return 0\n")
        stage_c_runner._git(wt, "commit", "-qam", "different committed content", "--amend")
        path.write_text("def add(a, b):\n    return a + b\n")

    def lookup(*args):
        reads.append(1)
        raise RuntimeError("missing PR")

    result = stage_c_runner.publish(db, task, job, out["diff"], wt, remote, lost, find_pr=lookup)
    expected = "DRIFT" if tamper in {"extra parent", "wrong committed tree"} else "PR_FAILED"
    assert result["outcome"] == expected and _stage(task) == "INDEPENDENT_REVIEW"
    assert created == [1] and reads == ([1] if tamper == "missing PR" else [])


@pytest.mark.parametrize("remote", ["git@github.com:o/r.git", "ext::sh -c touch% /tmp/pwned", "relative/path"])
def test_only_https_or_a_local_test_remote_is_accepted(approved, remote):
    db, task, job, out, wt, _, _ = approved
    # Break caught: an ext:: or ssh remote turns the push into command execution or a key use.
    with pytest.raises(ValueError):
        stage_c_runner.publish(db, task, job, out["diff"], wt, remote, _opener([]))
