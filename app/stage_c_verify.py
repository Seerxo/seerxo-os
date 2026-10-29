"""Stage C after the merge: is the change live and healthy, and did it do what it promised?

Called by the worker control loop for merged tasks. Deterministic, no model call. `step` decides one move:

  MERGED    -> VERIFYING   main CI on the merge commit succeeded AND live code contains it
                           (caller proves both API/worker SHAs). Evidence: verified_at,
                           and the measured metric's baseline at that moment.
  MERGED    -> (BLOCKED)   main CI on the merge commit failed: a regression on main, Needs You.
  VERIFYING -> MEASURED    no metric named: outcome NOT_MEASURABLE, shown to the founder.
                           metric named: the first reading after verified_at + window_hours,
                           compared with the baseline in the promised direction.

A task names its metric in context.improvement.measure:
  {"selector": {"subject": "youtube:…", "metric": "views", "window": "", "source": "…"},
   "direction": "up" | "down", "window_hours": 24}
A task opened for a goal that names no metric is measured on the goal's own metric (measure_source "goal"):
direction from baseline to target, window at least the series' own window. That number is goal-wide, so a change
in it is recorded next to the task, never claimed as the task's effect.
Unknown is never success: missing CI, an unknown live SHA or a missing reading means wait.
"""
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, commit_statuses, enums, goals, models, stage_c

ACTOR = "stage-c-verify"
SHA = re.compile(r"[0-9a-f]{40}")
SERIES_WINDOW = re.compile(r"([1-9][0-9]{0,3})([hd])")


def _imp(task: models.Task) -> dict:
    return (task.context or {}).get("improvement") or {}


def _evidence(task: models.Task, stage: str) -> dict:
    return next((h.get("evidence") or {} for h in reversed(_imp(task).get("history", []))
                 if h["stage"] == stage), {})


def _goal_measure(db: Session, task: models.Task) -> dict | None:
    goal = db.get(models.Goal, task.goal_id) if task.goal_id else None
    if goal is None or goals.config_issues(goal):
        return None   # an unevaluable goal is not measured; the outcome stays NOT_MEASURABLE
    window = goal.metric_selector.get("window", "")
    match = SERIES_WINDOW.fullmatch(window)
    if window and not match:
        return None
    hours = int(match[1]) * (24 if match[2] == "d" else 1) if match else 0
    up = goals.number(goal.target) > goals.number(goal.baseline)
    return {"selector": goal.metric_selector, "direction": "up" if up else "down",
            "window_hours": max(24, hours), "measure_source": "goal", "goal_id": goal.id}


def _reading(db: Session, selector: dict | None, now: datetime, after: datetime | None = None):
    row = goals.latest(db, selector, now)
    if row is not None and after is not None:
        M = models.MetricObservation
        row = db.scalars(select(M).where(
            M.source == row.source, M.subject == row.subject, M.metric == row.metric, M.window == row.window,
            M.observed_at > after, M.observed_at <= now,
        ).order_by(M.observed_at.asc()).limit(1)).first()
    if row is None:
        return None
    at = row.observed_at if row.observed_at.tzinfo else row.observed_at.replace(tzinfo=timezone.utc)
    return {"value": float(row.value), "observed_at": at.isoformat()}


def step(db: Session, task: models.Task, facts: dict, now: datetime | None = None) -> dict:
    """facts: {"main_ci": "success" | "failure" | None, "live_contains_merge": bool | None}.
    Caller commits."""
    now = now or datetime.now(timezone.utc)
    imp = _imp(task)
    stage = imp.get("stage")
    if task.status == enums.TaskStatus.BLOCKED:
        return {"outcome": "SKIPPED", "reason": "blocked; waiting for the founder"}
    measure = imp.get("measure") or {}

    if stage == "MERGED":
        merge = _evidence(task, "MERGED").get("merge_commit")
        if facts.get("main_ci") == "failure":
            task.status = enums.TaskStatus.BLOCKED
            task.error = f"main CI failed on merge commit {str(merge)[:7]}: a regression on main"
            audit.record(db, actor=ACTOR, action="improvement.blocked", entity_type="task",
                         entity_key=task.key, after={"main_ci": "failure", "merge_commit": merge},
                         task_id=task.id)
            return {"outcome": "MAIN_CI_FAILED"}
        if facts.get("main_ci") != "success" or facts.get("live_contains_merge") is not True:
            return {"outcome": "WAITING", "reason": "main CI or deploy of the merge commit not confirmed"}
        derived = None if measure else _goal_measure(db, task)
        baseline = _reading(db, (measure or derived or {}).get("selector"), now) if measure or derived else None
        stage_c.advance(db, task, "VERIFYING", ACTOR, {"merge_commit": merge, "main_ci": "success",
                                                       "live": True, "verified_at": now.isoformat(),
                                                       "baseline": baseline, **({"measure": derived} if derived else {})})
        return {"outcome": "VERIFIED"}

    if stage == "VERIFYING":
        measure = measure or _evidence(task, "VERIFYING").get("measure") or {}
        if not measure:
            stage_c.advance(db, task, "MEASURED", ACTOR, {"outcome": "NOT_MEASURABLE",
                                                          "reason": "the task names no metric"})
            return {"outcome": "NOT_MEASURABLE"}
        ev = _evidence(task, "VERIFYING")
        verified = datetime.fromisoformat(ev["verified_at"])
        after = verified + timedelta(hours=int(measure.get("window_hours", 24)))
        if now < after:
            return {"outcome": "WAITING", "reason": f"measurement window ends {after.isoformat()}"}
        reading, baseline = _reading(db, measure.get("selector"), now, after=after), ev.get("baseline")
        if reading is None:
            return {"outcome": "WAITING", "reason": "no reading after the measurement window yet"}
        if baseline is None:
            outcome = "NO_BASELINE"
        else:
            delta = reading["value"] - baseline["value"]
            outcome = "IMPROVED" if (delta > 0 if measure.get("direction") == "up" else delta < 0) else "NOT_IMPROVED"
        stage_c.advance(db, task, "MEASURED", ACTOR, {"outcome": outcome, "baseline": baseline,
                                                      "reading": reading, "measure": measure})
        return {"outcome": outcome}

    return {"outcome": "SKIPPED", "reason": f"stage {stage}"}


def gh_facts(repository: str, merge_commit: str, live_sha: str | None, workflows: set[str], *,
             publisher: dict, status_contexts: tuple[str, ...], run=subprocess.run) -> dict:
    """Read-only: main CI on the merge commit (Actions push runs and the trusted CircleCI commit
    statuses), and whether the live SHA contains it. Error bodies are never surfaced."""
    if not SHA.fullmatch(merge_commit or ""):
        raise ValueError("invalid merge commit")
    if not isinstance(status_contexts, tuple) or (not workflows and not status_contexts):
        raise ValueError("explicit required CI workflows or status contexts are required")

    def api(path: str, jq: str):
        out = run(["gh", "api", "--method", "GET", path, "--jq", jq], capture_output=True, text=True, timeout=20)
        if out.returncode:
            raise RuntimeError("GitHub read failed")
        return json.loads(out.stdout)

    runs = api(f"repos/{repository}/actions/runs?head_sha={quote(merge_commit)}&event=push&branch=main&per_page=100",
               "{total_count, workflow_runs: [.workflow_runs[] | {path, head_sha, head_branch, event, status, conclusion, run_number, run_attempt}]}") if workflows else {
                   "total_count": 0, "workflow_runs": []}
    latest = {}
    for r in runs["workflow_runs"]:
        if r["path"] not in workflows or r["head_sha"] != merge_commit or r["head_branch"] != "main" or r["event"] != "push":
            continue
        if (r["path"] not in latest or
                                       (r["run_number"], r["run_attempt"]) > (latest[r["path"]]["run_number"], latest[r["path"]]["run_attempt"])):
            latest[r["path"]] = r
    statuses = commit_statuses.latest_statuses(
        api(f"repos/{repository}/commits/{merge_commit}/statuses?per_page={commit_statuses.STATUS_PAGE}",
            commit_statuses.STATUS_QUERY) if status_contexts else [], status_contexts, publisher)
    checks = [{"name": path, "status": r["status"], "conclusion": r["conclusion"]} for path, r in latest.items()]
    checks += commit_statuses.status_checks(statuses["statuses"], status_contexts)
    complete = (type(runs["total_count"]) is int and 0 <= runs["total_count"] == len(runs["workflow_runs"]) < 100
                and set(latest) == set(workflows) and statuses["statuses_complete"]
                and set(statuses["statuses"]) == set(status_contexts) and not statuses["status_problems"])
    if checks and complete and all(c["status"] == "completed" and c["conclusion"] == "success" for c in checks):
        main_ci = "success"
    elif any(c["status"] == "completed" and c["conclusion"] in ("failure", "timed_out", "error") for c in checks):
        main_ci = "failure"
    else:
        main_ci = None
    live = None
    if live_sha and SHA.fullmatch(live_sha):
        # An object, never a bare --jq string: gh prints a string result unquoted (ahead), which is not JSON.
        status = api(f"repos/{repository}/compare/{merge_commit}...{live_sha}", "{status}")["status"]
        live = status in ("identical", "ahead")
    return {"main_ci": main_ci, "live_contains_merge": live}
