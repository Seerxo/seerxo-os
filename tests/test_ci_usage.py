"""CI usage monitor: estimates from GitHub job times, never presented as Blacksmith billing.

A fake GitHub API serves runs and jobs with pagination and counts calls; no network is used.
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import ci_usage, enums, models

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
BS2 = ["blacksmith-2vcpu-ubuntu-2404"]


def job(job_id, seconds, *, labels=BS2, status="completed", conclusion="success", end=None, attempt=1):
    end = end or NOW - timedelta(hours=1)
    return {"id": job_id, "run_attempt": attempt, "labels": labels, "status": status, "conclusion": conclusion,
            "started_at": (end - timedelta(seconds=seconds)).isoformat(),
            "completed_at": end.isoformat() if status == "completed" else None}


class FakeGitHub:
    def __init__(self, runs, jobs):
        self.runs, self.jobs, self.calls = runs, jobs, []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        start = (params["page"] - 1) * params["per_page"]
        if path.endswith("/jobs"):
            rows = self.jobs[int(path.split("/")[-2])]
            return {"total_count": len(rows), "jobs": rows[start:start + params["per_page"]]}
        return {"total_count": len(self.runs), "workflow_runs": self.runs[start:start + params["per_page"]]}


def run(run_id, path=".github/workflows/python-tests.yml", attempt=1, status="completed", updated="t1"):
    return {"id": run_id, "path": path, "run_attempt": attempt, "status": status, "updated_at": updated}


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    return Session(engine)


def state_of(db):
    return db.get(models.ControlState, ci_usage.CONTROL_KEY).value


def test_runner_labels_are_classified_without_guessing_sizes():
    assert ci_usage.classify(BS2) == ("blacksmith", 2)
    assert ci_usage.classify(["blacksmith-4vcpu-ubuntu-2404"]) == ("blacksmith", 4)
    assert ci_usage.classify(["self-hosted", "linux", "x64", "sez-deploy"]) == ("self-hosted", None)
    assert ci_usage.classify(["ubuntu-latest"]) == ("github-hosted", None)
    assert ci_usage.classify(["something-else"]) == ("unknown", None)


def test_runtime_and_per_job_ceiling_bound_the_estimate():
    db = _db()
    gh = FakeGitHub([run(1)], {1: [job(10, 20), job(11, 61)]})
    ci_usage.collect(db, gh, NOW)
    s = ci_usage.summarize(state_of(db), NOW)
    assert s["to_date"]["runtime_minutes"] == pytest.approx(81 / 60, abs=0.06)   # 1.35, rounded to one decimal
    assert s["to_date"]["ceiling_minutes"] == 1 + 2
    assert s["billing"] == "BILLING_UNVERIFIED" and "estimated" in s["source"]
    assert s["cycle"]["basis"] == "assumed UTC calendar month"


def test_repeated_collection_never_double_counts_and_skips_finished_runs():
    db = _db()
    gh = FakeGitHub([run(1)], {1: [job(10, 120)]})
    ci_usage.collect(db, gh, NOW)
    first = ci_usage.summarize(state_of(db), NOW)["to_date"]
    ci_usage.collect(db, gh, NOW + timedelta(hours=1))
    ci_usage.collect(db, gh, NOW + timedelta(hours=2))
    assert ci_usage.summarize(state_of(db), NOW)["to_date"] == first
    assert sum(1 for path, _ in gh.calls if path.endswith("/jobs")) == 1   # finished, unchanged: not refetched


def test_a_rerun_is_counted_because_it_ran_again():
    db = _db()
    gh = FakeGitHub([run(1)], {1: [job(10, 120)]})
    ci_usage.collect(db, gh, NOW)
    gh.runs = [run(1, attempt=2, updated="t2")]
    gh.jobs = {1: [job(10, 120), job(12, 180, attempt=2)]}
    ci_usage.collect(db, gh, NOW)
    assert ci_usage.summarize(state_of(db), NOW)["to_date"]["ceiling_minutes"] == 2 + 3


def test_in_progress_jobs_are_not_counted_and_cancelled_jobs_are():
    db = _db()
    gh = FakeGitHub([run(1, status="in_progress")],
                    {1: [job(10, 600, status="in_progress"), job(11, 90, conclusion="cancelled")]})
    ci_usage.collect(db, gh, NOW)
    s = ci_usage.summarize(state_of(db), NOW)
    assert s["to_date"]["ceiling_minutes"] == 2 and s["incomplete_jobs"] == 1


def test_only_two_vcpu_blacksmith_jobs_count_against_the_allowance():
    db = _db()
    gh = FakeGitHub([run(1)], {1: [job(10, 60), job(11, 600, labels=["blacksmith-4vcpu-ubuntu-2404"]),
                                   job(12, 600, labels=["self-hosted", "sez-deploy"]),
                                   job(13, 600, labels=["ubuntu-latest"])]})
    ci_usage.collect(db, gh, NOW)
    s = ci_usage.summarize(state_of(db), NOW)
    assert s["to_date"]["ceiling_minutes"] == 1
    assert s["unpriced_runtime_minutes"] == 10.0          # 4-vCPU: recorded, multiplier not guessed


def test_the_cycle_boundary_excludes_last_month():
    db = _db()
    gh = FakeGitHub([run(1)], {1: [job(10, 60, end=datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)),
                                   job(11, 60, end=datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc))]})
    ci_usage.collect(db, gh, NOW)
    s = ci_usage.summarize(state_of(db), NOW)
    assert s["to_date"]["ceiling_minutes"] == 1 and list(s["days"]) == ["2026-10-01"]


def test_pagination_reads_every_page():
    db = _db()
    runs = [run(i) for i in range(1, 151)]
    gh = FakeGitHub(runs, {i: [job(1000 + i, 30)] for i in range(1, 151)})
    ci_usage.collect(db, gh, NOW)
    assert ci_usage.summarize(state_of(db), NOW)["to_date"]["ceiling_minutes"] == 150
    assert {p["page"] for path, p in gh.calls if path.endswith("/runs")} == {1, 2}


@pytest.mark.parametrize("key", ["workflow_runs", "jobs"])
@pytest.mark.parametrize("pages", [
    [(-1, [1])], [(True, [1])], [(0, [1])], [(2, [1])], [(1, [1, 2])],
    [(3, [1, 2, 3])], [(3, [1, 2]), (4, [3, 4])], [(3, [1, 2]), (2, [])],
    [(4, [1, 2]), (4, [3])], [(2, [1, 1])], [(3, [1, 2]), (3, [1])],
])
def test_incomplete_or_contradictory_pages_never_prove_usage(monkeypatch, key, pages):
    monkeypatch.setattr(ci_usage, "PAGE", 2)
    def fetch(path, params):
        total, ids = pages[params["page"] - 1]
        return {"total_count": total, key: [{"id": value} for value in ids]}
    with pytest.raises(ci_usage.Unavailable):
        ci_usage._pages(fetch, "fixture", key)


@pytest.mark.parametrize("truncated", ["workflow_runs", "jobs"])
def test_incomplete_inventory_preserves_known_usage_without_fresh_proof(truncated):
    db = _db()
    previous = ci_usage.collect(db, FakeGitHub([run(1), run(2)], {
        1: [job(10, 120), job(11, 240)], 2: [job(20, 180)]}), NOW)
    def fetch(path, params):
        if path.endswith("/runs"):
            rows = [run(1, updated="t2"), run(2)]
            return {"total_count": 2, "workflow_runs": rows[:1] if truncated == "workflow_runs" else rows}
        return {"total_count": 2, "jobs": [job(10, 120)]}
    state = ci_usage.collect(db, fetch, NOW + timedelta(hours=1))
    assert state["runs"] == previous["runs"] and state["collected_at"] == previous["collected_at"]
    assert state["error"] and state["error_at"] == (NOW + timedelta(hours=1)).isoformat()
    assert ci_usage.summarize(state, NOW)["to_date"]["ceiling_minutes"] == 9
    assert [f.event_type for f in ci_usage.findings(db, NOW)] == ["CI_USAGE_STALE"]


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_transport_failure_preserves_usage_and_records_only_a_sanitized_error(monkeypatch, error):
    db = _db()
    previous = ci_usage.collect(db, FakeGitHub([run(1)], {1: [job(10, 60)]}), NOW)
    class Client:
        def __init__(self, **kwargs):
            pass
        def get(self, *args, **kwargs):
            raise error("private transport response with test-only-token")
    monkeypatch.setattr(httpx, "Client", Client)
    state = ci_usage.collect(db, ci_usage._github_fetch("test-only-token"), NOW + timedelta(hours=1))
    assert state["runs"] == previous["runs"] and state["collected_at"] == previous["collected_at"]
    assert state["error"] and "private" not in state["error"] and "test-only-token" not in state["error"]
    assert [f.event_type for f in ci_usage.findings(db, NOW)] == ["CI_USAGE_STALE"]


def test_a_list_beyond_the_page_limit_is_an_error_not_a_partial_count(monkeypatch):
    db = _db()
    monkeypatch.setattr(ci_usage, "MAX_PAGES", 1)
    ci_usage.collect(db, FakeGitHub([run(1)], {1: [job(10, 60)]}), NOW)          # a good first collection
    gh = FakeGitHub([run(i) for i in range(1, 151)], {i: [job(1000 + i, 30)] for i in range(1, 151)})
    state = ci_usage.collect(db, gh, NOW + timedelta(hours=1))
    assert "exceeds" in state["error"]
    assert ci_usage.summarize(state, NOW)["to_date"]["ceiling_minutes"] == 1   # previous data kept, not zeroed


@pytest.mark.parametrize("body", [{"workflow_runs": "x", "total_count": 1}, {"total_count": 1}, ["not", "a", "dict"]])
def test_malformed_github_data_is_an_error_and_keeps_previous_data(body):
    db = _db()
    ci_usage.collect(db, FakeGitHub([run(1)], {1: [job(10, 60)]}), NOW)
    state = ci_usage.collect(db, lambda path, params: body, NOW + timedelta(hours=1))
    assert "malformed" in state["error"] and state["runs"]


def test_only_this_repositorys_actions_endpoints_are_read():
    db = _db()
    gh = FakeGitHub([run(1)], {1: [job(10, 60)]})
    ci_usage.collect(db, gh, NOW)
    assert all(path.startswith("repos/Seerxo/os/actions/") for path, _ in gh.calls)


# --------------------------------------------------------------------------- findings


def seed_state(db, jobs, collected_at=NOW, error=None):
    db.add(models.ControlState(key=ci_usage.CONTROL_KEY, updated_by="test", value={
        "runs": {"1": {"stamp": ["t", 1, "completed"], "jobs": {str(i): j for i, j in enumerate(jobs)}}},
        "collected_at": collected_at.isoformat(), "error": error}))
    db.commit()


def bs(minutes, day="2026-10-19", workflow=".github/workflows/python-tests.yml"):
    return {"provider": "blacksmith", "vcpu": 2, "seconds": minutes * 60, "day": day, "workflow": workflow,
            "status": "completed", "conclusion": "success", "run": 1, "attempt": 1}


def test_no_findings_before_the_first_collection():
    assert ci_usage.findings(_db(), NOW) == []


def test_stale_or_failing_data_pauses_budget_alerts():
    db = _db()
    seed_state(db, [bs(30)] * 80, collected_at=NOW - timedelta(hours=4))
    [f] = ci_usage.findings(db, NOW)
    assert f.event_type == "CI_USAGE_STALE" and f.dedupe == "ci-usage-stale"


@pytest.mark.parametrize("seconds", [61, 86400])
def test_future_collection_time_is_not_fresh_budget_evidence(seconds):
    db = _db()
    seed_state(db, [bs(10)], collected_at=NOW + timedelta(seconds=seconds))
    [finding] = ci_usage.findings(db, NOW)
    assert finding.event_type == "CI_USAGE_STALE" and "future" in finding.title


@pytest.mark.parametrize("seconds", [0, 60])
def test_small_collection_clock_skew_does_not_invalidate_usage(seconds):
    db = _db()
    seed_state(db, [bs(10)], collected_at=NOW + timedelta(seconds=seconds))
    assert not any(f.event_type == "CI_USAGE_STALE" for f in ci_usage.findings(db, NOW))


@pytest.mark.parametrize("jobs, level, severity", [
    (71, "warning", enums.Severity.MEDIUM),     # 71 x 30 = 2130 >= 2100
    (86, "high", enums.Severity.HIGH),          # 2580 >= 2550
    (96, "critical", enums.Severity.CRITICAL),  # 2880 >= 2850
    (100, "exhausted", enums.Severity.CRITICAL),
])
def test_threshold_alerts_are_labelled_estimates_and_deduplicated_per_cycle(jobs, level, severity):
    db = _db()
    seed_state(db, [bs(30)] * jobs)
    risk = [f for f in ci_usage.findings(db, NOW) if f.event_type == "CI_BUDGET_ESTIMATED_RISK"]
    assert len(risk) == 1 and risk[0].severity == severity
    assert risk[0].dedupe == f"ci-budget:2026-10-01:{level}"
    assert "BILLING_UNVERIFIED" in risk[0].detail and "not a Blacksmith invoice" in risk[0].detail
    assert "Recommended:" in risk[0].detail and "Top workflow" in risk[0].detail
    assert [f.dedupe for f in ci_usage.findings(db, NOW)] == [f.dedupe for f in ci_usage.findings(db, NOW)]


def test_below_every_threshold_there_is_no_budget_alert():
    db = _db()
    seed_state(db, [bs(30)] * 10, )
    assert not [f for f in ci_usage.findings(db, NOW) if f.event_type.startswith("CI_BUDGET")]


def test_a_forecast_overrun_is_reported_before_a_threshold_is_crossed():
    db = _db()
    days = [(NOW.date() - timedelta(days=i)).isoformat() for i in range(1, 8)]
    seed_state(db, [bs(29, day=d) for d in days for _ in range(8)])   # 56 x 29 = 1624 so far; 232/day, 12 days left
    kinds = [f.event_type for f in ci_usage.findings(db, NOW)]
    assert "CI_BUDGET_FORECAST_OVERRUN" in kinds and "CI_BUDGET_ESTIMATED_RISK" not in kinds


def test_a_spike_and_an_expensive_job_are_reported_once_each():
    db = _db()
    today = NOW.date().isoformat()
    seed_state(db, [bs(5, day=(NOW.date() - timedelta(days=2)).isoformat()), bs(45, day=today)])
    found = {f.event_type: f for f in ci_usage.findings(db, NOW)}
    assert found["CI_USAGE_SPIKE"].dedupe == f"ci-spike:{today}"
    assert found["CI_EXPENSIVE_JOB"].dedupe.startswith("ci-expensive:")


def test_a_short_history_is_not_diluted_by_days_before_usage_began():
    """Usage that began yesterday must not be averaged over seven days (the post-migration undercount)."""
    yesterday, today = (NOW.date() - timedelta(days=1)).isoformat(), NOW.date().isoformat()
    state = {"runs": {"1": {"jobs": {str(i): bs(10, day=d) for i, d in enumerate([yesterday] * 30 + [today] * 15)}}}}
    s = ci_usage.summarize(state, NOW)                        # NOW is 12:00 UTC: 1.5 days of usage
    assert s["rate_per_day"]["window_days"] == 1.5
    assert s["rate_per_day"]["ceiling_minutes"] == 300.0      # 450 minutes over 1.5 days
    assert s["forecast"]["ceiling_minutes"] == 450 + 300 * 11.5   # 11.5 days left in October
    assert s["forecast"]["ceiling_minutes"] > ci_usage.ALLOWANCE_MINUTES
