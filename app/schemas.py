from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app import enums


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TaskCreate(BaseModel):
    title: str
    # §35 — refusing a task without success criteria is cheaper than an unverifiable one.
    success_criteria: str = Field(min_length=3)
    description: str | None = None
    business_unit: str | None = None
    project: str | None = None
    objective: str | None = None
    priority: enums.Priority = enums.Priority.P2
    risk_level: enums.RiskLevel = enums.RiskLevel.LOW
    status: enums.TaskStatus = enums.TaskStatus.BACKLOG
    approval_required: bool = False
    assigned_agent: str | None = None
    context: dict | None = None
    max_attempts: int = 3
    dedupe_key: str | None = None
    created_by: str = "founder"
    reason: str | None = None
    goal: int | None = Field(default=None, description="Goal id this work serves")


class TaskOut(ORM):
    key: str
    title: str
    status: enums.TaskStatus
    priority: enums.Priority
    risk_level: enums.RiskLevel
    approval_required: bool
    success_criteria: str
    retry_count: int
    created_at: datetime
    completed_at: datetime | None = None


class TaskStatusUpdate(BaseModel):
    status: enums.TaskStatus
    result: str | None = None
    # §45: an agent's claim of success is not evidence. DONE requires something checkable.
    evidence: str | None = None
    error: str | None = None
    actor: str = "operator"
    reason: str | None = None


class IncidentCreate(BaseModel):
    title: str
    description: str
    severity: enums.IncidentSeverity
    detected_by: str
    started_at: datetime | None = None
    service: str | None = None
    project: str | None = None
    root_cause: str | None = None


class IncidentResolve(BaseModel):
    """§94 — closing an incident without a root cause and a lesson wastes the incident."""
    root_cause: str = Field(min_length=10)
    resolution: str = Field(min_length=10)
    learning: str | None = None
    status: enums.IncidentStatus = enums.IncidentStatus.RESOLVED
    resolved_by: str = "operator"


class DecisionCreate(BaseModel):
    title: str
    decision: str
    reason: str
    approved_by: str = "founder"
    business_unit: str | None = None
    project: str | None = None
    supersedes: str | None = None


class NoteCreate(BaseModel):
    note: str = Field(min_length=5)
    agent: str | None = None
    task: str | None = None
    tags: str | None = None


class CompactionRequest(BaseModel):
    """Compaction proposes by default. `apply` is the deliberate second step."""
    apply: bool = False
    promote_after: int = 2
    retention_days: int = 30
    actor: str = "compaction"


class LearningApprove(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    reason: str | None = None


class LearningCreate(BaseModel):
    title: str
    lesson: str
    evidence: str | None = None
    business_unit: str | None = None
    project: str | None = None
    incident: str | None = None
    tags: str | None = None
    created_by: str = "operator"


class ApprovalCreate(BaseModel):
    """§112 — the founder must see all of this before deciding."""
    action: str
    reason: str
    expected_effect: str
    risk_level: enums.RiskLevel
    rollback: str
    requested_by: str
    task: str | None = None


class ApprovalDecision(BaseModel):
    granted: bool
    decided_by: str = "founder"
    notes: str | None = None


class AgentExecutionControl(BaseModel):
    paused: bool
    actor: str = "founder"
    reason: str = Field(min_length=3)


class PackagingIn(BaseModel):
    """A YouTube package from youtube-auto. History is newest first."""
    job: str = Field(min_length=3, max_length=200, pattern=r"^[A-Za-z0-9._-]+$")
    title: str = Field(min_length=3, max_length=120)
    thumbnailText: str = Field(min_length=1, max_length=60)
    history: list[dict] = Field(default_factory=list, max_length=50)


class PhaseUpdate(BaseModel):
    status: enums.PhaseStatus
    # A phase changes only with evidence: generated docs must not claim what nobody proved.
    evidence: str = Field(min_length=10)
    actor: str = "founder"


class TaskAssign(BaseModel):
    agent: str
    actor: str = "founder"
    reason: str | None = None


class DeskCommand(BaseModel):
    """A founder request typed into the desk. It becomes a task, never a shell command."""
    text: str = Field(min_length=3, max_length=2000)
    agent: str = "ceo"
    priority: enums.Priority = enums.Priority.P2
    # Optional work contract (§35). Autonomy and repository writes are not offered here:
    # Stage C admission decides those from its own policy, never from a Desk form.
    title: str | None = Field(default=None, min_length=3, max_length=200)
    objective: str | None = Field(default=None, max_length=2000)
    success_criteria: str | None = Field(default=None, min_length=3, max_length=2000)
    business_unit: str | None = Field(default=None, max_length=64)
    project: str | None = Field(default=None, max_length=64)
    goal: int | None = None
    risk_level: enums.RiskLevel = enums.RiskLevel.LOW


class RunReport(BaseModel):
    """A herdr control-center run, as reported by scripts/company-report.

    `herdr_state` is the control center's own state.json, passed through unchanged.
    Company OS interprets it rather than trusting a summary someone wrote about it.
    """
    herdr_state: dict
    learnings: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    reported_by: str = "company-report"


class EventIn(BaseModel):
    event_type: str
    source: str
    severity: enums.Severity = enums.Severity.INFO
    payload: dict | None = None
    business_unit: str | None = None
    project: str | None = None
    service: str | None = None
    task: str | None = None
    incident: str | None = None
    dedupe_key: str | None = None


class CheckpointIn(BaseModel):
    actor: str
    summary: str
    completed: dict | None = None
    unfinished: dict | None = None
    next_action: str | None = None


class BackupReport(BaseModel):
    """Written by scripts/backup.sh and restore-test.sh. §76 — 'uploaded' alone is not success."""
    target: str
    destination: str | None = None
    size_bytes: int | None = None
    checksum: str | None = None
    uploaded: bool = False
    integrity_verified: bool = False
    restore_tested: bool = False
    ok: bool = False
    error: str | None = None
    # True when the failure is a known, accepted gap (e.g. R2 credentials not issued yet).
    # The row is still recorded as not ok, but the founder is not paged for a state they
    # already know about — §111 forbids alert spam.
    expected: bool = False


class MetricIn(BaseModel):
    """A measured value. Unknown values are omitted, never sent as 0."""
    subject: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9:._/-]+$")
    metric: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    window: str = Field(default="", max_length=40, pattern=r"^[a-z0-9_]*$")
    value: float = Field(allow_inf_nan=False)
    unit: str | None = Field(default=None, max_length=20)
    source: str = Field(min_length=2, max_length=120, pattern=r"^[A-Za-z0-9._/-]+$")
    observed_at: datetime
    evidence: dict | None = None


class MetricsIn(BaseModel):
    business_unit: str = Field(min_length=1, max_length=64)
    observations: list[MetricIn] = Field(min_length=1, max_length=200)


class GoalSelector(BaseModel):
    """Which metric_observations row measures a goal (same vocabulary as MetricIn)."""
    subject: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9:._/-]+$")
    metric: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    window: str = Field(default="", max_length=40, pattern=r"^[a-z0-9_]*$")
    source: str | None = Field(default=None, min_length=2, max_length=120, pattern=r"^[A-Za-z0-9._/-]+$")


class GoalCreate(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    description: str | None = None
    metric: str = Field(min_length=2, max_length=200, description="Human description of the metric")
    baseline: str | None = Field(default=None, max_length=100)
    target: str = Field(min_length=1, max_length=100)
    deadline: datetime | None = None
    priority: enums.Priority = enums.Priority.P2
    project: str | None = None
    selector: GoalSelector | None = None
    status: enums.GoalStatus = enums.GoalStatus.PROPOSED
    created_by: str = "founder"
    reason: str | None = None


class GoalUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=300)
    description: str | None = None
    baseline: str | None = Field(default=None, max_length=100)
    target: str | None = Field(default=None, min_length=1, max_length=100)
    deadline: datetime | None = None
    priority: enums.Priority | None = None
    selector: GoalSelector | None = None
    status: enums.GoalStatus | None = None
    actor: str = "founder"
    reason: str | None = None


class ExperimentCreate(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    hypothesis: str = Field(min_length=3)
    business_unit: str
    metric_selector: dict
    expected_direction: str
    measurement_window_hours: int = Field(gt=0)
    min_effect: float = Field(default=0, ge=0, allow_inf_nan=False)
    goal_id: int | None = None
    project_id: int | None = None


class ExperimentAction(BaseModel):
    intervention_ref: str | None = Field(default=None, max_length=300)
    reason: str | None = None


class StageCAuthorize(BaseModel):
    """The SHA is never client-supplied: the server binds the authorization to the deployed source."""
    task_key: str = Field(pattern=r"^[A-Z]+-\d{1,6}$")
    minutes: int = Field(default=30, ge=1, le=60)


class StageCStanding(BaseModel):
    """Files and limits only: the SHA is the deployed source, never client-supplied."""
    paths: list[str] = Field(min_length=1, max_length=20)
    days: int = Field(default=3, ge=1, le=7)
    max_per_day: int = Field(default=1, ge=1, le=3)


class StageCRevoke(BaseModel):
    reason: str = Field(default="revoked", max_length=300)
