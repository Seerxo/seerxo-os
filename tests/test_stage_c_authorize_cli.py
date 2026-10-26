"""The real scripts/companyctl authorize/revoke/status path against a disposable database.

Only the two host boundaries are doubled: `docker` (runs the embedded python against a temp SQLite
database and logs every call) and `id` (stands in for root; the real non-root refusal is tested in
test_stage_c_deploy_interlock). Nothing here can reach production.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import control, enums, models

ROOT = Path(__file__).parents[1]
COMPANYCTL = ROOT / "scripts" / "companyctl"
SHA = "d" * 40

DOCKER = """#!/usr/bin/env bash
[ "$1" = inspect ] && { echo "inspect" >> "$FAKE_DOCKER_LOG"; echo; exit 0; }
[ "$1" = exec ] || { echo "unexpected docker call: $*" >&2; exit 9; }
shift
while [ "$1" = -e ] || [ "$1" = -i ]; do
  if [ "$1" = -e ]; then export "$2"; shift 2; else shift; fi
done
shift   # container name
echo "exec $1 $2" >> "$FAKE_DOCKER_LOG"
case "$1 $2" in
  "printenv COMPANY_OS_GIT_SHA") echo "$FAKE_WORKER_SHA" ;;
  "printenv ENABLE_AUTONOMOUS_CODE_WRITE") echo false ;;
  "python -c") exec "$FAKE_PYTHON" -c "$3" ;;
esac
"""


@pytest.fixture
def host(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(DOCKER)
    (bin_dir / "id").write_text("#!/bin/sh\necho 0\n")
    for tool in ("docker", "id"):
        (bin_dir / tool).chmod(0o755)
    url = f"sqlite:///{tmp_path / 'state.db'}"
    engine = create_engine(url)
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(models.Task(key="TASK-0045", title="pilot", success_criteria="s",
                           status=enums.TaskStatus.READY, priority=enums.Priority.P2,
                           risk_level=enums.RiskLevel.LOW))
        db.commit()
    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "DATABASE_URL": url, "PYTHONPATH": str(ROOT),
                        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"), "FAKE_PYTHON": sys.executable,
                        "FAKE_WORKER_SHA": SHA}

    def run(*args, **override):
        return subprocess.run(["bash", str(COMPANYCTL), "stage-c", *args], text=True, capture_output=True,
                              env=env | override)
    run.engine, run.log = engine, tmp_path / "docker.log"
    return run


def row(host):
    with Session(host.engine) as db:
        state = db.get(models.ControlState, "stage-c-pilot-authorization")
        return (dict(state.value), state.updated_by) if state else None


def audits(host):
    with Session(host.engine) as db:
        return [a.action for a in db.query(models.AuditLog).filter_by(entity_key="stage-c-pilot-authorization")]


FOUNDER = "founder:access:founder@example.invalid"


def issue(host, task="TASK-0045"):
    """Stands in for POST /stage-c/authorization: the only issuer is a verified founder principal."""
    with Session(host.engine) as db:
        return control.authorize_pilot(db, task, SHA, founder=FOUNDER)


def test_root_on_the_host_can_no_longer_issue_a_founder_authorization(host):
    for args in (("authorize", "TASK-0045", "20"), ("authorize", "TASK-0045"), ("authorize",)):
        result = host(*args)
        assert result.returncode == 1 and "verified founder identity" in result.stderr
        assert row(host) is None and audits(host) == []
    assert not host.log.exists()  # refused before any docker call: nothing touched the worker or the database


def test_issued_authorization_is_bound_audited_and_attributed_to_the_founder(host):
    value = issue(host)
    stored, writer = row(host)
    assert writer == FOUNDER and stored["issued_by"] == FOUNDER and stored["action"] == "stage_c.code_write"
    assert (stored["task_key"], stored["source_sha"]) == ("TASK-0045", SHA)
    assert stored["repository"] == "seerxo/os" and stored["consumed_at"] is None and value == stored
    assert audits(host) == ["stage-c.authorization.issue"]


def test_consumed_exactly_once_then_never_reusable_and_status_shows_it(host):
    issue(host)
    with Session(host.engine) as db:
        assert control.consume_pilot_authorization(db, "TASK-0045", SHA, 7) is None
        db.commit()
    with Session(host.engine) as db:
        assert control.consume_pilot_authorization(db, "TASK-0045", SHA, 8) == "stage_c_authorization_consumed"
        assert control.pilot_authorization_denial(db, "TASK-0045", SHA) == "stage_c_authorization_consumed"
    assert row(host)[0]["consumed_by_run_id"] == 7
    status = host("status")
    assert status.returncode == 0, status.stderr
    shown = json.loads(status.stdout)
    assert shown["authorization"] == {"consumed": True, "expires_at": row(host)[0]["expires_at"],
                                      "source_sha": SHA, "task_key": "TASK-0045"}
    assert shown["code_write_flag"] is False and "TOKEN" not in status.stdout


def test_revoke_removes_it_and_is_audited_and_idempotent(host):
    issue(host)
    first = host("revoke")
    assert first.returncode == 0 and json.loads(first.stdout) == {"revoked": True}
    assert row(host) is None
    assert json.loads(host("revoke").stdout) == {"revoked": False}
    assert audits(host) == ["stage-c.authorization.issue", "stage-c.authorization.revoke"]
    with Session(host.engine) as db:
        revoke = db.query(models.AuditLog).filter_by(action="stage-c.authorization.revoke").one()
        assert revoke.actor == "operator:host-root"  # an operator withdrew it; never recorded as the founder


def test_no_command_enables_writes_or_deploys_anything(host):
    issue(host)
    for args in (("status",), ("revoke",)):
        assert host(*args).returncode == 0
    assert host("authorize", "TASK-0045").returncode == 1
    calls = host.log.read_text().splitlines()
    # only read-the-environment and run-python-in-the-worker; never compose/up/restart/run/rm/stop
    assert set(calls) <= {"inspect", "exec printenv COMPANY_OS_GIT_SHA", "exec printenv ENABLE_AUTONOMOUS_CODE_WRITE", "exec python -c"}
    with Session(host.engine) as db:
        assert db.get(models.ControlState, "agents_paused") is None
        assert db.query(models.AgentRun).count() == 0
