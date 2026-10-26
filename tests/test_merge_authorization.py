"""Regression tests for app/merge_authorization.evaluate: forged, stale, replayed and inconsistent
approvals must never make a PR eligible. Pure data, no database and no network."""
import copy

import pytest

from app.merge_authorization import evaluate

REPO = "owner/product"
FP = "f" * 64          # the tested and approved diff fingerprint
OLD_FP = "0" * 64      # an earlier cycle's fingerprint (a replay source)
COMMIT = "c" * 40
URL = f"https://github.com/{REPO}/pull/12"
GREEN = [{"name": "test", "status": "completed", "conclusion": "success"}]
REQUIRED = ("ci/circleci: python-tests", "ci/circleci: frontend-tests")
GREEN_STATUSES = {context: "success" for context in REQUIRED}


def _context():
    review_at = "2026-10-12T10:00:00+00:00"
    return {"improvement": {
        "stage": "READY_TO_MERGE",
        "history": [
            {"stage": "TESTING", "evidence": {"coder": "claude", "fingerprint": FP}},
            {"stage": "INDEPENDENT_REVIEW", "at": review_at, "evidence": {"fingerprint": FP}},
            {"stage": "PR_OPEN", "evidence": {"branch": "task/TASK-0070", "commit": COMMIT,
                                                    "fingerprint": FP, "pr": URL}},
            {"stage": "CI", "evidence": {"ci_state": "running"}},
            {"stage": "READY_TO_MERGE", "evidence": {"ci_state": "passed"}},
        ],
        "approval": {"fingerprint": FP, "review_stage_at": review_at, "audit_id": 17,
                     "reviewer": "codex", "coder": "claude", "findings": []},
    }}


def _facts(**over):
    base = {"state": "open", "merged": False, "draft": False, "head_sha": COMMIT,
            "head_ref": "task/TASK-0070", "head_repo": REPO, "base_ref": "main",
            "checks_complete": True, "checks": GREEN, "statuses": dict(GREEN_STATUSES),
            "statuses_complete": True, "status_problems": []}
    base.update(over)
    return base


def _run(context=None, facts=None, **kw):
    kw.setdefault("required_statuses", REQUIRED)
    return evaluate(context=context if context is not None else _context(), repository=REPO,
                    task_key="TASK-0070", target_branch="main",
                    facts=facts if facts is not None else _facts(), **kw)


def test_consistent_review_and_green_ci_on_the_published_head_is_eligible():
    decision = _run()
    assert decision.eligible, decision.reasons


def test_no_approval_means_not_eligible():
    ctx = _context()
    del ctx["improvement"]["approval"]
    d = _run(context=ctx)
    assert not d.eligible and any("no independent review approval" in r for r in d.reasons)


def test_forged_approval_for_different_bytes_is_refused():
    ctx = _context()
    ctx["improvement"]["approval"]["fingerprint"] = OLD_FP
    d = _run(context=ctx)
    assert not d.eligible
    assert any("latest tested fingerprint" in r for r in d.reasons)


def test_replayed_approval_from_an_earlier_cycle_is_refused():
    ctx = _context()
    hist = ctx["improvement"]["history"]
    hist.insert(0, {"stage": "INDEPENDENT_REVIEW", "evidence": {"fingerprint": OLD_FP}})
    # The approval still names the old cycle's fingerprint while the latest test is FP.
    ctx["improvement"]["approval"]["fingerprint"] = OLD_FP
    d = _run(context=ctx)
    assert not d.eligible and any("stale or replayed" in r for r in d.reasons)


def test_same_fingerprint_approval_from_an_earlier_review_attempt_is_refused():
    ctx = _context()
    ctx["improvement"]["history"].insert(1, {"stage": "INDEPENDENT_REVIEW", "at": "old",
                                              "evidence": {"fingerprint": FP}})
    ctx["improvement"]["approval"]["review_stage_at"] = "old"
    d = _run(context=ctx)
    assert not d.eligible and any("latest independent review attempt" in r for r in d.reasons)


def test_changed_commit_on_the_pr_head_is_refused():
    d = _run(facts=_facts(head_sha="d" * 40))
    assert not d.eligible
    assert any("PR head is not the published commit" in r for r in d.reasons)


def test_failed_review_leaves_no_approval_and_is_refused():
    ctx = _context()
    ctx["improvement"]["stage"] = "IMPLEMENTING"
    del ctx["improvement"]["approval"]
    d = _run(context=ctx)
    assert not d.eligible
    assert any("no independent review approval" in r for r in d.reasons)


def test_reviewer_that_is_the_coder_is_refused():
    ctx = _context()
    ctx["improvement"]["approval"]["reviewer"] = "claude"
    d = _run(context=ctx)
    assert not d.eligible and any("not independent" in r for r in d.reasons)


def test_unknown_reviewer_provider_is_refused():
    ctx = _context()
    ctx["improvement"]["approval"]["reviewer"] = "some-bot"
    d = _run(context=ctx)
    assert not d.eligible and any("not a known provider" in r for r in d.reasons)


def test_approval_naming_a_different_coder_is_refused():
    ctx = _context()
    ctx["improvement"]["approval"]["coder"] = "codex"  # reviewer is codex too; coder swapped
    d = _run(context=ctx)
    assert not d.eligible
    assert any("different coder" in r for r in d.reasons)


def test_failing_ci_on_the_head_is_refused():
    d = _run(facts=_facts(checks=[{"name": "test", "status": "completed", "conclusion": "failure"}]))
    assert not d.eligible and any("not green" in r for r in d.reasons)


def test_pending_ci_is_refused():
    d = _run(facts=_facts(checks_complete=False,
                          checks=[{"name": "test", "status": "in_progress", "conclusion": None}]))
    assert not d.eligible and any("not complete" in r for r in d.reasons)


def test_blocked_task_is_refused_even_if_everything_else_matches():
    d = _run(blocked=True)
    assert not d.eligible and any("BLOCKED" in r for r in d.reasons)


def test_pr_from_another_repository_is_refused():
    ctx = _context()
    ctx["improvement"]["history"][2]["evidence"]["pr"] = "https://github.com/other/repo/pull/12"
    d = _run(context=ctx)
    assert not d.eligible and any("expected repository" in r for r in d.reasons)


def test_task_branch_from_a_fork_is_refused():
    d = _run(facts=_facts(head_repo="attacker/fork"))
    assert not d.eligible and any("head repository" in r for r in d.reasons)


@pytest.mark.parametrize("facts, phrase", [
    (_facts(head_ref="task/TASK-9999"), "task identity"),
    (_facts(base_ref="release"), "target branch"),
])
def test_wrong_task_or_target_branch_is_refused(facts, phrase):
    d = _run(facts=facts)
    assert not d.eligible and any(phrase in r for r in d.reasons)


def test_draft_or_closed_pr_is_refused():
    assert not _run(facts=_facts(draft=True)).eligible
    d = _run(facts=_facts(state="closed"))
    assert not d.eligible and any("not open" in r for r in d.reasons)


def test_concurrent_second_authorization_after_merge_is_refused():
    # A second run that sees the PR already merged must not produce a new merge decision.
    d = _run(facts=_facts(merged=True, state="closed"))
    assert not d.eligible and any("already merged" in r for r in d.reasons)


def test_eligibility_does_not_mutate_its_inputs():
    ctx, facts = _context(), _facts()
    snap_ctx, snap_facts = copy.deepcopy(ctx), copy.deepcopy(facts)
    evaluate(context=ctx, repository=REPO, task_key="TASK-0070", target_branch="main", facts=facts,
             required_statuses=REQUIRED)
    assert ctx == snap_ctx and facts == snap_facts


@pytest.mark.parametrize("statuses, reason", [
    ({REQUIRED[0]: "success"}, "frontend-tests' is missing"),
    ({REQUIRED[0]: "success", REQUIRED[1]: "pending"}, "pending, not success"),
    ({REQUIRED[0]: "success", REQUIRED[1]: "failure"}, "failure, not success"),
    ({REQUIRED[0]: "success", REQUIRED[1]: "error"}, "error, not success"),
])
def test_each_required_circleci_status_must_be_its_latest_success(statuses, reason):
    d = _run(facts=_facts(statuses=statuses))
    assert not d.eligible and any(reason in r for r in d.reasons), d.reasons


def test_untrusted_or_malformed_status_evidence_refuses_even_if_the_latest_is_success():
    d = _run(facts=_facts(status_problems=["ci/circleci: frontend-tests was posted by an untrusted publisher"]))
    assert not d.eligible and any("untrusted or malformed" in r for r in d.reasons)


def test_incomplete_status_page_refuses():
    d = _run(facts=_facts(statuses_complete=False))
    assert not d.eligible and any("incomplete" in r for r in d.reasons)


def test_no_ci_evidence_at_all_is_fail_closed():
    d = _run(required_statuses=(), facts=_facts(checks=[], checks_complete=False))
    assert not d.eligible and any("not configured" in r for r in d.reasons)


def test_workflow_evidence_alone_is_eligible_when_no_statuses_are_required():
    assert _run(required_statuses=(), facts=_facts(statuses={})).eligible


@pytest.mark.parametrize("checks, complete", [
    ([{"name": "python-tests", "status": "completed", "conclusion": "failure"}], True),
    ([{"name": "python-tests", "status": "completed", "conclusion": "cancelled"}], True),
    ([{"name": "python-tests", "status": "completed", "conclusion": "skipped"}], True),
    ([{"name": "python-tests", "status": "in_progress", "conclusion": None}], True),
    (GREEN, False),   # a required workflow has no run on the head
])
def test_workflow_evidence_short_of_complete_success_refuses_without_statuses(checks, complete):
    d = _run(required_statuses=(), facts=_facts(statuses={}, checks=checks, checks_complete=complete))
    assert not d.eligible


def test_required_statuses_have_no_default_so_a_caller_cannot_skip_them():
    with pytest.raises(TypeError):
        evaluate(context=_context(), repository=REPO, task_key="TASK-0070", target_branch="main",
                 facts=_facts())


FORK = "agent/product"


def test_b5_pr_from_exactly_the_configured_fork_is_eligible():
    d = _run(facts=_facts(head_repo=FORK), head_repository=FORK)
    assert d.eligible, d.reasons


@pytest.mark.parametrize("head_repo", [REPO, "attacker/product", None])
def test_b5_with_a_fork_configured_any_other_head_is_refused(head_repo):
    d = _run(facts=_facts(head_repo=head_repo), head_repository=FORK)
    assert not d.eligible and any("head repository" in r for r in d.reasons)
