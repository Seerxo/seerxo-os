#!/usr/bin/env python3
"""Refuse a production deploy unless CI already passed on the exact commit.

The deploy runner is a 1 GB self-hosted host: it does not re-run the test suite. It reads the evidence for
the dispatched SHA and requires, from agents/repository_guardian.yaml:
- every `required_statuses` context `success`, posted by `status_publisher` (CircleCI; stdin, the raw
  GitHub statuses list). Shares app/commit_statuses.py with the Stage C merge gate, so a spoofed, pending,
  failed, missing or truncated status refuses here exactly as it refuses a merge;
- every `workflows` path's latest push run on main for that exact SHA `completed`/`success` (--runs, the
  GitHub Actions runs list). A pull_request run, another branch, another commit, a pending, failed or
  missing run, or a truncated list refuses.
"""
import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import commit_statuses  # noqa: E402

RUN_PAGE = 100
RUN_FIELDS = {"id": int, "path": str, "head_sha": str, "head_branch": str, "event": str, "status": str,
              "run_number": int, "run_attempt": int}


def evaluate(raw, contexts: tuple, publisher: dict) -> tuple:
    if not contexts:
        return True, []  # no status source; main() refuses unless required workflows are listed
    if not isinstance(publisher, dict) or not publisher.get("login") \
            or not isinstance(publisher.get("id"), int):
        return False, ["the trusted publisher or required statuses are not configured"]
    facts = commit_statuses.latest_statuses(raw, contexts, publisher)
    reasons = list(facts["status_problems"])
    if not facts["statuses_complete"]:
        reasons.append("commit statuses are incomplete (a full page; more may exist)")
    for context in contexts:
        state = facts["statuses"].get(context)
        if state != "success":
            reasons.append(f"{context!r} is {state or 'missing'}, not success")
    return not reasons, reasons


def evaluate_runs(raw, workflows: tuple, sha: str, branch: str = "main") -> tuple:
    """`raw` is {total_count, workflow_runs: [...]} for head_sha=sha, event=push, branch=main."""
    if not workflows:
        return True, []
    rows = raw.get("workflow_runs") if isinstance(raw, dict) else None
    total = raw.get("total_count") if isinstance(raw, dict) else None
    if not isinstance(rows, list) or type(total) is not int:
        return False, ["workflow run evidence is malformed"]
    if any(not isinstance(r, dict) or any(type(r.get(k)) is not t for k, t in RUN_FIELDS.items())
           or not (r.get("conclusion") is None or isinstance(r.get("conclusion"), str)) for r in rows):
        return False, ["workflow run evidence is malformed"]
    if total != len(rows) or len(rows) >= RUN_PAGE:
        return False, ["workflow runs are incomplete (more may exist)"]
    latest = {}
    for r in rows:
        # The API filters already; re-check so a run of another commit, branch or event never counts.
        if r["path"] not in workflows or r["head_sha"] != sha or r["head_branch"] != branch or r["event"] != "push":
            continue
        order = (r["run_number"], r["run_attempt"])
        if r["path"] not in latest or order > (latest[r["path"]]["run_number"], latest[r["path"]]["run_attempt"]):
            latest[r["path"]] = r
    reasons = []
    for path in workflows:
        r = latest.get(path)
        if r is None:
            reasons.append(f"{path} has no push run on {branch} for this commit")
        elif r["status"] != "completed" or r["conclusion"] != "success":
            reasons.append(f"{path} is {r['status']}/{r['conclusion']}, not completed/success")
    return not reasons, reasons


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--runs", help="GitHub Actions runs JSON for this commit (required when workflows are)")
    parser.add_argument("--policy", default=str(ROOT / "agents" / "repository_guardian.yaml"))
    args = parser.parse_args()
    policy = yaml.safe_load(Path(args.policy).read_text()) or {}
    workflows = policy.get("workflows")
    if not isinstance(workflows, list) or any(not isinstance(w, str) or not w.startswith(".github/workflows/")
                                              for w in workflows):
        print("DEPLOY REFUSED: the required workflows are not configured")
        return 1
    contexts = policy.get("required_statuses")
    if (not isinstance(contexts, list) or any(not isinstance(c, str) or not 0 < len(c) <= 255 for c in contexts)
            or len(set(contexts)) != len(contexts)):
        print("DEPLOY REFUSED: the required statuses are not configured")
        return 1
    if not workflows and not contexts:
        print("DEPLOY REFUSED: no CI evidence is configured (no required workflows or statuses)")
        return 1
    try:
        raw = json.load(sys.stdin) if contexts else []
        runs = json.loads(Path(args.runs).read_text()) if args.runs else None
    except (OSError, json.JSONDecodeError):
        print("DEPLOY REFUSED: invalid GitHub CI evidence")
        return 1
    allowed, reasons = evaluate(raw, tuple(contexts), policy.get("status_publisher"))
    if workflows and runs is None:
        allowed, reasons = False, reasons + ["workflow run evidence was not provided"]
    elif workflows:
        runs_ok, run_reasons = evaluate_runs(runs, tuple(workflows), args.sha)
        allowed, reasons = allowed and runs_ok, reasons + run_reasons
    if allowed:
        print(f"DEPLOY ALLOWED: CI passed on {args.sha}")
        return 0
    print(f"DEPLOY REFUSED: CI has not passed on {args.sha}: " + "; ".join(reasons))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
