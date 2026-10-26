"""Audited Stage C cleanup: preview is read-only, apply is exact, atomic and idempotent (SQLite), plus
PostgreSQL row-lock behaviour (disposable CI database only)."""
import copy
import os
from datetime import datetime, timedelta, timezone
from threading import Thread
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from fastapi import HTTPException

from app import approval_identity as ai, audit, enums, models, stage_c, worker
from app import stage_c_cleanup as cc
from app.stage_c_worker import REPOSITORY

REQUIRED_CONTEXTS = ("ci/circleci: python-tests", "ci/circleci: frontend-tests")
GREEN_STATUSES = {context: "success" for context in REQUIRED_CONTEXTS}


def ingest_ci(*args, **kwargs):
    """ingest with the policy's required commit statuses (the production contract)."""
    return stage_c_ci.ingest(*args, required_statuses=REQUIRED_CONTEXTS, **kwargs)


NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
TTL = timedelta(minutes=30)
KEY = "TASK-0044"
FOUNDER = ai.founder("founder@example.com")


def _req(principal):
    return SimpleNamespace(state=SimpleNamespace(principal=principal))


def _seed(db, status=enums.TaskStatus.READY, stage="TESTING", lease="ours", runs=(enums.AgentRunStatus.FAILED,),
          ended=True):
    """lease: ours (this task's, expired at NOW) | ours_live | other (live at NOW) | malformed | none."""
    task = models.Task(key=KEY, title="legacy", success_criteria="s", status=status, retry_count=1,
                       artifacts={"pr": "x"}, context={"stage_c": {"paths": ["a"]}})
    db.add(task)
    db.flush()
    steps = ["DETECTED", "PLANNED", "IMPLEMENTING", "TESTING"]
    for s in steps[:steps.index(stage) + 1] if stage in steps else []:
        stage_c.advance(db, task, s, "company-worker", {"repository": REPOSITORY} if s == "DETECTED" else {})
    for st in runs:
        db.add(models.AgentRun(task_id=task.id, status=st, error="boom",
                               ended_at=NOW - TTL if ended and st != enums.AgentRunStatus.STARTED else None))
    if lease == "ours":
        stage_c.acquire(db, REPOSITORY, f"stage-c-runner:{KEY}", TTL, now=NOW - 2 * TTL)
    elif lease == "ours_live":
        stage_c.acquire(db, REPOSITORY, f"stage-c-runner:{KEY}", TTL, now=NOW)
    elif lease == "other":
        stage_c.acquire(db, REPOSITORY, "stage-c-runner:TASK-9", TTL, now=NOW)
    elif lease == "malformed":
        db.add(models.ControlState(key=stage_c.lease_key(REPOSITORY), updated_by="t",
                                   value={"holder": f"stage-c-runner:{KEY}", "token": "x"}))
    db.commit()
    return task


def _approve(db, plan, **kw):
    """A decision as POST /approvals/{key} records it: decided_by and the approval.decide audit actor are the
    verified principal id. `by`, `audit_actor` and `audit=False` forge the variants that must refuse."""
    task = db.scalar(select(models.Task).where(models.Task.key == KEY))
    status = kw.get("status", enums.ApprovalStatus.GRANTED)
    by = kw.get("by", FOUNDER.id)
    apr = models.Approval(key="APR-1", action=kw.get("action", cc.approval_action(KEY, plan.digest)), reason="r",
                          expected_effect="e", risk_level=enums.RiskLevel.LOW, rollback="none",
                          requested_by="x", task_id=task.id, status=status,
                          decided_by=by, decided_at=kw.get("at", NOW - timedelta(minutes=5)))
    db.add(apr)
    if kw.get("audit", True):
        audit.record(db, actor=kw.get("audit_actor", by), action="approval.decide", entity_type="approval",
                     entity_key="APR-1", after={"status": status.value})
    db.commit()
    return cc.TrustedBoundary.issue(_req(FOUNDER), "APR-1")


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _snap(db):
    t = db.scalar(select(models.Task))
    return (t.status, t.retry_count, copy.deepcopy(t.context), t.completed_at, t.artifacts,
            [(r.id, r.status, r.error) for r in db.scalars(select(models.AgentRun))],
            db.scalar(select(func.count()).select_from(models.AuditLog)),
            db.get(models.ControlState, stage_c.lease_key(REPOSITORY)).value)


def test_preview_is_read_only_and_eligible(db):
    _seed(db)
    before = _snap(db)
    plan = cc.preview_cleanup(db, KEY, now=NOW)
    assert plan.state == "eligible" and not plan.refusals and plan.expected.lease_state == "ours_expired"
    assert not db.new and not db.dirty and _snap(db) == before


def test_apply_cleans_once_and_preserves_evidence(db):
    _seed(db)
    plan = cc.preview_cleanup(db, KEY, now=NOW)
    auth = _approve(db, plan)
    before = _snap(db)
    assert cc.apply_cleanup(db, plan.expected, auth, now=NOW) == "cleaned"
    db.commit()
    t = db.scalar(select(models.Task))
    assert t.status == enums.TaskStatus.CANCELLED and t.completed_at.replace(tzinfo=timezone.utc) == NOW
    assert t.context["improvement"]["stage"] == "ABANDONED"
    assert t.context["improvement"]["history"][-1]["evidence"]["reason"] == cc.EVIDENCE_REASON
    assert (t.retry_count, t.artifacts, t.context["stage_c"]) == (1, {"pr": "x"}, {"paths": ["a"]})
    assert [(r.id, r.status, r.error) for r in db.scalars(select(models.AgentRun))] == before[5]
    lease = db.get(models.ControlState, stage_c.lease_key(REPOSITORY)).value
    assert "holder" not in lease and lease["token"] == before[7]["token"]
    entry = db.scalars(select(models.AuditLog).where(models.AuditLog.action == "stage-c.cleanup.apply")).one()
    assert entry.before["status"] == "READY" and entry.after["lease_released"] is True
    assert entry.actor == FOUNDER.id and entry.after["approval_decided_by"] == FOUNDER.id  # verified ids only
    assert t.context["improvement"]["history"][-1]["actor"] == FOUNDER.id
    # second application, same inputs: no-op, no new audit
    n = db.scalar(select(func.count()).select_from(models.AuditLog))
    assert cc.apply_cleanup(db, plan.expected, auth, now=NOW) == "noop"
    assert db.scalar(select(func.count()).select_from(models.AuditLog)) == n
    assert cc.preview_cleanup(db, KEY, now=NOW).state == "already_cleaned"


@pytest.mark.parametrize("kw,reason", [
    ({"status": enums.TaskStatus.IN_PROGRESS}, "task_status_not_ready"),
    ({"stage": "IMPLEMENTING"}, "stage_not_testing"),
    ({"runs": (enums.AgentRunStatus.FAILED, enums.AgentRunStatus.STARTED)}, "started_run_present"),
    ({"lease": "other"}, "lease_other_live"),
    ({"lease": "ours_live"}, "lease_ours_live"),  # never release an active lease because we target its owner
    ({"ended": False}, "own_lease_execution_not_verified_inactive"),  # expired, but no proof the run ended
    ({"runs": ()}, "own_lease_execution_not_verified_inactive"),  # expired, and no run recorded at all
    ({"lease": "malformed"}, "lease_malformed"),
])
def test_refusals_are_exact_and_nothing_changes(db, kw, reason):
    _seed(db, **kw)
    before = _snap(db) if kw.get("lease") != "none" else None
    plan = cc.preview_cleanup(db, KEY, now=NOW)
    assert plan.state == "refused" and any(r.startswith(reason) for r in plan.refusals)
    with pytest.raises(cc.CleanupRefused):
        cc.apply_cleanup(db, plan.expected, _approve(db, plan), now=NOW)
    db.rollback()
    assert _snap(db)[:5] == before[:5]


def test_wrong_repository_refuses(db):
    t = _seed(db)
    ctx = copy.deepcopy(t.context)
    ctx["improvement"]["history"][0]["evidence"]["repository"] = "someone/else"
    t.context = ctx
    db.commit()
    assert any(r.startswith("repository_mismatch") for r in cc.preview_cleanup(db, KEY, now=NOW).refusals)


def test_expired_foreign_lease_is_left_alone(db):
    _seed(db, lease="other")
    later = NOW + TTL + timedelta(seconds=1)
    plan = cc.preview_cleanup(db, KEY, now=later)
    assert plan.state == "eligible" and plan.expected.lease_state == "other_expired"
    cc.apply_cleanup(db, plan.expected, _approve(db, plan, at=later - timedelta(minutes=1)), now=later)
    assert db.get(models.ControlState, stage_c.lease_key(REPOSITORY)).value["holder"] == "stage-c-runner:TASK-9"


def test_newer_run_or_state_change_after_preview_refuses(db):
    t = _seed(db)
    plan = cc.preview_cleanup(db, KEY, now=NOW)
    auth = _approve(db, plan)
    db.add(models.AgentRun(task_id=t.id, status=enums.AgentRunStatus.FAILED))
    db.commit()
    with pytest.raises(cc.CleanupRefused, match="state_changed:run_count,max_run_id"):
        cc.apply_cleanup(db, plan.expected, auth, now=NOW)
    db.rollback()
    t.retry_count = 2
    db.commit()
    with pytest.raises(cc.CleanupRefused, match="retry_count"):
        cc.apply_cleanup(db, plan.expected, auth, now=NOW)


def test_stale_fencing_token_refuses(db):
    _seed(db)
    plan = cc.preview_cleanup(db, KEY, now=NOW)
    auth = _approve(db, plan)
    row = db.get(models.ControlState, stage_c.lease_key(REPOSITORY))
    row.value = {**row.value, "token": row.value["token"] + 1}
    db.commit()
    with pytest.raises(cc.CleanupRefused, match="lease_token"):
        cc.apply_cleanup(db, plan.expected, auth, now=NOW)


@pytest.mark.parametrize("make", [
    lambda plan: "founder",  # a plain string is not authority
    lambda plan: None,
])
def test_plain_actor_is_not_authority(db, make):
    _seed(db)
    plan = cc.preview_cleanup(db, KEY, now=NOW)
    with pytest.raises(cc.CleanupRefused, match="founder_authority_missing"):
        cc.apply_cleanup(db, plan.expected, make(plan), now=NOW)
    with pytest.raises(PermissionError):
        cc.FounderAuthority("APR-1", FOUNDER.id)


@pytest.mark.parametrize("kw", [
    {"status": enums.ApprovalStatus.REQUESTED}, {"by": "agent"}, {"action": "stage-c.cleanup TASK-0044 other"},
    {"at": NOW - timedelta(days=2)}, {"at": NOW + timedelta(minutes=1)},
    {"by": "founder"},  # legacy string: not founder authority
    {"by": "founder (APR-1)"},
    {"by": "operator:host"}, {"by": "operator:local"}, {"by": "service:desk"},  # operators decide as themselves
    {"audit": False},  # founder prefix but the endpoint never recorded the decision
    {"audit_actor": "operator:host"},  # the recorded decider is someone else
])
def test_approval_must_be_granted_bound_and_fresh(db, kw):
    _seed(db)
    plan = cc.preview_cleanup(db, KEY, now=NOW)
    auth = _approve(db, plan, **kw)
    before = _snap(db)
    with pytest.raises(cc.CleanupRefused):
        cc.apply_cleanup(db, plan.expected, auth, now=NOW)
    assert not db.new and not db.dirty  # fails closed before any write
    db.rollback()
    assert _snap(db) == before


def test_injected_failure_rolls_back_everything(db, monkeypatch):
    _seed(db)
    plan = cc.preview_cleanup(db, KEY, now=NOW)
    auth = _approve(db, plan)
    before = _snap(db)

    def boom(*a, **k):
        raise RuntimeError("injected")
    monkeypatch.setattr(audit, "record", boom)
    with pytest.raises(RuntimeError, match="injected"):
        cc.apply_cleanup(db, plan.expected, auth, now=NOW)
    db.rollback()
    assert _snap(db) == before


def test_missing_task(db):
    assert cc.preview_cleanup(db, KEY, now=NOW) is None


# ---- PostgreSQL: the task row lock serialises concurrent applies ----------------------------------------

@pytest.fixture
def pg():
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url)
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    yield engine
    models.Base.metadata.drop_all(engine)
    engine.dispose()


def test_postgres_concurrent_apply_one_wins_other_noops(pg):
    with Session(pg) as s:
        _seed(s)
        plan = cc.preview_cleanup(s, KEY, now=NOW)
        auth = _approve(s, plan)
    a, b = Session(pg), Session(pg)
    assert cc.apply_cleanup(a, plan.expected, auth, now=NOW) == "cleaned"  # A holds the locks, uncommitted
    out = []
    t = Thread(target=lambda: out.append(cc.apply_cleanup(b, plan.expected, auth, now=NOW)))
    t.start()
    t.join(0.5)
    assert t.is_alive()  # B is blocked on A's task row lock
    with Session(pg) as r:  # preview never blocks
        assert cc.preview_cleanup(r, KEY, now=NOW).state == "eligible"
    a.commit()
    t.join(5)
    b.commit()
    assert out == ["noop"]
    with Session(pg) as r:
        assert r.scalar(select(func.count()).select_from(models.AuditLog)
                        .where(models.AuditLog.action == "stage-c.cleanup.apply")) == 1
    a.close(); b.close()


def test_postgres_state_change_while_blocked_is_revalidated(pg):
    with Session(pg) as s:
        t = _seed(s)
        plan = cc.preview_cleanup(s, KEY, now=NOW)
        auth = _approve(s, plan)
    a, b = Session(pg), Session(pg)
    task = a.scalar(select(models.Task).with_for_update())
    out = []

    def run():
        try:
            out.append(cc.apply_cleanup(b, plan.expected, auth, now=NOW))
        except cc.CleanupRefused as e:
            out.append(e.reasons)
    th = Thread(target=run)
    th.start()
    th.join(0.5)
    assert th.is_alive()
    a.add(models.AgentRun(task_id=task.id, status=enums.AgentRunStatus.STARTED))
    a.commit()  # a worker starts a run while the cleanup waits on the lock
    th.join(5)
    b.rollback()
    assert out and out[0][0].startswith("state_changed")
    with Session(pg) as r:
        assert r.scalar(select(models.Task)).status == enums.TaskStatus.READY
    a.close(); b.close()


def test_cleanup_stays_unwired_until_a_founder_boundary_exists():
    """BLOCKED FOR PRODUCTION: nothing outside this module may reach preview/apply or mint authority
    until an authenticated founder boundary is integrated and independently verified."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    sources = [p for d in ("app", "scripts", "migrations") for p in (root / d).rglob("*")
               if p.is_file() and p.suffix in (".py", ".sh") and p.name != "stage_c_cleanup.py"]
    sources += [root / "Dockerfile", root / "docker-compose.yml", *(root / ".github").rglob("*.yml")]
    hits = [str(p.relative_to(root)) for p in sources
            if p.is_file() and ("stage_c_cleanup" in p.read_text(errors="ignore")
                                or "TrustedBoundary" in p.read_text(errors="ignore"))]
    assert sources and hits == []


def test_trusted_boundary_is_defined_but_never_invoked():
    """Defining the mint is fine; calling it is the founder boundary's job (Agent 5), and no caller exists."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    calls = []
    for path in [*(root / "app").rglob("*.py"), *(root / "scripts").rglob("*.py")]:
        tree = ast.parse(path.read_text())
        mint = [range(n.lineno, n.end_lineno + 1) for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "TrustedBoundary"]
        for node in ast.walk(tree):
            f = getattr(node, "func", None)
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if name in ("issue", "FounderAuthority", "apply_cleanup", "preview_cleanup") \
                    and not any(node.lineno in r for r in mint):  # the mint itself constructs one
                calls.append(f"{path.relative_to(root)}:{node.lineno}:{name}")
    assert calls == [], calls


def test_postgres_injected_failure_rolls_back_everything(pg, monkeypatch):
    with Session(pg) as s:
        _seed(s)
        plan = cc.preview_cleanup(s, KEY, now=NOW)
        auth = _approve(s, plan)
        before = _snap(s)

    def boom(*a, **k):
        raise RuntimeError("injected")
    monkeypatch.setattr(audit, "record", boom)
    with Session(pg) as a:
        with pytest.raises(RuntimeError, match="injected"):
            cc.apply_cleanup(a, plan.expected, auth, now=NOW)
        a.commit()  # even a caller that commits after the failure persists nothing from the cleanup
    with Session(pg) as r:
        assert _snap(r) == before


@pytest.mark.parametrize("principal, code", [
    (None, 401), (ai.ANONYMOUS, 401), (ai.from_caller("host"), 403), (ai.from_caller("local"), 403),
    (ai.from_caller("desk"), 403), (ai.Principal("operator", FOUNDER.id), 403),  # founder-looking id, wrong kind
    (ai.Principal("founder", "founder"), 403),  # legacy string as a founder-kind principal
])
def test_only_a_verified_founder_request_can_mint_authority(principal, code):
    with pytest.raises(HTTPException) as e:
        cc.TrustedBoundary.issue(_req(principal) if principal else None, "APR-1")
    assert e.value.status_code == code
    authority = cc.TrustedBoundary.issue(_req(FOUNDER), "APR-1")
    assert authority.principal_id == FOUNDER.id


def test_forged_authority_object_is_refused_with_nothing_changed(db):
    _seed(db)
    plan = cc.preview_cleanup(db, KEY, now=NOW)
    _approve(db, plan)
    forged = object.__new__(cc.FounderAuthority)  # bypasses __init__/__post_init__
    object.__setattr__(forged, "approval_key", "APR-1")
    object.__setattr__(forged, "principal_id", "operator:host")
    before = _snap(db)
    with pytest.raises(cc.CleanupRefused, match="founder_authority_missing"):
        cc.apply_cleanup(db, plan.expected, forged, now=NOW)
    db.rollback()
    assert _snap(db) == before


def test_lease_expiring_between_preview_and_apply_refuses():
    """A live lease that later expires is a different state: the founder must re-preview, never inherit."""
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed(db, lease="ours_live")
        plan = cc.preview_cleanup(db, KEY, now=NOW)
        assert plan.state == "refused" and "lease_ours_live" in plan.refusals
        later = NOW + TTL + timedelta(seconds=1)
        with pytest.raises(cc.CleanupRefused, match="state_changed:lease_state"):
            cc.apply_cleanup(db, plan.expected, _approve(db, plan, at=later - timedelta(minutes=1)), now=later)


def test_stale_runner_is_fenced_after_cleanup_releases_its_expired_lease(db):
    _seed(db)
    stale = db.get(models.ControlState, stage_c.lease_key(REPOSITORY)).value["token"]
    plan = cc.preview_cleanup(db, KEY, now=NOW)
    assert cc.apply_cleanup(db, plan.expected, _approve(db, plan), now=NOW) == "cleaned"
    db.commit()
    assert not stage_c.holds(db, REPOSITORY, stale, now=NOW)  # the old runner can no longer advance anything
    fresh = stage_c.acquire(db, REPOSITORY, "stage-c-runner:TASK-0045", TTL, now=NOW)
    assert fresh == stale + 1
    with pytest.raises(stage_c.LeaseLost):
        stage_c.release(db, REPOSITORY, stale)  # nor release the new holder's lease
    assert db.get(models.ControlState, stage_c.lease_key(REPOSITORY)).value["holder"] == "stage-c-runner:TASK-0045"


# ---- PostgreSQL: concurrent worker and cleanup -------------------------------------------------------

def test_postgres_worker_claim_after_preview_makes_apply_refuse(pg):
    with Session(pg) as s:
        _seed(s)
        plan = cc.preview_cleanup(s, KEY, now=NOW)
        auth = _approve(s, plan)
    with Session(pg) as w:
        assert worker.claim(w, w.scalar(select(models.Task))) is True  # READY -> IN_PROGRESS, committed
    with Session(pg) as c:
        before = _snap(c)
        with pytest.raises(cc.CleanupRefused, match="state_changed:status"):
            cc.apply_cleanup(c, plan.expected, auth, now=NOW)
        c.rollback()
        assert _snap(c) == before and c.scalar(select(models.Task)).status == enums.TaskStatus.IN_PROGRESS


def test_postgres_worker_claim_racing_an_uncommitted_cleanup_claims_nothing(pg):
    with Session(pg) as s:
        _seed(s)
        plan = cc.preview_cleanup(s, KEY, now=NOW)
        auth = _approve(s, plan)
    c, w = Session(pg), Session(pg)
    assert cc.apply_cleanup(c, plan.expected, auth, now=NOW) == "cleaned"  # holds task + lease locks
    task = w.scalar(select(models.Task))  # plain read: not blocked, still READY in w's snapshot
    out = []
    th = Thread(target=lambda: out.append(worker.claim(w, task)))
    th.start()
    th.join(0.5)
    assert th.is_alive()  # the claim's UPDATE waits on the cleanup's task row lock
    c.commit()
    th.join(5)
    assert out == [False]  # re-evaluated after commit: CANCELLED, not READY
    with Session(pg) as r:
        t = r.scalar(select(models.Task))
        assert t.status == enums.TaskStatus.CANCELLED and t.context["improvement"]["stage"] == "ABANDONED"
        assert r.scalar(select(func.count()).select_from(models.AuditLog)
                        .where(models.AuditLog.action == "task.claim")) == 0
    c.close(); w.close()


def test_postgres_runner_acquire_racing_cleanup_gets_a_fresh_token(pg):
    with Session(pg) as s:
        _seed(s)
        stale = s.get(models.ControlState, stage_c.lease_key(REPOSITORY)).value["token"]
        plan = cc.preview_cleanup(s, KEY, now=NOW)
        auth = _approve(s, plan)
    c, r = Session(pg), Session(pg)
    assert cc.apply_cleanup(c, plan.expected, auth, now=NOW) == "cleaned"
    out = []
    th = Thread(target=lambda: out.append(stage_c.acquire(r, REPOSITORY, "stage-c-runner:TASK-0045", TTL, now=NOW)))
    th.start()
    th.join(0.5)
    assert th.is_alive()  # the lease row is locked by the cleanup
    c.commit()
    th.join(5)
    r.commit()
    assert out == [stale + 1]
    with Session(pg) as q:
        assert not stage_c.holds(q, REPOSITORY, stale, now=NOW)
    c.close(); r.close()
