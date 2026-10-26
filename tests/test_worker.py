"""Unattended worker: work-check, claim, provider fallback, supervision, retries."""
import json
import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import control, enums, models, providers, registry, worker
from app.providers import Health, Provider, ProviderResult


class FakeProvider(Provider):
    def __init__(self, name, outputs, health=Health.AVAILABLE):
        super().__init__(name=name, binary=name, environ={"PATH": ""})
        self.outputs, self.health, self.calls = list(outputs), health, 0

    def health_check(self):
        return self.health, None if self.health == Health.AVAILABLE else "fake"

    def _run(self, prompt, schema, **kw):
        self.calls += 1
        out = self.outputs.pop(0)
        if isinstance(out, ProviderResult):
            return out
        return ProviderResult(self.name, Health.AVAILABLE, output=out, model="fake")


def _good(key, **over):
    body = {"task_id": key, "agent_id": "research", "status": "completed",
            "summary": f"echoed selftest token for {key}", "actions": [],
            "evidence": [f"token selftest-x and {key}"], "artifacts": [],
            "learning_candidates": [], "new_task_candidates": [],
            "approval_request": None, "error": None}
    body.update(over)
    return body


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    for key in ("ceo", "developer", "supervisor", "research"):
        db.add(models.Agent(key=key, role=key, enabled=True))
    db.commit()
    return db


def _task(db, key="TASK-0100", autonomous=True, **over):
    research = db.execute(select(models.Agent).where(models.Agent.key == "research")).scalar_one()
    t = models.Task(key=key, title="probe", success_criteria="evidence has token",
                    objective="Echo the verification token",
                    status=enums.TaskStatus.READY, priority=enums.Priority.P3,
                    risk_level=enums.RiskLevel.LOW, assigned_agent_id=research.id,
                    context={"autonomous": autonomous, "scope": "company-os",
                             "verify": {"must_contain": ["selftest-x", key]}})
    for k, v in over.items():
        setattr(t, k, v)
    db.add(t)
    db.commit()
    return t


def _events(db, kind):
    return db.execute(select(func.count()).select_from(models.Event)
                      .where(models.Event.event_type == kind)).scalar_one()


SPECS = registry.load()


@pytest.mark.parametrize("codex_limited", [False, True])
def test_research_prefers_codex_then_falls_back_to_claude(codex_limited):
    db = _db()
    task = _task(db)
    codex_output = (ProviderResult("codex", Health.QUOTA_EXHAUSTED, error="usage limit")
                    if codex_limited else _good(task.key))
    out = worker.run_once(db, {"claude": FakeProvider("claude", [_good(task.key)]),
                               "codex": FakeProvider("codex", [codex_output])}, SPECS)
    run = db.get(models.AgentRun, out["run_id"])
    assert out["provider"] == ("claude" if codex_limited else "codex")
    assert [a["provider"] for a in run.tool_calls["attempts"]] == (
        ["codex", "claude"] if codex_limited else ["codex"])
    assert task.status == enums.TaskStatus.REVIEW and task.retry_count == 0


def test_no_work_means_no_model_call_and_a_heartbeat():
    db = _db()
    _task(db, autonomous=False)  # a real task the founder still drives through herdr
    fake = FakeProvider("claude", [])
    out = worker.run_once(db, {"claude": fake}, SPECS)
    assert out["model_calls"] == 0 and fake.calls == 0
    assert db.get(models.ControlState, "worker").value["last"] == "idle"
    # Break caught: an opted-out task silently picked up by the unattended worker.
    assert db.execute(select(models.Task)).scalar_one().status == enums.TaskStatus.READY


def test_success_reaches_review_never_done_with_supervisor_and_events():
    db = _db()
    task = _task(db)
    out = worker.run_once(db, {"claude": FakeProvider("claude", [_good(task.key)])}, SPECS)
    db.refresh(task)
    run = db.execute(select(models.AgentRun)).scalar_one()
    assert out["verdict"] == "APPROVED"
    assert task.status == enums.TaskStatus.REVIEW
    assert run.status == enums.AgentRunStatus.SUCCEEDED and run.provider == "claude"
    assert run.supervisor_verdict == enums.SupervisorVerdict.APPROVED
    assert _events(db, "TASK_STARTED") == 1 and _events(db, "TASK_COMPLETED") == 1


def test_missing_evidence_is_revision_not_success():
    db = _db()
    task = _task(db)
    worker.run_once(db, {"claude": FakeProvider("claude", [_good(task.key, evidence=["no token"])])}, SPECS)
    db.refresh(task)
    assert task.status == enums.TaskStatus.READY and task.retry_count == 1
    assert "missing required evidence" in db.execute(select(models.AgentRun.supervisor_notes)).scalar_one()


def test_malformed_output_fails_retries_with_backoff_then_stops_as_loop():
    db = _db()
    task = _task(db)
    fake = FakeProvider("claude", [{"nonsense": True}] * 3)
    worker.run_once(db, {"claude": fake}, SPECS)
    db.refresh(task)
    assert task.status == enums.TaskStatus.READY and task.retry_count == 1
    # Backoff: an immediate second tick must not burn another provider call.
    assert worker.run_once(db, {"claude": fake}, SPECS)["model_calls"] == 0
    for _ in range(2):
        last = db.execute(select(models.AgentRun).order_by(models.AgentRun.id.desc())).scalars().first()
        last.ended_at = last.ended_at - timedelta(hours=1)
        db.commit()
        worker.run_once(db, {"claude": fake}, SPECS)
    db.refresh(task)
    assert task.status == enums.TaskStatus.FAILED and fake.calls == 3
    loop = db.execute(select(models.Event).where(
        models.Event.event_type == "TASK_FAILED_REPEATEDLY")).scalar_one()
    assert "loop detected" in loop.payload["title"]
    # Never retry forever: exhausted tasks are not picked up again.
    assert worker.run_once(db, {"claude": fake}, SPECS)["model_calls"] == 0


def test_quota_exhausted_falls_back_to_the_next_zero_cost_provider():
    db = _db()
    task = _task(db)
    # Exercise Claude capacity classification even though the registry now prefers Codex.
    specs = {**SPECS, "research": dataclasses.replace(SPECS["research"], providers=["claude", "codex"])}
    claude = FakeProvider("claude", [ProviderResult("claude", Health.QUOTA_EXHAUSTED, error="usage limit")])
    codex = FakeProvider("codex", [_good(task.key)])
    out = worker.run_once(db, {"claude": claude, "codex": codex}, specs)
    assert out["provider"] == "codex" and out["verdict"] == "APPROVED"
    attempts = db.execute(select(models.AgentRun)).scalar_one().tool_calls["attempts"]
    assert [a["health"] for a in attempts] == ["QUOTA_EXHAUSTED", "AVAILABLE"]


def test_no_available_provider_waits_and_alerts_without_claiming():
    db = _db()
    task = _task(db)
    out = worker.run_once(db, {"claude": FakeProvider("claude", [], Health.AUTH_REQUIRED)}, SPECS)
    db.refresh(task)
    assert out["model_calls"] == 0 and task.status == enums.TaskStatus.READY
    assert _events(db, "WORKER_UNHEALTHY") == 1


def test_metered_credentials_trip_the_zero_cost_breaker():
    db = _db()
    task = _task(db)
    real = providers.ClaudeCodeProvider(environ={"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "x"})
    out = worker.run_once(db, {"claude": real}, SPECS)
    db.refresh(task)
    assert out["blocked"] == "zero_cost_guard" and task.status == enums.TaskStatus.READY
    assert _events(db, "INCIDENT_CREATED") == 1
    assert db.execute(select(models.Incident)).scalar_one().severity == enums.IncidentSeverity.SEV1


def test_claim_is_atomic():
    db = _db()
    task = _task(db)
    assert worker.claim(db, task) is True
    assert worker.claim(db, task) is False


def test_lost_run_after_restart_is_failed_and_requeued_once():
    db = _db()
    task = _task(db)
    assert worker.claim(db, task)  # as in production: the run belongs to the worker's claim
    db.add(models.AgentRun(task_id=task.id, agent_id=task.assigned_agent_id,
                           provider="claude", status=enums.AgentRunStatus.STARTED,
                           started_at=worker._now() - worker.STALE_AFTER - timedelta(minutes=1)))
    db.commit()
    assert worker.recover_stale(db) == [task.key]
    db.refresh(task)
    assert task.status == enums.TaskStatus.READY and task.retry_count == 1
    assert worker.recover_stale(db) == []  # no duplicate recovery


def test_pause_stops_the_worker():
    db = _db()
    _task(db)
    control.set_agents_paused(db, True, actor="founder", reason="test")
    fake = FakeProvider("claude", [])
    assert worker.run_once(db, {"claude": fake}, SPECS)["paused"] is True and fake.calls == 0


def test_selftest_task_is_idempotent_per_day_and_opted_in():
    db = _db()
    first = worker.create_selftest(db)
    assert worker.create_selftest(db) == first
    task = db.execute(select(models.Task).where(models.Task.key == first)).scalar_one()
    assert task.context["autonomous"] is True and task.risk_level == enums.RiskLevel.LOW


# --------------------------------------------------------------------------- adapters

def _runner(code, out, err="", seen=None):
    def run(argv, cwd, timeout, env, stdin=None):
        if seen is not None:
            seen.update(argv=argv, env=env, stdin=stdin)
        return code, out, err
    return run


def _session_limited_claude(structured=True):
    message = (Path(__file__).parent / "fixtures/claude-session-limit.txt").read_text().strip()
    # Message verbatim from production; JSON envelope models the CLI error path.
    out = json.dumps({"type": "result", "is_error": True, "result": message}) if structured else message
    p = providers.ClaudeCodeProvider(environ={"PATH": "/bin"}, runner=_runner(1, out))
    p.health_check = lambda: (Health.AVAILABLE, None)
    return p


@pytest.mark.parametrize("structured", [True, False])
def test_actual_claude_session_limit_is_capacity(structured):
    result = _session_limited_claude(structured).run("hi", {}, cwd="/tmp", timeout_s=5, max_turns=2)
    assert result.health == Health.QUOTA_EXHAUSTED and not result.ok


def test_session_limit_fallback_preserves_retry_and_cools_claude():
    db = _db()
    task = _task(db)
    # Exercise Claude capacity classification even though the registry now prefers Codex.
    specs = {**SPECS, "research": dataclasses.replace(SPECS["research"], providers=["claude", "codex"])}
    out = worker.run_once(db, {"claude": _session_limited_claude(),
                               "codex": FakeProvider("codex", [_good(task.key)])}, specs)
    db.refresh(task)
    assert out["provider"] == "codex" and task.status == enums.TaskStatus.REVIEW
    assert task.retry_count == 0 and _events(db, "TASK_FAILED") == 0
    assert worker._cooldowns(db)["claude"]["health"] == "QUOTA_EXHAUSTED"


def test_session_limit_no_capacity_does_not_spend_daily_work_budget():
    db = _db()
    task = _task(db)
    specs = {**SPECS, "research": dataclasses.replace(
        SPECS["research"], budget={**SPECS["research"].budget, "max_runs_per_day": 1})}
    out = worker.run_once(db, {"claude": _session_limited_claude(),
        "codex": FakeProvider("codex", [ProviderResult("codex", Health.RATE_LIMITED, error="429")])}, specs)
    assert out["verdict"] == "NO_CAPACITY"
    db.refresh(task)
    assert task.retry_count == 0 and task.status == enums.TaskStatus.READY
    assert _events(db, "TASK_FAILED") == 0
    assert worker._eligible(db, task, specs)[0] is None
    run = db.execute(select(models.AgentRun)).scalar_one()
    assert run.tool_calls["capacity_failure"] is True
    # Existing cooldown bounds probes; a repeated tick launches no CLI.
    assert worker.run_once(db, {"claude": _session_limited_claude()}, specs)["model_calls"] == 0
    state = db.get(models.ControlState, "worker")
    state.value = {**state.value, "cooldown": {}}
    db.commit()
    worker.run_once(db, {"codex": FakeProvider("codex", [_good(task.key)])}, specs)
    next_task = _task(db, "TASK-0101")
    assert worker._eligible(db, next_task, specs)[0] == "daily_run_budget"


@pytest.mark.parametrize("health", [Health.QUOTA_EXHAUSTED, Health.AUTH_REQUIRED, Health.RATE_LIMITED])
def test_legacy_capacity_only_runs_do_not_spend_daily_work_budget(health):
    db = _db()
    task = _task(db)
    db.add(models.AgentRun(agent_id=task.assigned_agent_id, task_id=task.id,
        provider="claude", status=enums.AgentRunStatus.FAILED,
        tool_calls={"attempts": [{"provider": "claude", "health": health.value}]}))
    db.commit()
    specs = {**SPECS, "research": dataclasses.replace(
        SPECS["research"], budget={**SPECS["research"].budget, "max_runs_per_day": 1})}
    assert worker._eligible(db, task, specs)[0] is None


@pytest.mark.parametrize("status", [enums.AgentRunStatus.STARTED, enums.AgentRunStatus.FAILED])
def test_unknown_or_real_work_run_still_spends_daily_budget(status):
    db = _db()
    task = _task(db)
    db.add(models.AgentRun(agent_id=task.assigned_agent_id, provider="claude", status=status))
    db.commit()
    specs = {**SPECS, "research": dataclasses.replace(
        SPECS["research"], budget={**SPECS["research"].budget, "max_runs_per_day": 1})}
    assert worker._eligible(db, task, specs)[0] == "daily_run_budget"


def test_claude_adapter_parses_structured_output_and_strips_metered_env():
    seen = {}
    payload = {"type": "result", "subtype": "success", "is_error": False,
               "structured_output": {"ok": 1}, "total_cost_usd": 0.12,
               "usage": {"input_tokens": 2, "output_tokens": 75},
               "modelUsage": {"claude-sonnet": {}}}
    p = providers.ClaudeCodeProvider(environ={"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "t",
                                              "DATABASE_URL": "postgresql://secret",
                                              "TELEGRAM_BOT_TOKEN": "secret"},
                                     runner=_runner(0, json.dumps(payload), seen=seen))
    p.health_check = lambda: (Health.AVAILABLE, None)
    r = p.run("hi", {}, cwd="/tmp", timeout_s=5, max_turns=2)
    assert r.ok and r.output == {"ok": 1} and r.list_cost_usd == 0.12 and r.model == "claude-sonnet"
    assert "--tools" in seen["argv"] and "dontAsk" in seen["argv"]
    assert "--dangerously-skip-permissions" not in seen["argv"]
    # Break caught: DATABASE_URL or a metered key reaching the model process.
    assert set(seen["env"]) == {"PATH", "CLAUDE_CODE_OAUTH_TOKEN",
                                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"}


def test_codex_adapter_classifies_usage_limit_as_quota_never_buys():
    events = "\n".join(json.dumps(e) for e in [
        {"type": "turn.started"},
        {"type": "turn.failed", "error": {"message": "You've hit your usage limit. "
                                                     "Upgrade to Pro or purchase more credits"}}])
    p = providers.CodexProvider(environ={"PATH": "/bin"}, runner=_runner(1, events))
    p.health_check = lambda: (Health.AVAILABLE, None)
    r = p.run("hi", {}, cwd="/tmp", timeout_s=5, max_turns=2)
    assert r.health == Health.QUOTA_EXHAUSTED and not r.ok


def test_codex_api_key_auth_is_metered(tmp_path):
    (tmp_path / "auth.json").write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "k"}))
    p = providers.CodexProvider(environ={"PATH": "/usr/bin:/bin", "CODEX_HOME": str(tmp_path)})
    p.binary = "sh"  # present on every host, so the auth check is what decides
    assert p.health_check()[0] == Health.METERED_BLOCKED


def test_codex_chatgpt_auth_file_is_configuration_not_live_model_proof(tmp_path):
    (tmp_path / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "refresh_token": "not-a-real-secret"}))
    p = providers.CodexProvider(environ={"PATH": "/usr/bin:/bin", "CODEX_HOME": str(tmp_path)})
    p.binary = "sh"
    assert p.health_check()[0] == Health.CREDENTIALS_CONFIGURED

    def answer(argv, cwd, timeout, env, stdin=None):
        Path(argv[argv.index("-o") + 1]).write_text('{"ok": true}')
        return 0, '{"type":"turn.completed","usage":{}}', ""

    p.runner = answer
    result = p.run("reply", {"type": "object"}, cwd="/tmp", timeout_s=5, max_turns=1)
    assert result.ok and result.health == Health.AVAILABLE


def test_worker_attempts_a_configured_provider_but_only_a_real_result_is_success():
    db = _db()
    task = _task(db)
    claude = FakeProvider("claude", [_good(task.key)], health=Health.CREDENTIALS_CONFIGURED)
    out = worker.run_once(db, {"claude": claude}, SPECS)
    assert claude.calls == 1 and out["model_calls"] == 1
    assert out["verdict"] == "APPROVED" and out["provider"] == "claude"


def test_timeout_is_a_failed_run_not_a_hang():
    p = providers.ClaudeCodeProvider(environ={"PATH": "/bin"}, runner=_runner(None, ""))
    p.health_check = lambda: (Health.AVAILABLE, None)
    r = p.run("hi", {}, cwd="/tmp", timeout_s=1, max_turns=1)
    assert not r.ok and "timeout" in r.error


def test_all_providers_out_of_capacity_spends_no_retry_and_cools_down():
    db = _db()
    task = _task(db)
    claude = FakeProvider("claude", [ProviderResult("claude", Health.AUTH_REQUIRED, error="Not logged in")])
    codex = FakeProvider("codex", [ProviderResult("codex", Health.QUOTA_EXHAUSTED, error="usage limit")])
    out = worker.run_once(db, {"claude": claude, "codex": codex}, SPECS)
    db.refresh(task)
    assert out["verdict"] == "NO_CAPACITY"
    assert task.status == enums.TaskStatus.READY and task.retry_count == 0
    # Break caught: an exhausted subscription probed again on every 60 s tick.
    again = worker.run_once(db, {"claude": claude, "codex": codex}, SPECS)
    assert again["model_calls"] == 0 and claude.calls == 1 and codex.calls == 1
    # One alert row for the outage, not one per tick while it cools down.
    assert _events(db, "WORKER_UNHEALTHY") == 1


def test_claude_without_login_is_auth_required_on_a_server(tmp_path, monkeypatch):
    monkeypatch.setattr(providers.sys, "platform", "linux")
    p = providers.ClaudeCodeProvider(environ={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    p.binary = "sh"
    assert p.health_check()[0] == Health.AUTH_REQUIRED
    p.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "t"
    assert p.health_check()[0] == Health.CREDENTIALS_CONFIGURED


# --------------------------------------------------------------------------- W3 + evals

def test_stage_b_never_claims_medium_risk_or_approval_gated_work():
    db = _db()
    _task(db, "TASK-0201", risk_level=enums.RiskLevel.MEDIUM)
    _task(db, "TASK-0202", risk_level=enums.RiskLevel.HIGH)
    _task(db, "TASK-0203", approval_required=True)
    fake = FakeProvider("claude", [])
    out = worker.run_once(db, {"claude": fake}, SPECS)
    assert out["model_calls"] == 0 and fake.calls == 0
    assert {s["reason"] for s in out["skipped"]} == {"needs_human_gate"}


def test_result_for_another_task_is_rejected():
    db = _db()
    task = _task(db)
    worker.run_once(db, {"claude": FakeProvider("claude", [_good("TASK-9999")])}, SPECS)
    db.refresh(task)
    assert task.status == enums.TaskStatus.READY
    assert "not TASK-0100" in db.execute(select(models.AgentRun.supervisor_notes)).scalar_one()


def test_model_saying_done_without_proof_is_not_done():
    db = _db()
    task = _task(db)
    claim = _good(task.key, summary="All done, everything works.", evidence=[])
    worker.run_once(db, {"claude": FakeProvider("claude", [claim])}, SPECS)
    db.refresh(task)
    assert task.status != enums.TaskStatus.REVIEW and task.status != enums.TaskStatus.DONE


def test_worker_state_feeds_desk_and_watchdog_without_inference():
    from app import watchdog
    db = _db()
    assert worker.worker_state(db)["status"] == "NO_DATA"
    assert watchdog.check_worker(db) == []  # never started: not a fault
    fake = FakeProvider("claude", [])
    worker.run_once(db, {"claude": fake}, SPECS)
    st = worker.worker_state(db)
    assert st["status"] == "HEALTHY" and st["providers"] == {"claude": "AVAILABLE"}
    assert fake.calls == 0
    # The Desk separates "foundation in the running code" from "repository writes enabled".
    assert st["autonomy"]["stage"] == "B" and st["autonomy"]["code_write_implemented"] is True
    assert st["autonomy"]["stage_c_foundation"] == ["lease", "worktree", "fingerprint", "lifecycle"]
    assert st["autonomy"]["auto_pr"] is False
    row = db.get(models.ControlState, "worker")
    row.value = {**row.value, "heartbeat_at": (worker._now() - timedelta(minutes=30)).isoformat()}
    db.commit()
    findings = watchdog.check_worker(db)
    assert [f.event_type for f in findings] == ["WORKER_UNHEALTHY"]
    assert findings[0].dedupe == "worker-heartbeat"


# --------------------------------------------------------------------------- subscription only

def test_worker_compose_environment_carries_no_metered_key_names():
    import yaml
    from pathlib import Path
    compose = yaml.safe_load((Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text())
    env = compose["services"]["company-worker"]["environment"]
    # Break caught: someone "fixes" a quota outage by wiring an API key into the worker.
    assert not set(env) & set(providers.METERED_ENV)
    assert env["ENABLE_AUTONOMOUS_CODE_WRITE"] == "false"


def test_quota_text_never_tells_anyone_to_buy_usage():
    raw = ("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
           "visit https://chatgpt.com/codex/settings/usage to purchase more credits or try "
           "again at 6:20 AM.")
    msg = providers.quota_message(raw)
    assert "purchase" not in msg and "Upgrade" not in msg and "http" not in msg
    assert "waiting for reset" in msg and "6:20 AM" in msg


def test_registry_rejects_any_money_budget():
    specs = registry.load()
    spec = specs["research"]
    spec.budget = {**spec.budget, "daily_usd": 5}
    assert any("zero-cost" in p for p in registry.validate(specs))


def test_turn_budget_comes_from_the_registry():
    db = _db()
    task = _task(db)
    seen = {}

    class Spy(FakeProvider):
        def run(self, prompt, schema, *, cwd, timeout_s, max_turns):
            seen["max_turns"] = max_turns
            return super().run(prompt, schema, cwd=cwd, timeout_s=timeout_s, max_turns=max_turns)

    worker.run_once(db, {"claude": Spy("claude", [_good(task.key)])}, SPECS)
    assert seen["max_turns"] == SPECS["research"].budget["max_turns_per_run"]


def test_cost_policy_keeps_seerxo_credit_out_of_agent_hands():
    import yaml
    from pathlib import Path
    policy = yaml.safe_load((Path(__file__).resolve().parent.parent / "agents" / "cost_policy.yaml").read_text())
    items, classes = policy["items"], set(policy["classes"])
    assert all(i["class"] in classes for i in items.values())
    seerxo = items["seerxo_openai"]
    assert seerxo["scope"] == "seerxo_product_only"
    assert seerxo["allow_agent_usage"] is False and seerxo["allow_new_topup"] is False
    assert items["claude_code"]["allow_metered_api"] is False
    assert items["codex"]["allow_metered_api"] is False
    # Break caught: an agent "using the Seerxo credit" as a fallback through the env.
    assert "OPENAI_API_KEY" in providers.METERED_ENV


def test_desk_reports_new_autonomous_spend_separately_from_approved_costs():
    from app import desk
    db = _db()
    view = desk.cost_view(db)
    assert view["new_autonomous_spend_usd"] == 0
    assert {i["key"] for i in view["items"]} >= {"vps_host", "seerxo_openai", "claude_code", "codex"}


def test_probe_needs_a_real_answer_not_just_a_login():
    ok = FakeProvider("claude", [{"ok": True}])
    quota = FakeProvider("codex", [ProviderResult("codex", Health.QUOTA_EXHAUSTED,
                                                     error="token-should-not-be-echoed")])
    out = worker.probe({"claude": ok, "codex": quota})
    # Break caught: a provider with a login file but no working path reported as fine.
    assert out["claude"]["ok"] is True and out["codex"] == {
        "health": "QUOTA_EXHAUSTED", "ok": False, "seconds": out["codex"]["seconds"]}
    assert "token-should-not-be-echoed" not in json.dumps(out)


def test_provider_probe_cli_can_probe_only_the_reviewer(monkeypatch, capsys):
    claude = FakeProvider("claude", [])
    codex = FakeProvider("codex", [{"ok": True}])
    monkeypatch.setattr(worker, "default_providers", lambda: {"claude": claude, "codex": codex})
    monkeypatch.setattr("sys.argv", ["company-worker", "--probe-provider", "codex"])
    assert worker.main() == 0
    assert claude.calls == 0 and codex.calls == 1
    output = capsys.readouterr().out
    assert '"codex"' in output and '"claude"' not in output


# --- the deliverable must exist, and nothing the agent returns is dropped (2026-10-10, TASK-0048) -------------
def _needs_items(db, n=1):
    task = _task(db)
    task.context = {**task.context, "verify": {"must_contain": ["selftest-x", task.key], "min_actions": n}}
    db.commit()
    return task


def test_a_task_that_requires_a_deliverable_is_not_approved_without_one():
    # Break caught: TASK-0048 said "the ranking below" and returned no ranking; the supervisor approved it.
    db = _db()
    task = _needs_items(db)
    worker.run_once(db, {"claude": FakeProvider("claude", [_good(task.key, actions=[])])}, SPECS)
    db.refresh(task)
    assert task.status == enums.TaskStatus.READY and task.retry_count == 1
    note = db.execute(select(models.AgentRun.supervisor_notes)).scalar_one()
    assert "deliverable missing" in note and "needs 1" in note


def test_blank_action_items_do_not_count_as_a_deliverable():
    db = _db()
    task = _needs_items(db)
    worker.run_once(db, {"claude": FakeProvider("claude", [_good(task.key, actions=["", "   "])])}, SPECS)
    db.refresh(task)
    assert task.status == enums.TaskStatus.READY


def test_the_deliverable_is_persisted_for_the_founder_and_never_dropped():
    db = _db()
    task = _needs_items(db, 2)
    items = ["H1: onboarding is unclear. Measure: first-screen exits.", "H2: sign-in wall. Measure: signups vs visits."]
    out = worker.run_once(db, {"claude": FakeProvider("claude", [_good(task.key, actions=items)])}, SPECS)
    db.refresh(task)
    assert out["verdict"] == "APPROVED" and task.status == enums.TaskStatus.REVIEW
    assert task.artifacts["actions"] == items and task.artifacts["evidence"]


def test_persisted_actions_are_bounded():
    db = _db()
    task = _task(db)
    many = [f"item {i}" for i in range(worker.MAX_ACTIONS + 10)]
    many[0] = "x" * (worker.MAX_ACTION_CHARS + 500)
    worker.run_once(db, {"claude": FakeProvider("claude", [_good(task.key, actions=many)])}, SPECS)
    db.refresh(task)
    stored = task.artifacts["actions"]
    assert len(stored) == worker.MAX_ACTIONS and len(stored[0]) == worker.MAX_ACTION_CHARS


def test_a_task_without_the_requirement_is_unchanged_and_stores_an_empty_list():
    db = _db()
    task = _task(db)    # verify has no min_actions: the self-test and packaging reviews behave as before
    out = worker.run_once(db, {"claude": FakeProvider("claude", [_good(task.key)])}, SPECS)
    db.refresh(task)
    assert out["verdict"] == "APPROVED" and task.artifacts["actions"] == []


@pytest.mark.parametrize("bad", [True, 0, -1, "1", None, 1.5])
def test_a_malformed_min_actions_requirement_is_ignored_not_trusted(bad):
    db = _db()
    task = _task(db)
    task.context = {**task.context, "verify": {"must_contain": ["selftest-x", task.key], "min_actions": bad}}
    db.commit()
    out = worker.run_once(db, {"claude": FakeProvider("claude", [_good(task.key)])}, SPECS)
    assert out["verdict"] == "APPROVED"


def test_the_prompt_tells_the_model_where_the_deliverable_goes():
    ctx = {"task": {"key": "TASK-1", "objective": "o", "title": "t", "description": "d", "success_criteria": "s"},
           "agent": {"key": "research", "role": "r"}}
    text = worker.envelope(ctx)
    assert "in actions" in text and "\"below\"" in text and "short statement" in text


def test_a_desk_command_requires_at_least_one_deliverable_item():
    from app import main, schemas
    db = _db()
    out = main.founder_desk_command(schemas.DeskCommand(text="Rank three hypotheses for the signup gap", agent="research"), db)
    task = db.execute(select(models.Task).where(models.Task.key == out["key"])).scalar_one()
    assert task.context["autonomous"] is True and task.context["verify"] == {"min_actions": 1}


DISABLED = "Your organization has disabled Claude subscription access for Claude Code · Use an Anthropic API key instead, or ask your admin to enable access"


def _per_token_runner(answers, calls):
    """answers: token -> (code, stdout). Records which token each run used and whether the second leaked."""
    def run(argv, cwd, timeout, env, stdin=None):
        calls.append((env.get("CLAUDE_CODE_OAUTH_TOKEN"), "CLAUDE_CODE_OAUTH_TOKEN_2" in env))
        return (*answers[env.get("CLAUDE_CODE_OAUTH_TOKEN")], "")
    return run


def test_a_disabled_subscription_is_a_login_problem_not_an_available_provider():
    """Production 2026-10-23: the reasoner's token belonged to an organization with Claude Code disabled, and the
    provider reported AVAILABLE, so nothing looked broken (gateway review 502, REVIEW_UNKNOWN)."""
    assert providers.classify(DISABLED) == Health.AUTH_REQUIRED
    # Ordinary failure text that merely mentions "disabled" and "access" stays an ordinary failure.
    assert providers.classify("Tool use is disabled for this session; cannot access files") == Health.BROKEN
    p = providers.ClaudeCodeProvider(environ={"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "a"},
                                     runner=_runner(1, json.dumps({"type": "result", "is_error": True, "result": DISABLED})))
    p.health_check = lambda: (Health.AVAILABLE, None)
    assert p.run("hi", {}, cwd="/tmp", timeout_s=5, max_turns=1).health == Health.AUTH_REQUIRED


def test_the_second_account_answers_only_when_the_first_could_not():
    ok = json.dumps({"type": "result", "is_error": False, "structured_output": {"verdict": "APPROVE"}})
    calls = []
    p = providers.ClaudeCodeProvider(
        environ={"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "a", "CLAUDE_CODE_OAUTH_TOKEN_2": "b"},
        runner=_per_token_runner({"a": (1, json.dumps({"type": "result", "is_error": True, "result": DISABLED})),
                                  "b": (0, ok)}, calls))
    p.health_check = lambda: (Health.AVAILABLE, None)
    r = p.run("hi", {}, cwd="/tmp", timeout_s=5, max_turns=1)
    assert r.ok and r.output == {"verdict": "APPROVE"}
    assert calls == [("a", False), ("b", False)]          # one token per run; the second never leaks as a variable


@pytest.mark.parametrize("first", [
    (0, json.dumps({"type": "result", "is_error": False, "structured_output": {"verdict": "REJECT"}})),
    (1, json.dumps({"type": "result", "is_error": True, "result": "tool error: something broke"}))])
def test_an_account_that_answered_or_failed_on_the_work_is_never_reasked(first):
    """No verdict shopping: a REJECT, or an ordinary failure, is the result; only login/quota/rate limit fall over."""
    calls = []
    p = providers.ClaudeCodeProvider(
        environ={"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "a", "CLAUDE_CODE_OAUTH_TOKEN_2": "b"},
        runner=_per_token_runner({"a": first, "b": (0, "{}")}, calls))
    p.health_check = lambda: (Health.AVAILABLE, None)
    p.run("hi", {}, cwd="/tmp", timeout_s=5, max_turns=1)
    assert [t for t, _ in calls] == ["a"]


def test_a_release_sized_prompt_reaches_claude_on_stdin_not_argv():
    """Production 2026-10-23: a 132 KB release review packet as one argv string raised E2BIG ("Argument list too
    long"), so every large release failed review. The prompt must travel on stdin; argv stays small."""
    seen = {}
    big = "x" * 300_000
    p = providers.ClaudeCodeProvider(environ={"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "t"}, runner=_runner(
        0, json.dumps({"type": "result", "is_error": False, "structured_output": {"ok": 1}}), seen=seen))
    p.health_check = lambda: (Health.AVAILABLE, None)
    assert p.run(big, {}, cwd="/tmp", timeout_s=5, max_turns=1).ok
    assert seen["stdin"] == big and big not in seen["argv"] and sum(map(len, seen["argv"])) < 10_000


def test_the_real_runner_feeds_stdin_beyond_the_argv_limit(tmp_path):
    code, out, _ = providers.subprocess_runner(["wc", "-c"], str(tmp_path), 10, {"PATH": "/usr/bin:/bin"}, "y" * 300_000)
    assert code == 0 and out.strip() == "300000"


def test_a_release_sized_prompt_reaches_codex_on_stdin_not_argv():
    """B-14 for codex: `codex exec -` reads the prompt from stdin; a 153 KB review packet in argv raised E2BIG."""
    seen = {}
    big = "x" * 300_000
    p = providers.CodexProvider(environ={"PATH": "/bin"}, runner=_runner(0, "", seen=seen))
    p.health_check = lambda: (Health.AVAILABLE, None)
    p.run(big, {}, cwd="/tmp", timeout_s=5, max_turns=1)
    assert seen["stdin"] == big and seen["argv"][-1] == "-" and sum(map(len, seen["argv"])) < 10_000
