"""The foundation roster and its persistent agent contract."""
import copy

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import models, registry


@pytest.fixture(scope="module")
def specs():
    return registry.load()


def test_foundation_has_exactly_four_enabled_agents(specs):
    # Break caught: speculative specialist definitions creep into the foundation.
    assert set(specs) == {"ceo", "developer", "supervisor", "research"}
    assert {key for key, spec in specs.items() if spec.enabled} == set(specs)


def test_org_chart_comes_from_registry_relationships(specs):
    # Break caught: the Desk draws a hard-coded hierarchy that drifts from job definitions.
    assert specs["ceo"].reports_to == "founder"
    assert {spec.reports_to for key, spec in specs.items() if key != "ceo"} == {"ceo"}


def test_every_enabled_agent_can_actually_wake(specs):
    for key, spec in specs.items():
        assert spec.triggers, f"{key} is enabled with no trigger"


def test_registry_validates_clean(specs):
    assert registry.validate(specs) == []


def test_production_image_includes_registry_skills():
    dockerfile = registry.REGISTRY_PATH.parent.parent / "Dockerfile"
    if dockerfile.exists():
        assert "COPY skills ./skills" in dockerfile.read_text()
    packaged = {path.parent.name for path in registry.SKILLS_PATH.glob("*/SKILL.md")}
    assert packaged >= {"company-state", "git", "server-health", "postgres-backup"}


def test_enabled_daily_budget_stays_small(specs):
    # This is a hard ceiling, not planned spend. A model call still requires work.
    assert registry.enabled_budget(specs) <= 10.0


def test_registry_validation_enforces_zero_cost_existing_access(specs):
    broken = dict(specs)
    agent = copy.deepcopy(specs["developer"])
    agent.budget = dict(agent.budget, daily_usd=1.0, existing_access_only=False)
    broken["developer"] = agent
    problems = registry.validate(broken)
    assert any("zero-cost" in problem for problem in problems)


def test_overriding_a_policy_cannot_shrink_the_safety_floor(specs):
    forbidden = set(specs["supervisor"].approval_policy["forbidden"])
    assert "approve_own_work" in forbidden
    assert "drop_database" in forbidden
    assert "rewrite_constitution" in forbidden
    assert "send_money" in forbidden


def test_every_agent_inherits_the_forbidden_floor(specs):
    floor = {"delete_repository", "drop_database", "docker_system_prune", "send_money",
             "disable_backups", "rewrite_constitution"}
    for key, spec in specs.items():
        missing = floor - set(spec.approval_policy.get("forbidden") or [])
        assert not missing, f"{key} is missing {sorted(missing)}"


def test_research_has_no_production_shell(specs):
    assert "production_shell" in specs["research"].forbidden_tools


def test_agent_contract_has_every_required_field(specs):
    # Break caught: identity exists as prose but runtime permissions and context cannot
    # be enforced because contract fields are missing.
    for key, spec in specs.items():
        assert spec.capabilities, key
        assert spec.allowed_tools, key
        assert spec.forbidden_tools, key
        assert spec.required_secrets == [], key
        assert isinstance(spec.projects, list), key
        assert spec.schedule is None or isinstance(spec.schedule, dict), key
        assert spec.skills, key
        assert spec.prompt_version == "v1", key
        assert "required_files" in spec.context_policy["include"], key


def test_registry_validation_rejects_missing_allowed_tools(specs):
    broken = dict(specs)
    agent = copy.deepcopy(specs["developer"])
    agent.allowed_tools = []
    broken["developer"] = agent
    assert any("no allowed_tools" in problem for problem in registry.validate(broken))


def test_registry_validation_rejects_unknown_skill(specs):
    broken = dict(specs)
    agent = copy.deepcopy(specs["research"])
    agent.skills = ["does-not-exist"]
    broken["research"] = agent
    assert any("unknown skills" in problem for problem in registry.validate(broken))


def test_no_agent_exceeds_the_autonomy_ceiling(specs):
    for key, spec in specs.items():
        assert spec.autonomy_level <= 3, f"{key} claims level {spec.autonomy_level}"


def test_model_class_is_used_not_a_model_id(specs):
    allowed = {"CHEAP", "STANDARD", "REASONING", "CRITICAL"}
    for key, spec in specs.items():
        assert spec.model_class in allowed, f"{key} has model_class {spec.model_class}"


def test_validation_catches_an_enabled_agent_with_no_trigger(specs):
    broken = dict(specs)
    agent = copy.deepcopy(specs["research"])
    agent.triggers = []
    broken["research"] = agent
    assert any("no triggers" in problem for problem in registry.validate(broken))


def test_validation_catches_autonomy_level_four(specs):
    broken = dict(specs)
    agent = copy.deepcopy(specs["developer"])
    agent.autonomy_level = 4
    broken["developer"] = agent
    assert any("exceeds the §113 ceiling" in problem for problem in registry.validate(broken))


def test_validation_catches_unknown_business_units(specs):
    problems = registry.validate(specs, known_business_units={"seerxo"})
    assert any("unknown business units" in problem for problem in problems)


def test_developer_routes_risk_to_herdr_workflows(specs):
    mapping = specs["developer"].herdr["workflow_by_risk"]
    assert mapping["LOW"] == "quick"
    assert mapping["HIGH"] == mapping["CRITICAL"] == "high-risk"


def test_work_check_is_sql_not_a_model_call(specs):
    hint = registry.has_work_query_hint(specs["developer"])
    assert hint.lstrip().upper().startswith("SELECT")
    assert "developer" in hint


def test_automatic_triggers_have_a_deterministic_work_check(specs):
    # Break caught: a cron trigger wakes a model just to discover there is no work.
    for key, spec in specs.items():
        if any(trigger["type"] == "schedule" for trigger in spec.triggers):
            assert spec.work_check, key


def test_sync_disables_a_removed_definition_without_deleting_its_history(specs):
    # Break caught: deleting a speculative YAML entry leaves its old DB row runnable.
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(models.Agent(key="watchdog", role="legacy", enabled=True))
        db.commit()
        result = registry.sync(db, {"ceo": specs["ceo"]})
        legacy = db.execute(
            select(models.Agent).where(models.Agent.key == "watchdog")
        ).scalar_one()
        assert legacy.enabled is False
        assert result["not_in_registry"] == ["watchdog"]
