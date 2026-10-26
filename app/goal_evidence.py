"""Read-only evidence for one goal's measurement: what supports its state, and why (§30, §102).

A thin typed view over goals.assess(), the single metric-selection algorithm, so this
contract and goals.progress() cannot disagree. It reads persisted facts only: no database
writes, no model call, nothing invented. A missing reading stays absent, never zero.
Documented in docs/GOAL_EVIDENCE_CONTRACT.md.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import goals

STATUSES = ("VERIFIED", "STALE", "UNAVAILABLE")
REASONS = ("MEASUREMENT_VERIFIED", "MEASUREMENT_MISSING", "MEASUREMENT_STALE", "SOURCE_AMBIGUOUS",
           "UNIT_CONFLICT", "SELECTOR_INVALID", "FUTURE_OBSERVATION", "INVALID_NUMERIC_EVIDENCE")
PROGRESS_REASONS = ("PROGRESS_COMPUTED", "BASELINE_INVALID", "TARGET_INVALID", "TARGET_EQUALS_BASELINE", None)
# Why a goal is not measurable, in words an operator can act on.
EXPLANATIONS = {
    "MEASUREMENT_VERIFIED": "a current observation from a single source and unit",
    "MEASUREMENT_MISSING": "no observation exists for the selector",
    "MEASUREMENT_STALE": "the newest observation is older than its source's freshness window",
    "SOURCE_AMBIGUOUS": "several sources report this series and the selector does not pin one",
    "UNIT_CONFLICT": "the series was reported in more than one unit",
    "SELECTOR_INVALID": "the goal's metric selector is malformed",
    "FUTURE_OBSERVATION": "every observation is dated in the future",
    "INVALID_NUMERIC_EVIDENCE": "the newest observation is not a finite number",
}


@dataclass(frozen=True)
class GoalEvidence:
    goal_id: int
    metric_selector: dict | None
    measurement_status: str          # VERIFIED | STALE | UNAVAILABLE
    reason_code: str                 # why the measurement is or is not usable
    source: str | None
    observation_id: int | None
    observed_at: str | None          # ISO-8601, UTC
    measurement_unit: str | None
    latest_value: float | None
    baseline: float | None
    target: float | None
    progress: float | None
    progress_reason_code: str | None  # why progress is or is not computable
    evidence_timestamp: str           # the evaluation time: same inputs and clock, same payload

    def __post_init__(self):
        if self.measurement_status not in STATUSES or self.reason_code not in REASONS \
                or self.progress_reason_code not in PROGRESS_REASONS:
            raise ValueError(f"unknown evidence code: {self.measurement_status}/{self.reason_code}/"
                             f"{self.progress_reason_code}")

    @property
    def explanation(self) -> str:
        return EXPLANATIONS[self.reason_code]

    def to_dict(self) -> dict:
        return asdict(self) | {"explanation": self.explanation}


def for_goal(db: Session, goal, now: datetime | None = None) -> GoalEvidence:
    now = now or datetime.now(timezone.utc)
    a = goals.assess(db, goal, now)
    latest = a["latest"] or {}
    return GoalEvidence(
        goal_id=goal.id, metric_selector=a["selector"], measurement_status=a["freshness"],
        reason_code=a["reason"], source=latest.get("source"), observation_id=a["observation_id"],
        observed_at=latest.get("observed_at"), measurement_unit=latest.get("unit"),
        latest_value=latest.get("value"), baseline=a["baseline_value"], target=a["target_value"],
        progress=a["progress"], progress_reason_code=a["progress_reason"],
        evidence_timestamp=(now if now.tzinfo else now.replace(tzinfo=timezone.utc)).isoformat())
