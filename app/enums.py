"""Status vocabularies from the constitution. §32 forbids inventing ambiguous states,
so these enums are the only allowed values and live in one place."""
import enum


class BusinessUnitStatus(str, enum.Enum):  # §28
    ACTIVE = "ACTIVE"
    MAINTENANCE = "MAINTENANCE"
    EXPERIMENT = "EXPERIMENT"
    PAUSED = "PAUSED"
    BACKLOG = "BACKLOG"
    ARCHIVED = "ARCHIVED"


class ProjectStatus(str, enum.Enum):
    IDEA = "IDEA"
    EVALUATING = "EVALUATING"
    BACKLOG = "BACKLOG"
    NEXT = "NEXT"
    ACTIVE = "ACTIVE"
    MAINTENANCE = "MAINTENANCE"
    DEGRADED = "DEGRADED"
    PAUSED = "PAUSED"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


class TaskStatus(str, enum.Enum):  # §32 — exactly these twelve
    IDEA = "IDEA"
    BACKLOG = "BACKLOG"
    READY = "READY"
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    BLOCKED = "BLOCKED"
    REVIEW = "REVIEW"
    FAILED = "FAILED"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


OPEN_TASK_STATES = [
    TaskStatus.READY, TaskStatus.QUEUED, TaskStatus.IN_PROGRESS,
    TaskStatus.WAITING_EXTERNAL, TaskStatus.WAITING_APPROVAL,
    TaskStatus.BLOCKED, TaskStatus.REVIEW,
]


class Priority(str, enum.Enum):  # §33
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class RiskLevel(str, enum.Enum):  # §34
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class GoalStatus(str, enum.Enum):
    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    ACHIEVED = "ACHIEVED"
    MISSED = "MISSED"
    ABANDONED = "ABANDONED"


class LearningStatus(str, enum.Enum):  # §53A
    CANDIDATE = "CANDIDATE"  # promoted from the notebook, not yet reviewed: never given to agents
    APPROVED = "APPROVED"


class DecisionStatus(str, enum.Enum):  # §50
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REVERTED = "REVERTED"


class IncidentSeverity(str, enum.Enum):  # §93
    SEV0 = "SEV0"
    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"


class IncidentStatus(str, enum.Enum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    MITIGATED = "MITIGATED"
    RESOLVED = "RESOLVED"


class ApprovalStatus(str, enum.Enum):  # §72
    REQUESTED = "REQUESTED"
    GRANTED = "GRANTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class AgentRunStatus(str, enum.Enum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"


class SupervisorVerdict(str, enum.Enum):  # §45
    APPROVED = "APPROVED"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    FAILED = "FAILED"
    ESCALATE = "ESCALATE"


class ServiceStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    FAILING = "FAILING"
    STOPPED = "STOPPED"
    UNKNOWN = "UNKNOWN"


class Severity(str, enum.Enum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class PhaseStatus(str, enum.Enum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"


# Historical event vocabulary retained for webhook compatibility. Telegram delivery
# is decided by app.notify's category and durable scope state, not this set.
ALERTABLE_EVENTS = {
    "PRODUCTION_AUTOMATION_STOPPED",
    "YOUTUBE_PIPELINE_STOPPED",
    "SCHEDULED_PRODUCTION_MISSING",
    "WORKER_UNHEALTHY",
    "TASK_FAILED_REPEATEDLY",
    "BACKUP_FAILED",
    "RESTORE_VERIFICATION_FAILED",
    "DISK_CRITICAL",
    "SERVICE_DOWN",
    "APPROVAL_REQUIRED",
    "INCIDENT_CREATED",
    "METRIC_SOURCE_STALE",
}


class ExperimentStatus(str, enum.Enum):  # app/experiments.py owns the transitions
    PROPOSED = "PROPOSED"
    READY = "READY"
    RUNNING = "RUNNING"
    AWAITING_MEASUREMENT = "AWAITING_MEASUREMENT"
    EVALUATED = "EVALUATED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
