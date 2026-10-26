"""Deterministic watchdog (§68). No model calls, ever.

Everything here is a SQL query, an arithmetic comparison or a fact collected by
`scripts/watchdog.sh` on the host. §36A: if it can be decided by code, it must not cost
a model call — and a guard that needs an LLM to notice a stopped container is not a guard.

Split of responsibility, deliberately:

    scripts/watchdog.sh  (host)  collects docker/disk/memory facts -> JSON on stdin
    app.watchdog         (here)  evaluates facts + database state -> events -> alerts

The host collects because reading container state needs the Docker socket, and mounting
that socket into company-api would hand the API root-equivalent control of the host.
The API stays unprivileged; the shell script holds the privilege it already needs.

Usage:  scripts/watchdog.sh | docker exec -i company-api python -m app.watchdog
        docker exec -i company-api python -m app.watchdog --self-test
"""
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, and_, cast, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, aliased

from app import (business_reconciler, ci_usage, enums, event_router, experiment_scheduler, metric_sources, models,
                 reconciler_heartbeat, worker)
from app.db import SessionLocal, engine
from app.notify import RECOVERY_RETRY_STATE_PREFIX, fingerprint, notifier

log = logging.getLogger(__name__)

# --- thresholds ------------------------------------------------------------------
# §157 disk thresholds; the rest come from §67 reminder rules and the backup cadence.
DISK_HIGH_PCT = 85
DISK_CRITICAL_PCT = 95
MEM_HIGH_PCT = 92
# Daily backup at 03:30 UTC, so 36h means at least one run was missed entirely.
BACKUP_MAX_AGE_H = 36
# Weekly restore test, so 10 days means one was missed.
RESTORE_TEST_MAX_AGE_D = 10
TASK_READY_STALE_H = 48
TASK_BLOCKED_STALE_H = 72
APPROVAL_STALE_H = 48
# youtube-auto publishes daily; 48h is a missed day plus margin.
PRODUCTION_INACTIVE_H = 48
# One ControlState row holding the latest host reading, overwritten every run (no history).
HOST_METRICS_KEY = "host_metrics"
WATCHDOG_CONDITION_PREFIX = "watchdog-condition:"
RECOVERY_RETRY_INTERVAL = timedelta(minutes=5)
RECOVERY_MAX_ATTEMPTS = 3


@dataclass
class Finding:
    event_type: str
    severity: enums.Severity
    title: str
    detail: str
    service_key: str | None = None
    dedupe: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_h(ts: datetime | None) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (_now() - ts).total_seconds() / 3600


# --- checks driven by host facts --------------------------------------------------

def check_host(facts: dict) -> list[Finding]:
    out: list[Finding] = []

    disk = facts.get("disk_used_pct")
    if isinstance(disk, (int, float)):
        if disk >= DISK_CRITICAL_PCT:
            out.append(Finding("DISK_CRITICAL", enums.Severity.CRITICAL,
                               f"Disk at {disk}%",
                               f"Root filesystem {disk}% used (critical >= {DISK_CRITICAL_PCT}%)",
                               dedupe="disk-critical"))
        elif disk >= DISK_HIGH_PCT:
            out.append(Finding("DISK_CRITICAL", enums.Severity.HIGH,
                               f"Disk at {disk}%",
                               f"Root filesystem {disk}% used (high >= {DISK_HIGH_PCT}%)",
                               dedupe="disk-high"))

    mem = facts.get("mem_used_pct")
    if isinstance(mem, (int, float)) and mem >= MEM_HIGH_PCT:
        # The host has no swap, so a high-memory reading is an OOM risk, not just load.
        out.append(Finding("WORKER_UNHEALTHY", enums.Severity.HIGH,
                           f"Memory at {mem}%",
                           f"Host memory {mem}% used and the machine has no swap",
                           dedupe="mem-high"))
    return out


def _resolve_container(registered: str, states: dict[str, str]) -> str | None:
    """Find the live container for a registered name.

    Coolify appends a per-deployment id, so `wr2sdv20joia2t3r0f6j60ow-132720799925`
    becomes `...-023625132569` on the next deploy. Pinning the full name would make the
    watchdog cry wolf after every single deployment, so the registry stores the stable
    prefix and an exact match is tried first, prefix match second.
    """
    if registered in states:
        return registered
    matches = [name for name in states if name.startswith(registered + "-")]
    # Ambiguity means an old container was left behind; treat it as unresolved rather
    # than guessing which one is authoritative.
    return matches[0] if len(matches) == 1 else None


def check_containers(db: Session, facts: dict) -> list[Finding]:
    """A service registered as ACTIVE must have its container running.

    Services deliberately marked STOPPED (flow-browser, cdp-proxy) are skipped, so a
    planned shutdown does not page anyone — §111.
    """
    out: list[Finding] = []
    states = facts.get("containers")
    if not isinstance(states, dict) or facts.get("containers_complete", True) is not True:
        return []  # collection failure is UNKNOWN, not proof that every service is down
    health = facts.get("container_health", {})
    health_complete = (isinstance(health, dict)
                       and facts.get("container_health_complete", True) is True)
    if not isinstance(health, dict):
        health = {}

    services = db.execute(
        select(models.Service).where(
            models.Service.container.is_not(None),
            models.Service.status.notin_([enums.ServiceStatus.STOPPED]),
        )
    ).scalars().all()

    for svc in services:
        actual = _resolve_container(svc.container, states)
        state = states.get(actual) if actual else None
        if state is None:
            out.append(Finding("SERVICE_DOWN", enums.Severity.HIGH,
                               f"{svc.key}: container missing",
                               f"Registered container '{svc.container}' was not found on the host",
                               service_key=svc.key, dedupe=f"missing:{svc.key}"))
        elif state != "running":
            out.append(Finding("SERVICE_DOWN", enums.Severity.HIGH,
                               f"{svc.key}: container {state}",
                               f"Container '{actual}' is {state}, expected running",
                               service_key=svc.key, dedupe=f"down:{svc.key}"))
        elif health_complete and health.get(actual) == "unhealthy":
            out.append(Finding("WORKER_UNHEALTHY", enums.Severity.MEDIUM,
                               f"{svc.key}: unhealthy",
                               f"Container '{actual}' is running but its healthcheck fails",
                               service_key=svc.key, dedupe=f"unhealthy:{svc.key}"))
    return out


def check_http(facts: dict) -> list[Finding]:
    out: list[Finding] = []
    for key, result in (facts.get("http") or {}).items():
        code = result.get("code")
        if code != 200:
            out.append(Finding("SERVICE_DOWN", enums.Severity.HIGH,
                               f"{key}: HTTP {code}",
                               f"{result.get('url')} returned {code}, expected 200",
                               service_key=result.get("service"), dedupe=f"http:{key}"))
    return out


# --- checks driven purely by the database ------------------------------------------

def check_backups(db: Session) -> list[Finding]:
    out: list[Finding] = []

    last_ok = _last_verified_backup(db, restore=False)
    age = _age_h(last_ok.started_at) if last_ok else None
    if age is None:
        # An incomplete report is not proof. The notification policy pages once per
        # condition, so this remains actionable without a ten-minute Telegram loop.
        out.append(Finding("BACKUP_FAILED", enums.Severity.CRITICAL,
                           "No verified backup recorded",
                           "No BACKUP_VERIFIED event is linked to a successful run",
                           dedupe="backup-never"))
    elif age > BACKUP_MAX_AGE_H:
        out.append(Finding("BACKUP_FAILED", enums.Severity.CRITICAL,
                           f"Last successful backup {age:.0f}h ago",
                           f"Newest fully successful backup is older than {BACKUP_MAX_AGE_H}h",
                           dedupe="backup-stale"))

    last_restore = _last_verified_backup(db, restore=True)
    if last_restore is None:
        # Only an off-site (uploaded, BACKUP_VERIFIED) restore counts: a local-fallback PASS proves the file on
        # this host, not recovery from R2. One episode per condition, so the 10-minute tick does not re-page.
        out.append(Finding("RESTORE_VERIFICATION_FAILED", enums.Severity.HIGH,
                           "No verified off-site restore recorded",
                           "No restore test from an R2 copy has a BACKUP_VERIFIED event",
                           dedupe="restore-never"))
    else:
        days = (_age_h(last_restore.restore_tested_at) or 0) / 24
        if days > RESTORE_TEST_MAX_AGE_D:
            out.append(Finding("RESTORE_VERIFICATION_FAILED", enums.Severity.HIGH,
                               f"Restore not verified for {days:.0f} days",
                               f"Last restore test older than {RESTORE_TEST_MAX_AGE_D} days",
                               dedupe="restore-stale"))
    return out


def _last_verified_backup(db: Session, *, restore: bool,
                          target: str | None = None) -> models.BackupRun | None:
    """A run is proof only if report_backup committed its BACKUP_VERIFIED event."""
    stmt = (select(models.BackupRun)
            .join(models.Event, and_(models.Event.event_type == "BACKUP_VERIFIED",
                cast(models.Event.payload["backup_run_id"].as_string(), String)
                == cast(models.BackupRun.id, String),
                models.Event.payload["target"].as_string() == models.BackupRun.target))
            .where(models.BackupRun.ok.is_(True), models.BackupRun.uploaded.is_(True),
                   models.BackupRun.integrity_verified.is_(True),
                   models.BackupRun.error.is_(None)))
    if target is not None:
        stmt = stmt.where(models.BackupRun.target == target)
    if restore:
        stmt = stmt.where(models.BackupRun.restore_tested_at.is_not(None)).order_by(
            models.BackupRun.restore_tested_at.desc())
    else:
        stmt = stmt.where(models.BackupRun.restore_tested_at.is_(None)).order_by(
            models.BackupRun.started_at.desc())
    return db.scalar(stmt.limit(1))


def check_stale_work(db: Session) -> list[Finding]:
    """§67 reminder rules. Deliberately one aggregated finding per class, not per task,
    so a backlog of ten stale tasks is one message rather than ten."""
    out: list[Finding] = []
    now = _now()

    for status, hours, label in (
        (enums.TaskStatus.READY, TASK_READY_STALE_H, "READY"),
        (enums.TaskStatus.BLOCKED, TASK_BLOCKED_STALE_H, "BLOCKED"),
    ):
        cutoff = now - timedelta(hours=hours)
        rows = db.execute(
            select(models.Task).where(models.Task.status == status,
                                      models.Task.created_at < cutoff)
        ).scalars().all()
        if rows:
            keys = ", ".join(t.key for t in rows[:6])
            out.append(Finding("TASK_FAILED_REPEATEDLY", enums.Severity.MEDIUM,
                               f"{len(rows)} task(s) stale in {label} > {hours}h",
                               f"{keys}{' ...' if len(rows) > 6 else ''}",
                               dedupe=f"stale:{label}"))

    cutoff = now - timedelta(hours=APPROVAL_STALE_H)
    pending = db.execute(
        select(models.Approval).where(models.Approval.status == enums.ApprovalStatus.REQUESTED,
                                      models.Approval.created_at < cutoff)
    ).scalars().all()
    if pending:
        out.append(Finding("APPROVAL_REQUIRED", enums.Severity.HIGH,
                           f"{len(pending)} approval(s) waiting > {APPROVAL_STALE_H}h",
                           ", ".join(a.key for a in pending[:6]),
                           dedupe="approval-stale"))

    dead = db.execute(
        select(models.Task).where(models.Task.status == enums.TaskStatus.FAILED,
                                  models.Task.retry_count >= models.Task.max_attempts)
    ).scalars().all()
    if dead:
        out.append(Finding("TASK_FAILED_REPEATEDLY", enums.Severity.HIGH,
                           f"{len(dead)} task(s) exhausted their retries",
                           ", ".join(t.key for t in dead[:6]) + " — dead letter review (§149)",
                           dedupe="deadletter"))
    return out


def check_worker(db: Session) -> list[Finding]:
    """The unattended worker beats every tick. Silence after it has run means it died.
    A worker that has never beaten was never deployed, which is not a fault."""
    st = worker.worker_state(db)
    if st["status"] != "STALE":
        return []
    minutes = st["heartbeat_age_s"] // 60
    return [Finding("WORKER_UNHEALTHY", enums.Severity.HIGH,
                    f"company-worker: heartbeat {minutes}m old",
                    f"Last beat {st['heartbeat_at']}; autonomous tasks are not being worked",
                    service_key=None, dedupe="worker-heartbeat")]


def check_stage_c(facts: dict) -> list[Finding]:
    """Code writes stay suspended when the existing preflight loses any proof.

    The host runs the same zero-model preflight used before enablement. This does not flip
    the founder's flag; stage_c_worker.ready() refuses a non-ready or stale report (stored by
    record_stage_c_readiness on every tick). One deduped
    alert makes that safety suspension visible without paging every watchdog tick.
    """
    stage = facts.get("stage_c")
    if not isinstance(stage, dict) or stage.get("enabled") is not True:
        return []
    report = stage.get("report")
    if isinstance(report, dict) and report.get("ready") is True:
        return []
    failed = []
    if isinstance(report, dict):
        failed = [str(c.get("name")) for c in report.get("checks", [])
                  if isinstance(c, dict) and c.get("status") != "PASS"]
    detail = ("Stage C preflight unavailable" if not failed else
              "Stage C preflight no longer passes: " + ", ".join(failed[:6]))
    return [Finding("WORKER_UNHEALTHY", enums.Severity.HIGH,
                    "Stage C code writes suspended", detail, dedupe="stage-c-readiness")]


def check_production_activity(db: Session, facts: dict) -> list[Finding]:
    hours = facts.get("youtube_last_publish_age_h")
    if isinstance(hours, (int, float)) and hours > PRODUCTION_INACTIVE_H:
        return [Finding("SCHEDULED_PRODUCTION_MISSING", enums.Severity.HIGH,
                        f"@AxiomTalesHQ: no publish for {hours:.0f}h",
                        f"Expected roughly daily; threshold {PRODUCTION_INACTIVE_H}h",
                        service_key="youtube-auto", dedupe="youtube-inactive")]
    return []


def check_metric_sources(newest: dict[str, datetime]) -> list[Finding]:
    """A registered source whose newest reading is older than its freshness window has
    stopped reporting (collector, token or upstream broke). A source that never reported
    is not configured yet, which is not an alert (§111)."""
    out: list[Finding] = []
    for source, observed_at in sorted(newest.items()):
        if source not in metric_sources.SOURCES:
            continue
        age, limit = _age_h(observed_at), metric_sources.freshness(source).total_seconds() / 3600
        if age is not None and age > limit:
            out.append(Finding("METRIC_SOURCE_STALE", enums.Severity.MEDIUM,
                               f"{source}: no new metrics for {age:.0f}h",
                               f"Newest reading is older than its {limit:.0f}h window; "
                               f"check that source's collector log and token",
                               dedupe=f"metric-stale:{source}"))
    return out


def _newest_metric_by_source(db: Session) -> dict[str, datetime]:
    rows = db.execute(select(models.MetricObservation.source,
                             func.max(models.MetricObservation.observed_at))
                      .group_by(models.MetricObservation.source)).all()
    return {source: newest for source, newest in rows}


def record_host_metrics(db: Session, facts: dict) -> dict | None:
    """Persist the latest CPU/RAM/disk reading for the Desk. Returns what was stored."""
    def pct(key):
        v = facts.get(key)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    observed_at = _parse_observed_at(facts.get("collected_at")) or _now()
    value = {"cpu_percent": pct("cpu_used_pct"), "ram_percent": pct("mem_used_pct"),
             "disk_percent": pct("disk_used_pct"), "at": observed_at.isoformat(),
             "collection": {
                 "containers": "OK" if facts.get("containers_complete") is True else "UNKNOWN",
                 "container_health": "OK" if facts.get("container_health_complete") is True else "UNKNOWN",
             }}
    runtime = facts.get("runtime")
    def sha(key):
        v = runtime.get(key) if isinstance(runtime, dict) else None
        return v if isinstance(v, str) and re.fullmatch(r"[a-f0-9]{40}", v) else None
    value["runtime"] = {k: sha(k) for k in ("api_sha", "worker_sha")}
    if value["cpu_percent"] is None and value["ram_percent"] is None and value["disk_percent"] is None:
        return None  # collector failed: keep the last good reading, whose age shows it is old
    insert_fn = (pg_insert if db.get_bind().dialect.name == "postgresql" else
                 sqlite_insert if db.get_bind().dialect.name == "sqlite" else None)
    if insert_fn is not None:
        db.execute(insert_fn(models.ControlState).values(
            key=HOST_METRICS_KEY, value={}, updated_by="watchdog"
        ).on_conflict_do_nothing(index_elements=[models.ControlState.key]))
    row = db.scalar(select(models.ControlState).where(
        models.ControlState.key == HOST_METRICS_KEY).with_for_update())
    previous_at = _parse_observed_at((row.value or {}).get("at")) if row else None
    if previous_at is not None and observed_at <= previous_at:
        db.commit()
        return None  # a delayed collector must not replace a newer source snapshot
    if row is None:
        row = models.ControlState(key=HOST_METRICS_KEY, value=value, updated_by="watchdog")
        db.add(row)
    else:
        row.value = value
        row.updated_by = "watchdog"
    db.commit()
    return value


# --- emit ---------------------------------------------------------------------------

def _watchdog_condition_scope(f: Finding) -> str | None:
    if not f.dedupe:
        return None
    return WATCHDOG_CONDITION_PREFIX + fingerprint(f.event_type, "watchdog", f.dedupe)


def _observed_at(facts: dict) -> datetime:
    value = facts.get("collected_at")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        except ValueError:
            pass
    return _now()


def _parse_observed_at(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _condition_checkpoint_key(scope: str) -> str:
    return "watchdog-seen:" + fingerprint("condition-seen", "watchdog", scope)


def _condition_seen_at(db: Session, event: models.Event, scope: str) -> datetime | None:
    checkpoint = db.get(models.ControlState, _condition_checkpoint_key(scope))
    if checkpoint is not None:
        return _parse_observed_at((checkpoint.value or {}).get("last_seen_at"))
    # Compatibility with a condition event created before observation checkpoints existed.
    return (_parse_observed_at((event.payload or {}).get("last_seen_at"))
            or _parse_observed_at(event.occurred_at))


def _record_condition_observation(db: Session, scope: str, observed_at: datetime,
                                 finding: Finding | None = None, event_id: int | None = None,
                                 episode: int | None = None) -> None:
    key = _condition_checkpoint_key(scope)
    row = db.get(models.ControlState, key)
    previous = _parse_observed_at((row.value or {}).get("last_seen_at")) if row else None
    if previous is not None and observed_at <= previous:
        return
    value = {"condition_scope": scope, "last_seen_at": observed_at.isoformat()}
    if finding is not None:
        value["latest_observation"] = {
            "title": finding.title, "detail": finding.detail,
            "severity": finding.severity.value, "observed_at": observed_at.isoformat(),
        }
    if event_id is not None:
        value["event_id"] = event_id
    if episode is not None:
        value["episode"] = episode
    if row is None:
        db.add(models.ControlState(key=key, value=value, updated_by="watchdog"))
    else:
        row.value, row.updated_by = value, "watchdog"


def _condition_is_recovered(db: Session, event: models.Event, facts: dict, scope: str) -> bool:
    """Return true only when the source currently proves this condition ended."""
    condition = (event.payload or {}).get("condition_key")
    previous_observation = _condition_seen_at(db, event, scope)
    observed_at = _parse_observed_at(facts.get("collected_at"))
    if observed_at is None:
        return False
    if previous_observation is not None and observed_at <= previous_observation:
        return False

    if event.event_type == "DISK_CRITICAL":
        used = facts.get("disk_used_pct")
        limit = DISK_CRITICAL_PCT if condition == "disk-critical" else DISK_HIGH_PCT
        return isinstance(used, (int, float)) and not isinstance(used, bool) and used < limit

    if event.event_type == "WORKER_UNHEALTHY":
        if condition == "mem-high":
            used = facts.get("mem_used_pct")
            return isinstance(used, (int, float)) and not isinstance(used, bool) and used < MEM_HIGH_PCT
        if condition == "worker-heartbeat":
            return worker.worker_state(db)["status"] == "HEALTHY"
        if condition == "stage-c-readiness":
            stage = facts.get("stage_c")
            report = stage.get("report") if isinstance(stage, dict) else None
            return isinstance(report, dict) and report.get("ready") is True
        if isinstance(condition, str) and condition.startswith("unhealthy:"):
            states, health = facts.get("containers"), facts.get("container_health")
            if (not isinstance(states, dict) or facts.get("containers_complete", True) is not True
                    or not isinstance(health, dict)
                    or facts.get("container_health_complete", True) is not True):
                return False
            service_key = condition.removeprefix("unhealthy:")
            service = db.scalar(select(models.Service).where(models.Service.key == service_key))
            actual = _resolve_container(service.container, states) if service and service.container else None
            return bool(actual and states.get(actual) == "running" and health.get(actual) == "healthy")
        return False

    if event.event_type == "SERVICE_DOWN":
        if isinstance(condition, str) and condition.startswith("http:"):
            result = (facts.get("http") or {}).get(condition.removeprefix("http:"))
            return isinstance(result, dict) and result.get("code") == 200
        if isinstance(condition, str) and condition.startswith(("missing:", "down:")):
            states = facts.get("containers")
            if not isinstance(states, dict) or facts.get("containers_complete", True) is not True:
                return False
            health = facts.get("container_health")
            if health is not None and not isinstance(health, dict):
                return False
            service_key = condition.split(":", 1)[1]
            service = db.scalar(select(models.Service).where(models.Service.key == service_key))
            actual = _resolve_container(service.container, states) if service and service.container else None
            health = facts.get("container_health")
            return bool(actual and states.get(actual) == "running" and
                        (health is None or health.get(actual) != "unhealthy"))
        return False

    if event.event_type == "SCHEDULED_PRODUCTION_MISSING":
        age = facts.get("youtube_last_publish_age_h")
        return isinstance(age, (int, float)) and not isinstance(age, bool) and age <= PRODUCTION_INACTIVE_H

    if event.event_type in ("TASK_FAILED_REPEATEDLY", "APPROVAL_REQUIRED"):
        return condition not in {finding.dedupe for finding in check_stale_work(db)}

    if event.event_type == "BACKUP_FAILED":
        backup = _last_verified_backup(db, restore=False)
        age = _age_h(backup.started_at) if backup else None
        return age is not None and age <= BACKUP_MAX_AGE_H

    if event.event_type == "RESTORE_VERIFICATION_FAILED":
        restore = _last_verified_backup(db, restore=True)
        age = _age_h(restore.restore_tested_at) if restore else None
        return age is not None and age <= RESTORE_TEST_MAX_AGE_D * 24

    if event.event_type == "METRIC_SOURCE_STALE" and isinstance(condition, str):
        source = condition.removeprefix("metric-stale:")
        observed_at = _newest_metric_by_source(db).get(source)
        return (observed_at is not None and
                (_age_h(observed_at) or 0) <= metric_sources.freshness(source).total_seconds() / 3600)

    return False


def _record_finding_event(db: Session, event: models.Event, *, require_route: bool = False,
                          claim: tuple[str, int, int, datetime] | None = None) -> tuple[bool, bool]:
    """Finish routing and notification decisions for an already persisted event."""
    if event.incident_id is None and event.event_type in event_router.RULES:
        # The durable condition state retries transient route exceptions. safe_route()
        # intentionally hides them and is therefore unsuitable for this checkpoint.
        if event_router.route(db, event) is None:
            if require_route:
                raise LookupError("incident rule could not resolve its required service")
    if claim is not None and _claim_matches(db, *claim) is None:
        raise _StaleWatchdogClaim()
    delivered, _ = notifier.dispatch(db, event)
    return delivered, not delivered


class _StaleWatchdogClaim(Exception):
    """The condition recovered or advanced while an older route attempt was running."""


def _claim_matches(db: Session, scope: str, event_id: int, episode: int,
                   claim_at: datetime) -> models.NotificationState | None:
    state = db.scalar(select(models.NotificationState).where(
        models.NotificationState.scope_key == scope).with_for_update()
        .execution_options(populate_existing=True))
    attempted_at = state.attempted_at if state else None
    if attempted_at is not None and attempted_at.tzinfo is None:
        attempted_at = attempted_at.replace(tzinfo=timezone.utc)
    if (state is None or not state.active or state.event_id != event_id
            or state.episode != episode or state.delivery_status != "processing"
            or attempted_at != claim_at):
        return None
    return state


def _process_watchdog_condition(db: Session, scope: str) -> tuple[bool, bool]:
    """Claim and finish a first sighting once; stale claims are recoverable after 5m."""
    state = db.scalar(select(models.NotificationState).where(
        models.NotificationState.scope_key == scope,
        models.NotificationState.active.is_(True),
        or_(
            models.NotificationState.delivery_status == "observed",
            and_(models.NotificationState.delivery_status == "route_retry",
                 models.NotificationState.attempted_at < _now() - timedelta(minutes=5)),
            and_(models.NotificationState.delivery_status == "processing",
                 models.NotificationState.attempted_at < _now() - timedelta(minutes=5)),
        ),
    ).with_for_update().execution_options(populate_existing=True))
    if state is None or state.event_id is None:
        return False, False
    event_id, episode = state.event_id, state.episode
    claim_at = _now()
    prior_attempt = state.attempted_at
    if prior_attempt is not None:
        if prior_attempt.tzinfo is None:
            prior_attempt = prior_attempt.replace(tzinfo=timezone.utc)
        if claim_at <= prior_attempt:
            claim_at = prior_attempt + timedelta(microseconds=1)
    state.delivery_status = "processing"
    state.attempted_at = claim_at
    db.commit()

    event = db.get(models.Event, event_id)
    if event is None:
        return False, False
    try:
        delivered, suppressed = _record_finding_event(
            db, event, require_route=True, claim=(scope, event_id, episode, claim_at))
        if _claim_matches(db, scope, event_id, episode, claim_at) is None:
            raise _StaleWatchdogClaim()
    except _StaleWatchdogClaim:
        db.rollback()
        return False, False
    except Exception as exc:  # noqa: BLE001 - route failures are persisted for bounded retry
        db.rollback()
        if _claim_matches(db, scope, event_id, episode, claim_at) is None:
            db.rollback()
            return False, False
        event = db.get(models.Event, event_id)
        if event is not None:
            try:
                notifier.dispatch(db, event)
            except Exception:  # noqa: BLE001 - keep the condition checkpoint durable
                db.rollback()
        state = _claim_matches(db, scope, event_id, episode, claim_at)
        if state is None:
            db.rollback()
            return False, False
        route_key = "watchdog-route:" + str(event_id)
        checkpoint = db.get(models.ControlState, route_key)
        previous_attempts = (checkpoint.value or {}).get("attempts", 0) if checkpoint else 0
        attempts = previous_attempts + 1 if isinstance(previous_attempts, int) and previous_attempts >= 0 else 1
        value = {"event_id": event_id, "attempts": attempts,
                 "redrives": (checkpoint.value or {}).get("redrives", 0) if checkpoint else 0,
                 "last_error": type(exc).__name__}
        if checkpoint is None:
            db.add(models.ControlState(key=route_key, value=value, updated_by="watchdog"))
        else:
            checkpoint.value, checkpoint.updated_by = value, "watchdog"
        state.delivery_status = "route_failed" if attempts >= 3 else "route_retry"
        state.attempted_at = _now()
        db.commit()
        log.error("watchdog routing failed for event %s on attempt %s (%s)",
                  event_id, attempts, type(exc).__name__)
        return False, False
    state = _claim_matches(db, scope, event_id, episode, claim_at)
    if state is None:
        db.rollback()
        return delivered, suppressed
    state.delivery_status = "processed"
    state.attempted_at = None
    db.commit()
    return delivered, suppressed


def _route_failure_redrive_ready(db: Session, state: models.NotificationState) -> bool:
    """Allow one bounded redrive after service repair or a cooldown for transient errors."""
    if not state.event_id:
        return False
    checkpoint = db.get(models.ControlState, f"watchdog-route:{state.event_id}")
    value = (checkpoint.value or {}) if checkpoint else {}
    if value.get("redrives", 0) >= 1:
        return False
    if value.get("last_error") != "LookupError":
        attempted_at = state.attempted_at
        if attempted_at is None:
            return False
        if attempted_at.tzinfo is None:
            attempted_at = attempted_at.replace(tzinfo=timezone.utc)
        return attempted_at < _now() - timedelta(minutes=30)
    event = db.get(models.Event, state.event_id)
    payload = (event.payload or {}) if event else {}
    service_key = payload.get("service_key")
    return bool(isinstance(service_key, str) and db.scalar(
        select(models.Service.id).where(models.Service.key == service_key)))


def _finalize_condition_recovery(db: Session, recovery: models.Event) -> bool:
    """Close routing state only after recovery delivery is definitively resolved.

    Notification delivery has its own scope state. This separate watchdog claim must
    also close so a delayed incident route cannot create work after positive recovery.
    A delivery intent is not proof of delivery: keep ambiguous outcomes active and
    uncertain so a new episode cannot overtake them.
    """
    recovery = db.scalar(select(models.Event).where(
        models.Event.id == recovery.id).execution_options(populate_existing=True))
    if recovery is None:
        return False
    payload = recovery.payload or {}
    scope = payload.get("condition_scope")
    event_id = payload.get("resolved_event_id")
    episode = payload.get("episode")
    if (not isinstance(scope, str) or not scope.startswith(WATCHDOG_CONDITION_PREFIX)
            or not isinstance(event_id, int) or isinstance(event_id, bool)
            or not isinstance(episode, int) or isinstance(episode, bool)):
        return False
    state = db.scalar(select(models.NotificationState).where(
        models.NotificationState.scope_key == scope).with_for_update()
        .execution_options(populate_existing=True))
    if state is None or state.event_id != event_id or state.episode != episode:
        return False
    resolved_without_delivery = recovery.notify_reason in (
        "incident recovered without Telegram delivery",
        "incident was not actively notified",
        "incident recovery follows incident lifecycle",
    )
    retry_exhausted_before_send = (
        recovery.notify_reason == "recovery dispatch retries exhausted; founder review required")
    waiting_for_source_delivery = recovery.notify_reason == "recovery waiting for alert delivery"
    if not recovery.notified and not resolved_without_delivery:
        state.active = True
        if (recovery.notify_reason != "recovery dispatch pending"
                and not waiting_for_source_delivery
                and not retry_exhausted_before_send):
            state.delivery_status = "uncertain"
        db.commit()
        return False
    if state.active:
        state.active = False
        state.delivery_status = "recovered"
        db.commit()
    return True


def _start_recovery_retry(db: Session, event: models.Event) -> int | None:
    """Persist one bounded, cooldown-protected attempt before replaying a recovery."""
    event = db.scalar(select(models.Event).where(
        models.Event.id == event.id).with_for_update()
        .execution_options(populate_existing=True))
    if (event is None or event.notified or event.notify_reason not in
            (None, "recovery dispatch pending")):
        db.rollback()
        return None
    key = f"{RECOVERY_RETRY_STATE_PREFIX}{event.id}"
    checkpoint = db.scalar(select(models.ControlState).where(
        models.ControlState.key == key).with_for_update()
        .execution_options(populate_existing=True))
    value = checkpoint.value if checkpoint else {}
    if not isinstance(value, dict):
        event.notify_reason = "recovery dispatch retries exhausted; founder review required"
        event.notify_error = "Invalid recovery retry checkpoint; inspect before resuming."
        db.commit()
        return None
    attempts = value.get("attempts", 0)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        attempts = RECOVERY_MAX_ATTEMPTS
    now = _now()
    if checkpoint is not None:
        next_retry_at = _parse_observed_at(value.get("next_retry_at"))
        if next_retry_at is None:
            event.notify_reason = "recovery dispatch retries exhausted; founder review required"
            event.notify_error = "Invalid recovery retry checkpoint; inspect before resuming."
            db.commit()
            return None
        if now < next_retry_at:
            db.rollback()
            return None
    if attempts >= RECOVERY_MAX_ATTEMPTS:
        event.notify_reason = "recovery dispatch retries exhausted; founder review required"
        event.notify_error = value.get("last_error") or "Bounded recovery dispatch attempts exhausted."
        if checkpoint is not None:
            db.delete(checkpoint)
        db.commit()
        return None
    attempts += 1
    value = {"attempts": attempts,
             "last_attempt_at": now.isoformat(),
             "next_retry_at": (now + RECOVERY_RETRY_INTERVAL).isoformat()}
    if checkpoint is None:
        db.add(models.ControlState(key=key, value=value, updated_by="watchdog"))
    else:
        checkpoint.value, checkpoint.updated_by = value, "watchdog"
    db.commit()
    return attempts


def _finish_recovery_retry(db: Session, event: models.Event, attempts: int) -> None:
    """Forget resolved work; stop after three safe pre-send failures."""
    event = db.scalar(select(models.Event).where(
        models.Event.id == event.id).execution_options(populate_existing=True))
    if event is None:
        return
    key = f"{RECOVERY_RETRY_STATE_PREFIX}{event.id}"
    checkpoint = db.get(models.ControlState, key)
    if event.notify_reason is None and not event.notified:
        event.notify_reason = "recovery dispatch pending"
        event.notify_error = event.notify_error or "Notification returned without a durable decision."
    if event.notify_reason == "recovery dispatch pending" and attempts >= RECOVERY_MAX_ATTEMPTS:
        event.notify_reason = "recovery dispatch retries exhausted; founder review required"
        event.notify_error = (event.notify_error or
                              "Three safe pre-send attempts failed; inspect before retrying.")
        if checkpoint is not None:
            db.delete(checkpoint)
    elif event.notify_reason != "recovery dispatch pending" or event.notified:
        if checkpoint is not None:
            db.delete(checkpoint)
    elif checkpoint is not None:
        value = dict(checkpoint.value or {})
        value["last_error"] = event.notify_error
        checkpoint.value, checkpoint.updated_by = value, "watchdog"
    db.commit()


def emit(db: Session, findings: list[Finding], facts: dict | None = None) -> dict:
    """Persist one event per active episode; recover only from positive source evidence."""
    facts = facts or {}
    observed_at = _observed_at(facts)
    source_observed_at = _parse_observed_at(facts.get("collected_at"))
    delivered = suppressed = emitted = deduplicated = recovered = 0
    active_scopes = {scope for f in findings if (scope := _watchdog_condition_scope(f))}

    # A crash may occur after the recovery event commits but before its notification
    # decision is recorded. Dispatch only safe, never-attempted intents. Ambiguous
    # sends remain active and uncertain for founder review; terminal rows do not
    # consume the bounded replay window.
    retry_key = RECOVERY_RETRY_STATE_PREFIX + cast(models.Event.id, String)
    pending_recovery_filters = (
        models.Event.notified.is_(False),
        or_(
            and_(models.Event.event_type == "WATCHDOG_CONDITION_RECOVERED",
                 models.Event.source == "watchdog"),
            models.Event.event_type == "INCIDENT_RESOLVED",
        ),
        or_(models.Event.notify_reason.is_(None),
            models.Event.notify_reason == "recovery dispatch pending"),
    )
    # Parse retry timestamps in Python: lexical SQL comparison cannot distinguish an
    # impossible but ISO-shaped date (for example month 99) from a valid future time.
    # Page through all pending intents so a batch of valid cooldowns cannot starve a
    # later malformed checkpoint that needs founder review.
    last_recovery_id = 0
    retry_dispatches = 0
    while retry_dispatches < 50:
        retry_state = aliased(models.ControlState)
        pending_recoveries = db.execute(select(models.Event, retry_state).outerjoin(
            retry_state, retry_state.key == retry_key).where(
                *pending_recovery_filters,
                models.Event.id > last_recovery_id,
            ).order_by(models.Event.id).limit(100)).all()
        if not pending_recoveries:
            break
        for recovery, checkpoint in pending_recoveries:
            last_recovery_id = recovery.id
            if checkpoint is not None and isinstance(checkpoint.value, dict):
                retry_at = _parse_observed_at(checkpoint.value.get("next_retry_at"))
                if retry_at is not None and retry_at > _now():
                    continue
            attempts = _start_recovery_retry(db, recovery)
            if attempts is None:
                suppressed += 1
                continue
            retry_dispatches += 1
            was_delivered, _ = notifier.dispatch(db, recovery)
            delivered += int(was_delivered)
            suppressed += int(not was_delivered)
            _finish_recovery_retry(db, recovery, attempts)
            if recovery.event_type == "WATCHDOG_CONDITION_RECOVERED":
                _finalize_condition_recovery(db, recovery)
            if retry_dispatches >= 50:
                break
        if len(pending_recoveries) < 100:
            break

    # Missing a finding is not recovery: a collector may omit a whole source section.
    # Close an episode only when its original source now proves the condition ended.
    active_conditions = db.scalars(select(models.NotificationState).where(
        models.NotificationState.scope_key.startswith(WATCHDOG_CONDITION_PREFIX),
        models.NotificationState.active.is_(True),
    ).order_by(models.NotificationState.scope_key).with_for_update()).all()
    recoveries = []
    for state in active_conditions:
        if state.scope_key in active_scopes:
            continue
        previous = db.get(models.Event, state.event_id) if state.event_id else None
        if previous is None or not _condition_is_recovered(db, previous, facts, state.scope_key):
            continue
        previous_payload = previous.payload or {}
        recovery_key = fingerprint(
            "WATCHDOG_CONDITION_RECOVERED", "watchdog",
            f"{state.scope_key}|{state.episode}")
        existing_recovery = db.scalar(select(models.Event).where(
            models.Event.dedupe_key == recovery_key))
        if existing_recovery is not None:
            _finalize_condition_recovery(db, existing_recovery)
            continue
        recovery = models.Event(
            event_type="WATCHDOG_CONDITION_RECOVERED", source="watchdog",
            severity=enums.Severity.INFO,
            payload={"condition_key": previous_payload.get("condition_key"),
                     "condition_scope": state.scope_key,
                     "resolved_event_id": state.event_id,
                     "episode": state.episode,
                     "observed_at": observed_at.isoformat()},
            dedupe_key=recovery_key,
            notify_reason=("recovery dispatch pending" if previous.incident_id is None
                           else "incident recovery follows incident lifecycle"),
        )
        # An incident's notification follows the incident-resolution lifecycle. For
        # standalone conditions, leave the claim active until the recovery dispatcher
        # records a delivery decision; the durable marker prevents a recurrence from
        # overtaking that work after this transaction commits.
        if previous.incident_id is not None:
            state.active = False
            state.delivery_status = "recovered"
        db.add(recovery)
        recoveries.append((previous, recovery))

    if recoveries:
        db.commit()
    for previous, recovery in recoveries:
        # Incident notifications follow the explicit incident resolution lifecycle.
        # Standalone conditions use the matching notification recovery scope.
        if previous.incident_id is None:
            notifier.dispatch(db, recovery)
            _finalize_condition_recovery(db, recovery)
        recovered += 1

    # Recover before resuming pending work. A crash after persistence must not route an
    # already recovered condition into a fresh incident on the next watchdog tick.
    pending = db.scalars(select(models.NotificationState).where(
        models.NotificationState.scope_key.startswith(WATCHDOG_CONDITION_PREFIX),
        models.NotificationState.active.is_(True),
        or_(
            models.NotificationState.delivery_status == "observed",
            and_(models.NotificationState.delivery_status == "route_retry",
                 models.NotificationState.attempted_at < _now() - timedelta(minutes=5)),
            and_(models.NotificationState.delivery_status == "processing",
                 models.NotificationState.attempted_at < _now() - timedelta(minutes=5)),
        ),
    ).order_by(models.NotificationState.scope_key).with_for_update()).all()
    for state in pending:
        was_delivered, was_suppressed = _process_watchdog_condition(db, state.scope_key)
        delivered += int(was_delivered)
        suppressed += int(was_suppressed)

    for f in findings:
        svc = None
        if f.service_key:
            svc = db.execute(
                select(models.Service).where(models.Service.key == f.service_key)
            ).scalar_one_or_none()
        payload = {"title": f.title, "detail": f.detail,
                   "condition_key": f.dedupe or f.title,
                   "service_key": f.service_key,
                   "last_seen_at": observed_at.isoformat()}
        dedupe_key = fingerprint(f.event_type, "watchdog", f.dedupe or f.title)
        scope = _watchdog_condition_scope(f)
        event = None
        if scope is not None:
            # The unique scope row serializes concurrent first sightings on PostgreSQL.
            # SQLite uses the same upsert contract for deterministic offline checks.
            db.execute(text("INSERT INTO notification_states "
                            "(scope_key, active, episode, delivery_status) "
                            "VALUES (:scope, false, 0, 'recovered') "
                            "ON CONFLICT (scope_key) DO NOTHING"), {"scope": scope})
            state = db.scalar(select(models.NotificationState).where(
                models.NotificationState.scope_key == scope).with_for_update())
            if state.active and state.event_id:
                deduplicated += 1
                _record_condition_observation(db, scope, observed_at, finding=f,
                                              event_id=state.event_id, episode=state.episode)
                db.commit()
                if state.delivery_status in ("observed", "route_retry"):
                    was_delivered, was_suppressed = _process_watchdog_condition(db, scope)
                    delivered += int(was_delivered)
                    suppressed += int(was_suppressed)
                elif state.delivery_status == "route_failed" and _route_failure_redrive_ready(db, state):
                    checkpoint = db.get(models.ControlState, f"watchdog-route:{state.event_id}")
                    route_value = checkpoint.value or {}
                    checkpoint.value = {**route_value, "attempts": 0,
                                        "redrives": route_value.get("redrives", 0) + 1}
                    state.delivery_status = "route_retry"
                    state.attempted_at = _now() - timedelta(minutes=6)
                    db.commit()
                    was_delivered, was_suppressed = _process_watchdog_condition(db, scope)
                    delivered += int(was_delivered)
                    suppressed += int(was_suppressed)
            else:
                recovered_at = None
                if state.episode:
                    recovery_key = fingerprint(
                        "WATCHDOG_CONDITION_RECOVERED", "watchdog",
                        f"{scope}|{state.episode}")
                    recovery = db.scalar(select(models.Event).where(
                        models.Event.dedupe_key == recovery_key))
                    if recovery is not None:
                        _finalize_condition_recovery(db, recovery)
                        if state.active:
                            deduplicated += 1
                            continue
                        recovered_at = _parse_observed_at(
                            (recovery.payload or {}).get("observed_at"))
                    if recovered_at is not None and (
                            source_observed_at is None or source_observed_at <= recovered_at):
                        deduplicated += 1
                        continue
                state.active = True
                state.episode += 1
                state.severity = f.severity.value
                event = models.Event(
                    event_type=f.event_type, source="watchdog", severity=f.severity,
                    service_id=svc.id if svc else None,
                    payload={**payload, "episode": state.episode,
                             "condition_scope": scope,
                             "condition_episode": state.episode},
                    dedupe_key=dedupe_key,
                )
                db.add(event)
                db.flush()
                state.event_id = event.id
                state.delivery_status = "observed"
                _record_condition_observation(db, scope, observed_at, finding=f,
                                              event_id=event.id, episode=state.episode)
                emitted += 1
                db.commit()
                was_delivered, was_suppressed = _process_watchdog_condition(db, scope)
                delivered += int(was_delivered)
                suppressed += int(was_suppressed)
                continue
        else:
            event = models.Event(
                event_type=f.event_type, source="watchdog", severity=f.severity,
                service_id=svc.id if svc else None, payload=payload, dedupe_key=dedupe_key,
            )
            db.add(event)
            db.commit()
            db.refresh(event)
            emitted += 1

        if event is not None:
            was_delivered, was_suppressed = _record_finding_event(db, event)
            delivered += int(was_delivered)
            suppressed += int(was_suppressed)
    return {"findings": len(findings), "emitted": emitted,
            "deduplicated": deduplicated, "recovered": recovered,
            "delivered": delivered, "suppressed": suppressed}


STAGE_C_READINESS_KEY = "stage-c-runtime-readiness"


def record_stage_c_readiness(db: Session, facts: dict) -> dict | None:
    """Store the live Stage C preflight so worker admission can enforce it (fail closed), not just
    alert. Collected on every tick, flag on or off, so enabling the flag has an immediate answer.
    A missing or malformed report (preflight could not run) is stored as NOT ready, so the last
    READY never outlives the ability to collect fresh facts."""
    stage = facts.get("stage_c")
    report = stage.get("report") if isinstance(stage, dict) else None
    if not isinstance(report, dict) or not isinstance(report.get("ready"), bool):
        report = {"ready": False, "checks": [{"name": "preflight_unavailable", "status": "UNKNOWN"}]}
    sha = report.get("source_sha")
    value = {"ready": report["ready"], "checked_at": _now().isoformat(),
             "source_sha": sha if isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha) else None,
             "failed": [str(c.get("name")) for c in report.get("checks", [])
                        if isinstance(c, dict) and c.get("status") != "PASS"][:10]}
    row = db.get(models.ControlState, STAGE_C_READINESS_KEY)
    if row is None:
        db.add(models.ControlState(key=STAGE_C_READINESS_KEY, value=value, updated_by="watchdog"))
    else:
        row.value, row.updated_by = value, "watchdog"
    db.commit()
    return value


def run_experiment_scheduler() -> dict | None:
    """Advance experiments whose window is over (docs/EXPERIMENT_SCHEDULER.md). None unless BOTH founder switches
    are set; never a founder decision, never a model call. Its own session, because run_if_enabled commits per
    experiment; a failure is logged and must never break the watchdog tick or change its exit code."""
    db = SessionLocal()
    try:
        tick = experiment_scheduler.run_if_enabled(db)
    except Exception as exc:   # noqa: BLE001 - the host tick must survive any scheduler failure
        db.rollback()
        log.exception("experiment scheduler failed")
        return {"error": type(exc).__name__}
    finally:
        db.close()
    if tick is None:
        return None
    if tick.outcomes:
        log.info("experiments: %s", tick.counts)
    return {"counts": tick.counts, "truncated": tick.truncated}


def run(facts: dict) -> dict:
    db = SessionLocal()
    try:
        findings: list[Finding] = []
        findings += check_host(facts)
        findings += check_containers(db, facts)
        findings += check_http(facts)
        findings += check_backups(db)
        findings += check_stale_work(db)
        findings += check_worker(db)
        findings += check_stage_c(facts)
        findings += check_production_activity(db, facts)
        findings += check_metric_sources(_newest_metric_by_source(db))
        findings += ci_usage.findings(db)   # estimated CI runner usage vs the Blacksmith free allowance
        result = emit(db, findings, facts)
        # A fresh heartbeat is positive recovery evidence. A missing or paused
        # heartbeat is not; an absent finding alone must not reset the episode.
        if worker.worker_state(db)["status"] == "HEALTHY":
            notifier.recover_condition(db, source="watchdog", condition_key="worker-heartbeat")
        # Only a fresh successful run proves that the earlier backup warning ended.
        fresh_backup = _last_verified_backup(db, restore=False)
        if (fresh_backup is not None and _age_h(fresh_backup.started_at) is not None
                and _age_h(fresh_backup.started_at) <= BACKUP_MAX_AGE_H):
            for condition in ("backup-never", "backup-stale"):
                notifier.recover_condition(db, source="watchdog", condition_key=condition)
        fresh_restore = _last_verified_backup(db, restore=True)
        if (fresh_restore is not None and _age_h(fresh_restore.restore_tested_at) is not None
                and _age_h(fresh_restore.restore_tested_at) <= RESTORE_TEST_MAX_AGE_D * 24):
            for condition in ("restore-never", "restore-stale"):
                notifier.recover_condition(db, source="watchdog", condition_key=condition)
        record_stage_c_readiness(db, facts)
        record_host_metrics(db, facts)
        result["business_tasks"] = reconciler_heartbeat.run(db, business_reconciler.reconcile)
        result["experiments"] = run_experiment_scheduler()
        result["detail"] = [
            {"event_type": f.event_type, "severity": f.severity.value, "title": f.title}
            for f in findings
        ]
        return result
    finally:
        db.close()


def self_test() -> int:
    """Threshold logic, checked without a database. §35: a check nobody verifies is a guess."""
    assert check_host({"disk_used_pct": 29, "mem_used_pct": 45}) == []
    high = check_host({"disk_used_pct": 88})
    assert len(high) == 1 and high[0].severity is enums.Severity.HIGH, high
    crit = check_host({"disk_used_pct": 96})
    assert crit[0].severity is enums.Severity.CRITICAL, crit
    mem = check_host({"mem_used_pct": 95})
    assert mem and mem[0].event_type == "WORKER_UNHEALTHY", mem

    assert check_http({"http": {"seerxo": {"code": 200, "url": "x"}}}) == []
    bad = check_http({"http": {"seerxo": {"code": 502, "url": "x", "service": "seerxo-api"}}})
    assert len(bad) == 1 and bad[0].event_type == "SERVICE_DOWN", bad

    states = {"company-api": "running", "wr2sdv20joia2t3r0f6j60ow-023625132569": "running"}
    assert _resolve_container("company-api", states) == "company-api"
    assert _resolve_container("wr2sdv20joia2t3r0f6j60ow", states) == \
        "wr2sdv20joia2t3r0f6j60ow-023625132569", "Coolify suffix must prefix-match"
    assert _resolve_container("nope", states) is None
    # Two candidates is ambiguous, not a match: a stale container was left behind.
    assert _resolve_container("x", {"x-1": "running", "x-2": "exited"}) is None

    assert check_production_activity(None, {"youtube_last_publish_age_h": 10}) == []
    stale = check_production_activity(None, {"youtube_last_publish_age_h": 72})
    assert stale[0].event_type == "SCHEDULED_PRODUCTION_MISSING", stale

    print("watchdog self-test OK")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    raw = sys.stdin.read().strip()
    facts = json.loads(raw) if raw else {}
    result = run(facts)
    print(json.dumps(result, indent=1))
    # Exit 1 when something was found, so cron and humans can tell at a glance.
    return 1 if result["findings"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        engine.dispose()
