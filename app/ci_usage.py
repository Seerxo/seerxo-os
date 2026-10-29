"""CI runner usage from GitHub Actions job metadata, against the Blacksmith free allowance. Estimates only.

BILLING_UNVERIFIED: no documented Blacksmith billing API is used. Every figure here is derived from GitHub's
job start and completion times, never from a Blacksmith invoice. Two estimates bound the truth:
- runtime minutes: exact job durations (a lower bound when a provider rounds jobs up);
- per-job ceiling minutes: each job rounded up to a whole minute (an upper bound for per-minute rounding).
Only blacksmith-2vcpu jobs are priced against the allowance. Other Blacksmith sizes are recorded as unpriced
(their multiplier is not documented here and is not guessed). Self-hosted and GitHub-hosted jobs are not
Blacksmith usage.

State lives in control_state["ci-usage"]: the jobs of recent runs keyed by job id (a re-poll overwrites the
same key, so nothing is counted twice; a re-run's jobs have new ids and are counted, because they ran again),
bounded to RETENTION_DAYS. The billing cycle is not known from any source: it is assumed to be the UTC calendar
month and labelled as an assumption.

Run hourly from cron:  docker exec company-api python -m app.ci_usage
Token: CI_USAGE_GITHUB_TOKEN, a fine-grained token with "Actions: read" on Seerxo/seerxo-os only. No token, no state.
"""
import math
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import enums, models

CONTROL_KEY = "ci-usage"
REPOSITORY = "Seerxo/seerxo-os"
ALLOWANCE_MINUTES = 3000
THRESHOLDS = ((0.70, "warning", enums.Severity.MEDIUM), (0.85, "high", enums.Severity.HIGH),
              (0.95, "critical", enums.Severity.CRITICAL), (1.00, "exhausted", enums.Severity.CRITICAL))
RETENTION_DAYS = 45
PAGE = 100
MAX_PAGES = 30
STALE_AFTER = timedelta(hours=3)
EXPENSIVE_JOB_MINUTES = 30
SPIKE_FACTOR = 2.0
SPIKE_MIN_MINUTES = 30
BLACKSMITH = re.compile(r"^blacksmith-(\d+)vcpu-")


class Unavailable(RuntimeError):
    """GitHub data could not be read completely. The reason never carries a response body."""


def _ts(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def classify(labels) -> tuple[str, int | None]:
    """(provider, vcpu) from a job's runner labels."""
    labels = [label for label in labels or [] if isinstance(label, str)]
    for label in labels:
        if match := BLACKSMITH.match(label):
            return "blacksmith", int(match[1])
    if "self-hosted" in labels:
        return "self-hosted", None
    if any(label.startswith(("ubuntu-", "windows-", "macos-")) for label in labels):
        return "github-hosted", None
    return "unknown", None


def job_record(job: dict, workflow: str, run_id: int) -> dict | None:
    """One job, reduced to what usage needs. None when GitHub's shape is not what is documented."""
    if not isinstance(job, dict) or type(job.get("id")) is not int:
        return None
    provider, vcpu = classify(job.get("labels"))
    started, completed = _ts(job.get("started_at")), _ts(job.get("completed_at"))
    seconds = None
    if job.get("status") == "completed" and started and completed and completed >= started:
        seconds = (completed - started).total_seconds()
    return {"run": run_id, "attempt": job.get("run_attempt") if type(job.get("run_attempt")) is int else None,
            "workflow": workflow, "provider": provider, "vcpu": vcpu, "status": job.get("status"),
            "conclusion": job.get("conclusion"), "seconds": seconds,
            "day": (completed or started).date().isoformat() if (completed or started) else None}


def _pages(fetch, path: str, key: str, params: dict | None = None) -> list:
    """Every item of a paginated GitHub list. Raises Unavailable instead of returning a partial list."""
    items, page, expected_total, seen = [], 1, None, set()
    while True:
        body = fetch(path, {**(params or {}), "per_page": PAGE, "page": page})
        rows = body.get(key) if isinstance(body, dict) else None
        total = body.get("total_count") if isinstance(body, dict) else None
        if not isinstance(rows, list) or type(total) is not int or total < 0:
            raise Unavailable(f"{key} list is malformed")
        if expected_total is None:
            expected_total = total
        if total != expected_total or len(rows) != min(PAGE, total - len(items)):
            raise Unavailable(f"{key} list is incomplete or changed during pagination")
        for row in rows:
            if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] in seen:
                raise Unavailable(f"{key} list has malformed or duplicate IDs")
            seen.add(row["id"])
        items += rows
        if len(items) == total:
            return items
        page += 1
        if page > MAX_PAGES:
            raise Unavailable(f"{key} list exceeds {MAX_PAGES} pages; refusing a partial count")


def ingest(state: dict, fetch, now: datetime, repository: str = REPOSITORY) -> dict:
    """Refresh the jobs of every run created in the retention window. Idempotent: jobs are keyed by id."""
    cutoff = (now - timedelta(days=RETENTION_DAYS)).date().isoformat()
    runs = _pages(fetch, f"repos/{repository}/actions/runs", "workflow_runs", {"created": f">={cutoff}"})
    known = state.get("runs") if isinstance(state.get("runs"), dict) else {}
    fresh: dict[str, dict] = {}
    for run in runs:
        if not isinstance(run, dict) or type(run.get("id")) is not int:
            raise Unavailable("workflow run is malformed")
        key, previous = str(run["id"]), known.get(str(run["id"]))
        stamp = (run.get("updated_at"), run.get("run_attempt"), run.get("status"))
        if previous and previous.get("stamp") == list(stamp) and run.get("status") == "completed":
            fresh[key] = previous            # unchanged and finished: no API call, no double count
            continue
        jobs = _pages(fetch, f"repos/{repository}/actions/runs/{run['id']}/jobs", "jobs", {"filter": "all"})
        records = {}
        for job in jobs:
            record = job_record(job, str(run.get("path") or run.get("name") or "unknown"), run["id"])
            if record is None:
                raise Unavailable("workflow job is malformed")
            records[str(job["id"])] = record
        fresh[key] = {"stamp": list(stamp), "jobs": records}
    return {"runs": fresh, "collected_at": now.isoformat(), "error": None}


def cycle(now: datetime) -> tuple[date, date]:
    """Assumed billing cycle: the UTC calendar month containing `now` (no source states the real one)."""
    start = now.astimezone(timezone.utc).date().replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def summarize(state: dict, now: datetime) -> dict:
    """Cycle-to-date estimates, per day and per workflow, with a forecast. Only completed jobs count."""
    start, end = cycle(now)
    days: dict[str, dict] = {}
    workflows: dict[str, dict] = {}
    unpriced = incomplete = 0.0
    expensive = []
    for run in (state.get("runs") or {}).values():
        for job_id, job in (run.get("jobs") or {}).items():
            if job.get("provider") != "blacksmith":
                continue
            if job.get("seconds") is None:
                incomplete += 1
                continue
            day = job.get("day")
            if not day or not (start.isoformat() <= day < end.isoformat()):
                continue
            if job.get("vcpu") != 2:
                unpriced += job["seconds"]
                continue
            seconds, ceiling = job["seconds"], math.ceil(job["seconds"] / 60)
            d = days.setdefault(day, {"runtime_minutes": 0.0, "ceiling_minutes": 0, "jobs": 0})
            d["runtime_minutes"] += seconds / 60
            d["ceiling_minutes"] += ceiling
            d["jobs"] += 1
            w = workflows.setdefault(job.get("workflow") or "unknown", {"runtime_minutes": 0.0, "ceiling_minutes": 0,
                                                                        "jobs": 0})
            w["runtime_minutes"] += seconds / 60
            w["ceiling_minutes"] += ceiling
            w["jobs"] += 1
            if seconds / 60 > EXPENSIVE_JOB_MINUTES:
                expensive.append({"job": job_id, "workflow": job.get("workflow"), "minutes": round(seconds / 60, 1)})
    low = sum(d["runtime_minutes"] for d in days.values())
    high = sum(d["ceiling_minutes"] for d in days.values())
    utc_now = now.astimezone(timezone.utc)
    today = utc_now.date()
    # The spike check compares today with the seven full days before it.
    week = [(today - timedelta(days=i)).isoformat() for i in range(1, 8)]
    avg7_high = sum(days.get(d, {}).get("ceiling_minutes", 0) for d in week) / 7
    avg7_low = sum(days.get(d, {}).get("runtime_minutes", 0.0) for d in week) / 7
    # The forecast rate covers at most the last seven days including today's elapsed part, and never the days
    # before usage began: dividing by days with no runner yet would understate the rate after a migration.
    first = min((date.fromisoformat(d) for d in days), default=today)
    window_start = max(today - timedelta(days=6), first)
    window = [d for d in days if window_start.isoformat() <= d <= today.isoformat()]
    elapsed = (today - window_start).days + (utc_now - datetime.combine(today, datetime.min.time(),
                                                                        tzinfo=timezone.utc)).total_seconds() / 86400
    rate_high = sum(days[d]["ceiling_minutes"] for d in window) / elapsed if elapsed > 0 else 0.0
    rate_low = sum(days[d]["runtime_minutes"] for d in window) / elapsed if elapsed > 0 else 0.0
    remaining_days = max(0.0, (datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc) - utc_now)
                         .total_seconds() / 86400)
    top = sorted(workflows.items(), key=lambda kv: -kv[1]["ceiling_minutes"])
    return {
        "billing": "BILLING_UNVERIFIED", "source": "estimated from GitHub job times",
        "cycle": {"start": start.isoformat(), "end": end.isoformat(), "basis": "assumed UTC calendar month"},
        "allowance_minutes": ALLOWANCE_MINUTES,
        "to_date": {"runtime_minutes": round(low, 1), "ceiling_minutes": high},
        "avg7_per_day": {"runtime_minutes": round(avg7_low, 1), "ceiling_minutes": round(avg7_high, 1)},
        "rate_per_day": {"runtime_minutes": round(rate_low, 1), "ceiling_minutes": round(rate_high, 1),
                         "window_days": round(elapsed, 2)},
        "forecast": {"runtime_minutes": round(low + rate_low * remaining_days, 1),
                     "ceiling_minutes": round(high + rate_high * remaining_days, 1)},
        "today": days.get(today.isoformat(), {"runtime_minutes": 0.0, "ceiling_minutes": 0, "jobs": 0}),
        "days": dict(sorted(days.items())),
        "top_workflows": [{"workflow": name, **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in w.items()}}
                          for name, w in top[:5]],
        "unpriced_runtime_minutes": round(unpriced / 60, 1), "incomplete_jobs": int(incomplete),
        "expensive_jobs": expensive,
    }


RECOMMENDATION = {
    ".github/workflows/python-tests.yml": "batch commits and run the Python suite locally before pushing to a PR",
}


def _advice(summary: dict) -> str:
    top = summary["top_workflows"][0]["workflow"] if summary["top_workflows"] else None
    return RECOMMENDATION.get(top, "avoid pushing intermediate commits to open PRs; each push re-runs every check")


def findings(db: Session, now: datetime | None = None) -> list:
    """Watchdog findings: estimated-risk thresholds, forecast overrun, spikes, expensive jobs, stale data.
    Nothing is reported before the first successful collection (an unconfigured source is not an alert)."""
    from app.watchdog import Finding  # imported here: the watchdog imports this module
    now = now or datetime.now(timezone.utc)
    row = db.get(models.ControlState, CONTROL_KEY)
    state = row.value if row is not None and isinstance(row.value, dict) else None
    if not state or not state.get("collected_at"):
        return []
    out = []
    collected = _ts(state.get("collected_at"))
    future = collected is not None and collected > now + timedelta(minutes=1)
    if collected is None or future or now - collected > STALE_AFTER or state.get("error"):
        age = "future" if future else f"{(now - collected).total_seconds() / 3600:.0f}h" if collected else "unknown"
        out.append(Finding("CI_USAGE_STALE", enums.Severity.MEDIUM, f"CI usage data is stale ({age})",
                           f"Last error: {state.get('error') or 'none'}. Check the ci-usage cron log and "
                           "CI_USAGE_GITHUB_TOKEN. Budget alerts are paused until data is fresh.",
                           dedupe="ci-usage-stale"))
        return out
    s = summarize(state, now)
    cycle_key, high, forecast = s["cycle"]["start"], s["to_date"]["ceiling_minutes"], s["forecast"]["ceiling_minutes"]
    top = s["top_workflows"][0] if s["top_workflows"] else None
    context = (f"Estimated usage {s['to_date']['runtime_minutes']}–{high} of {ALLOWANCE_MINUTES} min "
               f"(BILLING_UNVERIFIED: GitHub job times, not a Blacksmith invoice; cycle {s['cycle']['basis']}). "
               f"Forecast for the cycle: {s['forecast']['runtime_minutes']}–{forecast} min. "
               f"Top workflow: {top['workflow'] if top else 'none'}. Recommended: {_advice(s)}.")
    crossed = [t for t in THRESHOLDS if high >= t[0] * ALLOWANCE_MINUTES]
    if crossed:
        ratio, level, severity = crossed[-1]
        out.append(Finding("CI_BUDGET_ESTIMATED_RISK", severity,
                           f"CI usage estimate crossed {int(ratio * 100)}% ({level}) of the free allowance",
                           context, dedupe=f"ci-budget:{cycle_key}:{level}"))
    if forecast > ALLOWANCE_MINUTES and not crossed:
        out.append(Finding("CI_BUDGET_FORECAST_OVERRUN", enums.Severity.MEDIUM,
                           "CI usage is forecast to exceed the free allowance this cycle", context,
                           dedupe=f"ci-forecast:{cycle_key}"))
    today, avg7 = s["today"]["ceiling_minutes"], s["avg7_per_day"]["ceiling_minutes"]
    if today >= SPIKE_MIN_MINUTES and avg7 > 0 and today > SPIKE_FACTOR * avg7:
        out.append(Finding("CI_USAGE_SPIKE", enums.Severity.LOW,
                           f"CI usage today ({today} min) is over {SPIKE_FACTOR:g}x the 7-day average ({avg7})",
                           context, dedupe=f"ci-spike:{now.astimezone(timezone.utc).date().isoformat()}"))
    for job in s["expensive_jobs"]:
        out.append(Finding("CI_EXPENSIVE_JOB", enums.Severity.LOW,
                           f"A CI job ran {job['minutes']} min ({job['workflow']})", context,
                           dedupe=f"ci-expensive:{job['job']}"))
    return out


def collect(db: Session, fetch, now: datetime | None = None) -> dict:
    """Refresh control_state["ci-usage"]. On failure the previous jobs are kept and the error is recorded,
    so stale data is visible instead of silently becoming zero."""
    now = now or datetime.now(timezone.utc)
    row = db.get(models.ControlState, CONTROL_KEY)
    previous = row.value if row is not None and isinstance(row.value, dict) else {}
    try:
        state = ingest(previous, fetch, now)
    except Unavailable as error:
        state = {**previous, "error": str(error), "error_at": now.isoformat()}
    if row is None:
        db.add(models.ControlState(key=CONTROL_KEY, value=state, updated_by="ci-usage"))
    else:
        row.value, row.updated_by = state, "ci-usage"
    db.commit()
    return state


def _github_fetch(token: str):
    import httpx
    client = httpx.Client(base_url="https://api.github.com", timeout=30, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})

    def fetch(path: str, params: dict) -> dict:
        try:
            response = client.get("/" + path, params=params)
        except httpx.RequestError:
            raise Unavailable("GitHub request unavailable") from None
        if response.status_code != 200:   # status only: a body could echo request details
            raise Unavailable(f"GitHub returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise Unavailable("GitHub response is not JSON") from None
    return fetch


def main() -> int:
    token = os.environ.get("CI_USAGE_GITHUB_TOKEN", "")
    if not token:
        print("ci-usage: CI_USAGE_GITHUB_TOKEN not set; nothing recorded", file=sys.stderr)
        return 2
    from app.db import SessionLocal
    with SessionLocal() as db:
        state = collect(db, _github_fetch(token))
        if state.get("error"):
            print(f"ci-usage: {state['error']}", file=sys.stderr)
            return 1
        s = summarize(state, datetime.now(timezone.utc))
        print(f"ci-usage: {s['billing']} to_date={s['to_date']} forecast={s['forecast']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
