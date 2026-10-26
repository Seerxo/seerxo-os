"""Company OS persistent state. This is the company memory (§0, §164) — the database
is the source of truth, not any conversation."""
from datetime import datetime

from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app import enums


class Base(DeclarativeBase):
    pass


# Postgres enum types are created once and reused. Defining Enum(...) inline per column
# with the same name makes create_all emit CREATE TYPE repeatedly and fail.
PriorityType = Enum(enums.Priority, name="priority")
RiskLevelType = Enum(enums.RiskLevel, name="risk_level")
SeverityType = Enum(enums.Severity, name="severity")



def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TimestampMixin:
    """All timestamps are UTC (§154). Founder-facing conversion happens at the edge."""
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BusinessUnit(Base, TimestampMixin):  # §28
    __tablename__ = "business_units"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[enums.BusinessUnitStatus] = mapped_column(
        Enum(enums.BusinessUnitStatus, name="business_unit_status"),
        default=enums.BusinessUnitStatus.BACKLOG,
    )
    priority: Mapped[enums.Priority] = mapped_column(
        PriorityType, default=enums.Priority.P3
    )
    owner: Mapped[str] = mapped_column(String(100), default="founder")
    revenue_model: Mapped[str | None] = mapped_column(String(200))
    # §147 — a unit may cap its own agent spend. NULL means "not yet budgeted".
    daily_ai_budget_usd: Mapped[float | None] = mapped_column(Numeric(10, 2))
    max_concurrent_tasks: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)

    projects: Mapped[list["Project"]] = relationship(back_populates="business_unit")


class Project(Base, TimestampMixin):  # §29
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    business_unit_id: Mapped[int | None] = mapped_column(ForeignKey("business_units.id"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[enums.ProjectStatus] = mapped_column(
        Enum(enums.ProjectStatus, name="project_status"), default=enums.ProjectStatus.BACKLOG
    )
    priority: Mapped[enums.Priority] = mapped_column(
        PriorityType, default=enums.Priority.P3
    )
    repository: Mapped[str | None] = mapped_column(String(300))
    production_url: Mapped[str | None] = mapped_column(String(300))
    objective: Mapped[str | None] = mapped_column(Text)
    # Free-text status sentence, not a short label — Text avoids truncation.
    current_phase: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    business_unit: Mapped[BusinessUnit | None] = relationship(back_populates="projects")


class Goal(Base, TimestampMixin):  # §30 — a goal without a metric is not a goal
    __tablename__ = "goals"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    metric: Mapped[str] = mapped_column(String(200))
    baseline: Mapped[str | None] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(100))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority: Mapped[enums.Priority] = mapped_column(
        PriorityType, default=enums.Priority.P2
    )
    status: Mapped[enums.GoalStatus] = mapped_column(
        Enum(enums.GoalStatus, name="goal_status"), default=enums.GoalStatus.PROPOSED
    )
    created_by: Mapped[str] = mapped_column(String(100), default="founder")
    # Which metric_observations row measures this goal: {subject, metric, window, source?}.
    # Progress is computed from the newest verified observation; no selector, no progress.
    metric_selector: Mapped[dict | None] = mapped_column(JSON)


class Service(Base, TimestampMixin):  # §22 service registry
    __tablename__ = "services"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    purpose: Mapped[str | None] = mapped_column(Text)
    business_unit_id: Mapped[int | None] = mapped_column(ForeignKey("business_units.id"))
    server: Mapped[str] = mapped_column(String(80), default="company-os-host")
    path: Mapped[str | None] = mapped_column(String(300))
    repository: Mapped[str | None] = mapped_column(String(300))
    runtime: Mapped[str | None] = mapped_column(String(200))
    container: Mapped[str | None] = mapped_column(String(200))
    ports: Mapped[str | None] = mapped_column(String(200))
    domain: Mapped[str | None] = mapped_column(String(300))
    database: Mapped[str | None] = mapped_column(String(200))
    dependencies: Mapped[str | None] = mapped_column(Text)
    scheduler: Mapped[str | None] = mapped_column(Text)
    external_services: Mapped[str | None] = mapped_column(Text)
    health_url: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[enums.ServiceStatus] = mapped_column(
        Enum(enums.ServiceStatus, name="service_status"), default=enums.ServiceStatus.UNKNOWN
    )
    risk: Mapped[enums.RiskLevel] = mapped_column(
        RiskLevelType, default=enums.RiskLevel.LOW
    )
    backup: Mapped[str | None] = mapped_column(String(200))
    owner_agent: Mapped[str | None] = mapped_column(String(100))
    last_verified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class Agent(Base, TimestampMixin):  # §36
    __tablename__ = "agents"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    capabilities: Mapped[dict | None] = mapped_column(JSON)
    tools: Mapped[dict | None] = mapped_column(JSON)
    limits: Mapped[dict | None] = mapped_column(JSON)
    prompt_version: Mapped[str | None] = mapped_column(String(40))  # §120
    autonomy_level: Mapped[int] = mapped_column(Integer, default=2)  # §113
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # §69 heartbeat
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Task(Base, TimestampMixin):  # §31
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # TASK-0001
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    business_unit_id: Mapped[int | None] = mapped_column(ForeignKey("business_units.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id"))
    objective: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(100), default="founder")
    assigned_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"))
    priority: Mapped[enums.Priority] = mapped_column(
        PriorityType, default=enums.Priority.P2
    )
    status: Mapped[enums.TaskStatus] = mapped_column(
        Enum(enums.TaskStatus, name="task_status"), default=enums.TaskStatus.BACKLOG, index=True
    )
    context: Mapped[dict | None] = mapped_column(JSON)
    # §35 — a task without success criteria cannot be verified, so this is not nullable.
    success_criteria: Mapped[str] = mapped_column(Text)
    approval_required: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_level: Mapped[enums.RiskLevel] = mapped_column(
        RiskLevelType, default=enums.RiskLevel.LOW
    )
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)  # §70
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(Text)
    artifacts: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    # §150 — duplicate protection. Two open tasks must not share a dedupe key.
    dedupe_key: Mapped[str | None] = mapped_column(String(200), index=True)


class TaskDependency(Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (UniqueConstraint("task_id", "depends_on_id", name="uq_task_dep"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    depends_on_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))


class AgentRun(Base):  # §81, §83
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    status: Mapped[enums.AgentRunStatus] = mapped_column(
        Enum(enums.AgentRunStatus, name="agent_run_status"), default=enums.AgentRunStatus.STARTED
    )
    provider: Mapped[str | None] = mapped_column(String(60))
    model: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime] = _now()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 4))
    tool_calls: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    supervisor_verdict: Mapped[enums.SupervisorVerdict | None] = mapped_column(
        Enum(enums.SupervisorVerdict, name="supervisor_verdict")
    )
    supervisor_notes: Mapped[str | None] = mapped_column(Text)


class Decision(Base, TimestampMixin):  # §50
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # DEC-0001
    title: Mapped[str] = mapped_column(String(300))
    decision: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    business_unit_id: Mapped[int | None] = mapped_column(ForeignKey("business_units.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    approved_by: Mapped[str] = mapped_column(String(100), default="founder")
    status: Mapped[enums.DecisionStatus] = mapped_column(
        Enum(enums.DecisionStatus, name="decision_status"), default=enums.DecisionStatus.ACTIVE
    )
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"))


class Learning(Base, TimestampMixin):  # §52
    __tablename__ = "learnings"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    lesson: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)
    business_unit_id: Mapped[int | None] = mapped_column(ForeignKey("business_units.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id"))
    tags: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[str] = mapped_column(String(100), default="operator")
    # §53A: unreviewed memory must never become trusted by an omitted status argument.
    status: Mapped[enums.LearningStatus] = mapped_column(
        Enum(enums.LearningStatus, name="learning_status"), default=enums.LearningStatus.CANDIDATE,
        server_default=enums.LearningStatus.CANDIDATE.value)


class AgentNote(Base):  # §53A — notebook / episodic memory
    """Short notes an agent writes after a run: the "defter".

    Deliberately separate from `learnings`. A note is cheap and may be wrong; a learning
    has survived review. Promotion is the reviewed path in §53A, never automatic, and an
    agent may never promote its own note into the constitution.
    """
    __tablename__ = "agent_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
    note: Mapped[str] = mapped_column(Text)
    # Normalised form used to detect that two runs learned the same thing.
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    tags: Mapped[str | None] = mapped_column(String(300))
    # How many distinct runs produced an equivalent note. §53A promotes at 2.
    reference_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime] = _now()
    promoted_learning_id: Mapped[int | None] = mapped_column(ForeignKey("learnings.id"))
    # Archived, never deleted: §155 prefers append-only history.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_reason: Mapped[str | None] = mapped_column(String(200))


class Incident(Base, TimestampMixin):  # §93
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # INC-0001
    severity: Mapped[enums.IncidentSeverity] = mapped_column(
        Enum(enums.IncidentSeverity, name="incident_severity")
    )
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detected_by: Mapped[str] = mapped_column(String(120))
    founder_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[enums.IncidentStatus] = mapped_column(
        Enum(enums.IncidentStatus, name="incident_status"), default=enums.IncidentStatus.OPEN, index=True
    )
    root_cause: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    learning: Mapped[str | None] = mapped_column(Text)
    # Dedupe scope for automatically opened incidents ("service:7", "title:..."); the partial
    # unique index below allows one unresolved incident per scope. NULL for manual incidents.
    open_scope: Mapped[str | None] = mapped_column(String(300))


class Approval(Base, TimestampMixin):  # §112
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # APR-0001
    action: Mapped[str] = mapped_column(String(300))
    reason: Mapped[str] = mapped_column(Text)
    expected_effect: Mapped[str] = mapped_column(Text)
    risk_level: Mapped[enums.RiskLevel] = mapped_column(RiskLevelType)
    rollback: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(100))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    status: Mapped[enums.ApprovalStatus] = mapped_column(
        Enum(enums.ApprovalStatus, name="approval_status"),
        default=enums.ApprovalStatus.REQUESTED,
        index=True,
    )
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_notes: Mapped[str | None] = mapped_column(Text)


class Event(Base):  # §59 — append-only (§155). No update path is exposed by the API.
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    source: Mapped[str] = mapped_column(String(120))
    severity: Mapped[enums.Severity] = mapped_column(
        SeverityType, default=enums.Severity.INFO
    )
    business_unit_id: Mapped[int | None] = mapped_column(ForeignKey("business_units.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id"))
    payload: Mapped[dict | None] = mapped_column(JSON)
    occurred_at: Mapped[datetime] = _now()
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_error: Mapped[str | None] = mapped_column(Text)
    notify_reason: Mapped[str | None] = mapped_column(String(200))
    # Alert fingerprint for §111 spam control; NULL for non-alertable events.
    dedupe_key: Mapped[str | None] = mapped_column(String(200), index=True)


class NotificationState(Base):
    """One durable notification decision per active condition scope."""
    __tablename__ = "notification_states"
    scope_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    episode: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    severity: Mapped[str | None] = mapped_column(String(8))
    delivery_status: Mapped[str] = mapped_column(String(20), default="idle", nullable=False)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"))
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):  # §86, §115 — who, what, why, which task, when, previous state
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(120), index=True)
    action: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(String(80), index=True)
    entity_key: Mapped[str | None] = mapped_column(String(120), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    occurred_at: Mapped[datetime] = _now()


class ControlState(Base):  # §114 — agent execution control, never production services
    __tablename__ = "control_state"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(120), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Phase(Base, TimestampMixin):  # §138 — build phase tracking, out of CLAUDE.md
    __tablename__ = "phases"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[enums.PhaseStatus] = mapped_column(
        Enum(enums.PhaseStatus, name="phase_status"), default=enums.PhaseStatus.NOT_STARTED
    )
    deliverables: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class Checkpoint(Base):  # §58 — session checkpoint, so state survives a session restart
    __tablename__ = "checkpoints"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(120))
    summary: Mapped[str] = mapped_column(Text)
    completed: Mapped[dict | None] = mapped_column(JSON)
    unfinished: Mapped[dict | None] = mapped_column(JSON)
    next_action: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class BackupRun(Base):  # §76 — a backup is not done until integrity is verified
    __tablename__ = "backup_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    target: Mapped[str] = mapped_column(String(120), index=True)
    destination: Mapped[str | None] = mapped_column(String(300))
    started_at: Mapped[datetime] = _now()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum: Mapped[str | None] = mapped_column(String(80))
    uploaded: Mapped[bool] = mapped_column(Boolean, default=False)
    integrity_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    restore_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)



class MetricObservation(Base):  # §102 — a business number is only as good as its provenance
    """One measured value from a named source. Missing data is the absence of a row, never 0.
    Append-only history: a series (source, subject, metric, window) keeps every reading, and
    re-reporting the same reading (same observed_at) is idempotent."""
    __tablename__ = "metric_observations"
    __table_args__ = (UniqueConstraint("source", "subject", "metric", "window", "observed_at",
                                       name="uq_metric_observation_reading"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    business_unit_id: Mapped[int] = mapped_column(ForeignKey("business_units.id"), index=True)
    subject: Mapped[str] = mapped_column(String(200))   # "youtube:VIDEO_ID", "seerxo"
    metric: Mapped[str] = mapped_column(String(80))     # "views", "mrr"
    window: Mapped[str] = mapped_column(String(40), default="")  # "24h", "7d", "" = snapshot
    value: Mapped[float] = mapped_column(Numeric(18, 4))
    unit: Mapped[str | None] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(120))    # "youtube-data-api", "polar"
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = _now()
    evidence: Mapped[dict | None] = mapped_column(JSON)  # e.g. title, published_at

class Experiment(Base, TimestampMixin):
    """Did an improvement work? One record from hypothesis to decision; the numbers are existing
    metric_observations rows, the verdict on them is computed (app/experiments.py), never an opinion."""
    __tablename__ = "experiments"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # EXP-0001
    title: Mapped[str] = mapped_column(String(300))
    hypothesis: Mapped[str] = mapped_column(Text)
    status: Mapped[enums.ExperimentStatus] = mapped_column(
        Enum(enums.ExperimentStatus, name="experiment_status"), default=enums.ExperimentStatus.PROPOSED, index=True)
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id"), index=True)
    business_unit_id: Mapped[int] = mapped_column(ForeignKey("business_units.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    # {subject, metric, window, source, unit}: all required; the exact series this experiment is judged on.
    metric_selector: Mapped[dict] = mapped_column(JSON)
    expected_direction: Mapped[str] = mapped_column(String(4))  # UP | DOWN
    min_effect: Mapped[float] = mapped_column(Numeric(18, 4), default=0)  # smallest change that counts
    measurement_window_hours: Mapped[int] = mapped_column(Integer)
    intervention_ref: Mapped[str | None] = mapped_column(String(300))  # task key, PR URL, commit...
    baseline_observation_id: Mapped[int | None] = mapped_column(ForeignKey("metric_observations.id"))
    result_observation_id: Mapped[int | None] = mapped_column(ForeignKey("metric_observations.id"))
    outcome: Mapped[str | None] = mapped_column(String(30))  # IMPROVED | WORSENED | NO_MEANINGFUL_CHANGE
    evidence: Mapped[dict | None] = mapped_column(JSON)  # computed numbers; never model text
    created_by: Mapped[str] = mapped_column(String(100))
    approved_by: Mapped[str | None] = mapped_column(String(100))  # founder: READY
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    measure_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    learning_id: Mapped[int | None] = mapped_column(ForeignKey("learnings.id"))
    # The goal's metric selector as it was when the experiment was proposed: a later edit to the goal
    # is detected by comparison, never silently inherited (app/experiments.py).
    goal_selector_snapshot: Mapped[dict | None] = mapped_column(JSON)
    # sha256 of what makes a proposal "the same request"; one open experiment per fingerprint.
    fingerprint: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        CheckConstraint("expected_direction in ('UP', 'DOWN')", name="ck_experiments_direction"),
        CheckConstraint("min_effect >= 0", name="ck_experiments_min_effect"),
        CheckConstraint("measurement_window_hours > 0", name="ck_experiments_window_hours"),
    )


# §150 duplicate protection enforced by the database, so concurrent writers (watchdog,
# event router, worker, webhooks) cannot both commit the same open work. See app/keys.py.
_open_task = Task.dedupe_key.isnot(None) & Task.status.in_(enums.OPEN_TASK_STATES)
Index("uq_tasks_open_dedupe", Task.dedupe_key, unique=True,
      postgresql_where=_open_task, sqlite_where=_open_task)
_open_experiment = Experiment.status.in_([enums.ExperimentStatus.PROPOSED, enums.ExperimentStatus.READY,
                                           enums.ExperimentStatus.RUNNING, enums.ExperimentStatus.AWAITING_MEASUREMENT,
                                           enums.ExperimentStatus.EVALUATED])
Index("uq_experiments_open_fingerprint", Experiment.fingerprint, unique=True,
      postgresql_where=_open_experiment, sqlite_where=_open_experiment)
_unresolved = Incident.open_scope.isnot(None) & (Incident.status != enums.IncidentStatus.RESOLVED)
Index("uq_incidents_open_scope", Incident.open_scope, unique=True,
      postgresql_where=_unresolved, sqlite_where=_unresolved)
