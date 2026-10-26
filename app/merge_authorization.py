"""Merge eligibility for a Stage C improvement PR: a deterministic, fail-closed decision.

`evaluate` answers one question: may this exact PR head be merged, given the recorded
independent review and the CI facts read from GitHub? It never merges. Merge authority stays
with the founder (stage_c_ci: a merge counts only when merged_by is the founder's login). This
module is the verifiable input an automated merge would need later; it grants no permission.

Every condition must hold. Anything missing, stale or inconsistent is a refusal with a reason:
  - the task is not blocked, and its latest PR_OPEN publication is the one reviewed
  - the approval names the latest tested fingerprint, which is the published fingerprint
  - the approving reviewer is a known provider different from the coder that wrote the change
  - the PR is the published PR of the expected repository, open and not a draft, and its head comes from
    exactly the expected head repository (the repository itself, or its one configured publish fork)
  - the PR head is exactly the published commit (a moved head is never judged)
  - every CI check completed with success on that head
  - every required commit status (CircleCI) has its latest status `success` on that head, from the
    trusted publisher, with no spoofed, malformed or incomplete status evidence

A model's free text (a Codex summary, a comment, a pending review) is never an input here.
Only the structured `approval` written by stage_c_runner.review counts.
"""
from __future__ import annotations

from dataclasses import dataclass, field

KNOWN_PROVIDERS = frozenset({"claude", "codex"})  # the Stage B providers; see app/providers.py


@dataclass
class Decision:
    eligible: bool
    reasons: list[str] = field(default_factory=list)


def _last(history: list, stage: str) -> dict:
    return next((h for h in reversed(history) if h.get("stage") == stage), {})


def evaluate(*, context: dict, repository: str, task_key: str, target_branch: str,
             facts: dict, blocked: bool = False, required_statuses: tuple[str, ...],
             head_repository: str | None = None) -> Decision:
    """`context` is the task's JSON context; `facts` are the GitHub facts read for its PR.
    `head_repository` is the one repository the PR head may come from (default: `repository`).
    `required_statuses` is the explicit set of commit-status contexts (from the trusted publisher)
    that must each be `success` on the head. It has no default: the caller must name it."""
    reasons: list[str] = []
    imp = (context or {}).get("improvement") or {}
    history = imp.get("history") or []

    if blocked:
        reasons.append("task is BLOCKED; the founder must look at it first")
    if imp.get("stage") != "READY_TO_MERGE":
        reasons.append(f"stage is {imp.get('stage')!r}, not READY_TO_MERGE")

    published = _last(history, "PR_OPEN").get("evidence") or {}
    tested = _last(history, "INDEPENDENT_REVIEW").get("evidence") or {}
    review_stage = _last(history, "INDEPENDENT_REVIEW")
    coder = (_last(history, "TESTING").get("evidence") or {}).get("coder")
    approval = imp.get("approval") or {}

    if not approval:
        reasons.append("no independent review approval recorded")
    else:
        if not isinstance(approval.get("audit_id"), int) or approval["audit_id"] <= 0:
            reasons.append("review approval has no audit record identity")
        if approval.get("fingerprint") != tested.get("fingerprint") or not tested.get("fingerprint"):
            reasons.append("approval is not for the latest tested fingerprint (stale or replayed)")
        if approval.get("fingerprint") != published.get("fingerprint"):
            reasons.append("published fingerprint differs from the approved one")
        if approval.get("review_stage_at") != review_stage.get("at") or not review_stage.get("at"):
            reasons.append("approval is not bound to the latest independent review attempt")
        reviewer, approved_coder = approval.get("reviewer"), approval.get("coder")
        if reviewer not in KNOWN_PROVIDERS or approved_coder not in KNOWN_PROVIDERS:
            reasons.append(f"reviewer {reviewer!r} or coder {approved_coder!r} is not a known provider")
        if reviewer == approved_coder:
            reasons.append("reviewer and coder are the same provider; review is not independent")
        if approved_coder != coder:
            reasons.append("approval names a different coder than the one that tested the change")

    url = published.get("pr") or ""
    if not published.get("commit") or not url.startswith(f"https://github.com/{repository}/pull/"):
        reasons.append("published PR does not belong to the expected repository")
    expected_branch = f"task/{task_key}"
    if published.get("branch") != expected_branch or facts.get("head_ref") != expected_branch:
        reasons.append("PR branch does not match the published task identity")
    if facts.get("head_repo") != (head_repository or repository):
        reasons.append("PR head repository does not match the authorized repository")
    if not target_branch or facts.get("base_ref") != target_branch:
        reasons.append("PR target branch does not match the authorized target")

    if facts.get("merged"):
        reasons.append("PR is already merged; nothing to authorize")
    elif facts.get("state") != "open":
        reasons.append(f"PR state is {facts.get('state')!r}, not open")
    if facts.get("draft"):
        reasons.append("PR is a draft")
    if published.get("commit") and facts.get("head_sha") != published.get("commit"):
        reasons.append("PR head is not the published commit; the reviewed bytes may not be the ones tested")

    checks = facts.get("checks") or []
    if not (facts.get("checks_complete") and checks):
        reasons.append("CI is not complete on the PR head")
    elif not all(c.get("status") == "completed" and c.get("conclusion") == "success" for c in checks):
        failing = [c.get("name") for c in checks if c.get("conclusion") != "success"]
        reasons.append(f"CI is not green on the PR head: {', '.join(map(str, failing)) or 'unknown'}")

    # Commit statuses (CircleCI) are a second, independent CI source: both must hold during migration.
    statuses = facts.get("statuses") or {}
    if not required_statuses and not checks:
        reasons.append("no CI evidence is required; the merge gate is not configured")
    if facts.get("status_problems"):
        reasons.append("commit status evidence is untrusted or malformed: " + "; ".join(facts["status_problems"]))
    if not facts.get("statuses_complete"):
        reasons.append("commit statuses are incomplete on the PR head")
    for context in required_statuses:
        if statuses.get(context) != "success":
            reasons.append(f"status {context!r} is {statuses.get(context) or 'missing'}, not success, on the PR head")

    return Decision(eligible=not reasons, reasons=reasons)
