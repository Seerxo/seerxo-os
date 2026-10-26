"""Agent registry: identity from git, runtime state from the database.

`agents/registry.yaml` is the job description — reviewable, versioned, and the same for
Claude and Codex. The `agents` table holds runtime state. Foundation defines only CEO,
Developer, Supervisor and Research; specialist jobs are added when real work needs one.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import enums, models

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "agents" / "registry.yaml"
SKILLS_PATH = REGISTRY_PATH.parent.parent / "skills"

# Policies are merged key by key: an agent adding one forbidden tool must not silently
# drop the global list.
_MERGED_POLICY_KEYS = ("approval_policy", "context_policy", "memory_policy", "budget")

# Permission lists UNION with the defaults instead of replacing them. Without this an
# agent that declares `forbidden: [approve_own_work]` silently drops drop_database,
# send_money and rewrite_constitution from its own floor — a registry edit could hand
# itself powers the constitution denies. The safety floor may only ever grow.
_UNION_LIST_KEYS = {"forbidden", "require_approval", "auto", "exclude"}
_DEFAULT_LIST_FIELDS = (
    "allowed_tools", "forbidden_tools", "required_secrets", "projects", "skills",
)


@dataclass
class AgentSpec:
    key: str
    name: str
    role: str
    team: str
    reports_to: str
    enabled: bool
    description: str
    model_class: str
    autonomy_level: int
    owns: list[str]
    capabilities: list[str]
    allowed_tools: list[str]
    forbidden_tools: list[str]
    required_secrets: list[str]
    business_units: list[str]
    projects: list[str]
    skills: list[str]
    triggers: list[dict]
    schedule: dict | None
    work_check: str | None
    budget: dict
    context_policy: dict
    memory_policy: dict
    approval_policy: dict
    success_policy: dict
    prompt_version: str
    execution: str | None = None
    # Ordered provider preference for the unattended worker. Configurable, not a quality
    # ranking: capability routing changes as the CLIs do.
    providers: list[str] = field(default_factory=list)
    herdr: dict = field(default_factory=dict)
    enable_when: str | None = None
    blocked_by: str | None = None
    notes: str | None = None

    @property
    def daily_budget(self) -> float:
        return float(self.budget.get("daily_usd", 0) or 0)


class RegistryError(ValueError):
    pass


def _merge(default: Any, override: Any, key: str | None = None) -> Any:
    if isinstance(default, dict) and isinstance(override, dict):
        out = dict(default)
        for k, v in override.items():
            out[k] = _merge(default.get(k), v, k) if k in default else v
        return out
    if key in _UNION_LIST_KEYS and isinstance(default, list) and isinstance(override, list):
        # Order-preserving union, defaults first so the floor is always visible.
        return default + [item for item in override if item not in default]
    return override if override is not None else default


def load(path: Path | str | None = None) -> dict[str, AgentSpec]:
    raw = yaml.safe_load(Path(path or REGISTRY_PATH).read_text())
    defaults = raw.get("defaults") or {}
    specs: dict[str, AgentSpec] = {}

    for key, body in (raw.get("agents") or {}).items():
        merged = dict(body)
        for pol in _MERGED_POLICY_KEYS:
            merged[pol] = _merge(defaults.get(pol, {}), body.get(pol, {}))
        for simple in ("enabled", "autonomy_level", "model_class", "success_policy"):
            merged.setdefault(simple, defaults.get(simple))
        for list_field in _DEFAULT_LIST_FIELDS:
            inherited = list(defaults.get(list_field) or [])
            merged[list_field] = inherited + [
                item for item in (body.get(list_field) or []) if item not in inherited
            ]
        merged.setdefault("schedule", defaults.get("schedule"))
        merged.setdefault("prompt_version", defaults.get("prompt_version", "v1"))

        missing = [f for f in ("name", "role", "team", "description") if not merged.get(f)]
        if missing:
            raise RegistryError(f"agent '{key}' is missing {', '.join(missing)}")

        specs[key] = AgentSpec(
            key=key, name=merged["name"], role=merged["role"], team=merged["team"],
            reports_to=merged.get("reports_to") or "ceo",
            enabled=bool(merged.get("enabled")), description=merged["description"].strip(),
            model_class=merged.get("model_class") or "STANDARD",
            autonomy_level=int(merged.get("autonomy_level", 2)),
            owns=list(merged.get("owns") or []),
            capabilities=list(merged.get("capabilities") or []),
            allowed_tools=list(merged.get("allowed_tools") or []),
            forbidden_tools=list(merged.get("forbidden_tools") or []),
            required_secrets=list(merged.get("required_secrets") or []),
            business_units=list(merged.get("business_units") or []),
            projects=list(merged.get("projects") or []),
            skills=list(merged.get("skills") or []),
            triggers=list(merged.get("triggers") or []),
            schedule=merged.get("schedule"), work_check=merged.get("work_check"),
            budget=merged["budget"], context_policy=merged["context_policy"],
            memory_policy=merged["memory_policy"], approval_policy=merged["approval_policy"],
            success_policy=merged.get("success_policy") or {},
            prompt_version=merged.get("prompt_version") or "v1",
            execution=merged.get("execution"), herdr=merged.get("herdr") or {},
            providers=list(merged.get("providers") or defaults.get("providers") or []),
            enable_when=merged.get("enable_when"), blocked_by=merged.get("blocked_by"),
            notes=(merged.get("notes") or "").strip() or None,
        )
    return specs


def validate(specs: dict[str, AgentSpec], known_business_units: set[str] | None = None) -> list[str]:
    """Return problems rather than raising, so `companyctl agents --check` can list them."""
    problems: list[str] = []

    for key, s in specs.items():
        if s.enabled:
            # An enabled agent without a trigger can never wake, which is a silent
            # no-op rather than an error anyone would notice.
            if not s.triggers:
                problems.append(f"{key}: enabled but has no triggers — it can never wake")
            if s.blocked_by:
                problems.append(f"{key}: enabled while blocked_by={s.blocked_by}")
        else:
            if not (s.enable_when or s.blocked_by):
                problems.append(f"{key}: disabled with no enable_when — nobody knows when to turn it on")

        if s.reports_to != "founder" and s.reports_to not in specs:
            problems.append(f"{key}: reports_to references unknown agent '{s.reports_to}'")

        if s.autonomy_level > 3:
            # §113: the company target is level 2-3. Level 4 is broad autonomy and is
            # not something a registry edit should be able to grant.
            problems.append(f"{key}: autonomy_level {s.autonomy_level} exceeds the §113 ceiling of 3")

        if known_business_units is not None:
            unknown = [b for b in s.business_units if b not in known_business_units]
            if unknown:
                problems.append(f"{key}: unknown business units {unknown}")

        if not s.capabilities:
            problems.append(f"{key}: no capabilities")
        if not s.allowed_tools:
            problems.append(f"{key}: no allowed_tools")
        if not s.work_check:
            problems.append(f"{key}: no deterministic work_check")
        if any(t.get("type") == "schedule" for t in s.triggers) and not s.work_check:
            problems.append(f"{key}: scheduled without a deterministic work_check")
        if s.daily_budget > 0 or not s.budget.get("existing_access_only"):
            problems.append(
                f"{key}: zero-cost mode requires daily_usd=0 and existing_access_only=true"
            )

        available_skills = {
            path.parent.name for path in SKILLS_PATH.glob("*/SKILL.md")
        } if SKILLS_PATH.exists() else set()
        unknown_skills = sorted(set(s.skills) - available_skills)
        if unknown_skills:
            problems.append(f"{key}: unknown skills {unknown_skills}")

        forbidden = set(s.approval_policy.get("forbidden") or [])
        for never in ("rewrite_constitution", "drop_database", "send_money"):
            if never not in forbidden and s.model_class != "NONE":
                problems.append(f"{key}: '{never}' missing from forbidden — defaults were overridden")

    return problems


def enabled_budget(specs: dict[str, AgentSpec]) -> float:
    return round(sum(s.daily_budget for s in specs.values() if s.enabled), 2)


def sync(db: Session, specs: dict[str, AgentSpec]) -> dict:
    """Reconcile the registry into the agents table. Identity is overwritten from the
    file; runtime columns (heartbeat, last_success, last_error) are never touched."""
    created = updated = 0
    for key, s in specs.items():
        row = db.execute(select(models.Agent).where(models.Agent.key == key)).scalar_one_or_none()
        if row is None:
            row = models.Agent(key=key)
            db.add(row)
            created += 1
        else:
            updated += 1
        row.role = s.role
        row.description = s.description
        row.enabled = s.enabled
        row.autonomy_level = s.autonomy_level
        row.capabilities = {"owns": s.owns, "capabilities": s.capabilities,
                            "business_units": s.business_units, "projects": s.projects,
                            "skills": s.skills,
                            "model_class": s.model_class, "team": s.team,
                            "reports_to": s.reports_to,
                            "execution": s.execution}
        row.tools = {"allowed": s.allowed_tools, "forbidden": s.forbidden_tools,
                     "required_secrets": s.required_secrets,
                     "approval_policy": s.approval_policy, "herdr": s.herdr}
        row.limits = {"budget": s.budget, "context_policy": s.context_policy,
                      "memory_policy": s.memory_policy,
                      "triggers": s.triggers, "schedule": s.schedule,
                      "work_check": s.work_check,
                      "enable_when": s.enable_when, "blocked_by": s.blocked_by}
        row.prompt_version = s.prompt_version
    db.flush()

    stale_rows = [
        row for row in db.execute(select(models.Agent)).scalars() if row.key not in specs
    ]
    # Preserve removed definitions for audit (§23), but never leave them runnable.
    for row in stale_rows:
        row.enabled = False
    db.commit()
    stale = [row.key for row in stale_rows]
    return {"defined": len(specs), "created": created, "updated": updated,
            "enabled": sum(1 for s in specs.values() if s.enabled),
            "daily_budget_usd": enabled_budget(specs),
            # Rows not in the file are reported, never deleted (§23).
            "not_in_registry": stale}


def has_work_query_hint(spec: AgentSpec) -> str:
    """The deterministic check a scheduled agent runs before any model call (§36A)."""
    return (
        "SELECT count(*) FROM tasks WHERE status IN ('READY','QUEUED') "
        f"AND assigned_agent_id = (SELECT id FROM agents WHERE key = '{spec.key}')"
    )
