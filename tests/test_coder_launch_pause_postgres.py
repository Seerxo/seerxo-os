"""Atomic founder pause admission, using only the guarded disposable PostgreSQL fixture."""
import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import control, models
from tests.test_coder_launch import _load
from tests.test_coder_launch_pause import fence
from tests.test_stage_c_worker_postgres import db


def launcher(db, tmp_path, monkeypatch):
    mod = _load()
    path = fence(mod, tmp_path, monkeypatch)
    popen = subprocess.Popen
    mod.test_helpers = []
    url = db.get_bind().url.render_as_string(hide_password=False)

    def run_query(argv, **kwargs):
        assert argv[:6] == ["docker", "exec", "-i", "--user=10001:10001", "company-api", "python"]
        prelude = f"from app import control; control.CODER_PAUSE_FENCE = {str(path)!r}; control._FENCE_UID = {os.getuid()}\n"
        child = popen([sys.executable, "-u", "-c", prelude + mod.PAUSE_QUERY, *argv[-2:]],
                      env={**os.environ, "DATABASE_URL": url, "CODER_PAUSE_FENCE_ENABLED": "true"}, **kwargs)
        mod.test_helpers.append(child)
        return child

    monkeypatch.setattr(mod.subprocess, "Popen", run_query)
    return mod


@pytest.mark.parametrize("value", [None, {}, {"paused": True}, {"paused": 0}, {"paused": "false"}])
def test_unknown_or_paused_state_never_authorizes_a_coder(db, tmp_path, monkeypatch, value):
    if value is not None:
        db.add(models.ControlState(key="agents_paused", value=value, updated_by="founder"))
        db.commit()
    mod = launcher(db, tmp_path, monkeypatch)
    with pytest.raises(mod.Refused):
        with mod.pause_guard():
            pytest.fail("an unknown or paused state granted admission")


def test_pause_and_container_admission_are_serialized_until_the_start_permission(db, tmp_path, monkeypatch):
    control.set_agents_paused(db, False, actor="founder", reason="disposable test")
    mod = launcher(db, tmp_path, monkeypatch)
    with mod.pause_guard() as authorize:
        with Session(db.get_bind()) as founder:
            founder.execute(text("SET LOCAL lock_timeout = '150ms'"))
            with pytest.raises(OperationalError) as error:
                control.set_agents_paused(founder, True, actor="founder", reason="race probe")
            assert error.value.orig.sqlstate == "55P03"
        authorize()
    control.set_agents_paused(db, True, actor="founder", reason="pause after admitted start")
    with pytest.raises(mod.Refused):
        with mod.pause_guard():
            pytest.fail("a queued request bypassed the committed founder pause")


def test_helper_death_does_not_release_the_founder_commit_fence(db, tmp_path, monkeypatch):
    control.set_agents_paused(db, False, actor="founder", reason="test")
    mod = launcher(db, tmp_path, monkeypatch)
    monkeypatch.setenv("CODER_PAUSE_FENCE_ENABLED", "true")
    monkeypatch.setattr(control, "CODER_PAUSE_FENCE", str(mod.PAUSE_FENCE))
    monkeypatch.setattr(control, "_FENCE_UID", os.getuid())
    monkeypatch.setattr(control, "_FENCE_TIMEOUT", 0.05)
    with mod.pause_guard() as authorize:
        authorize()
        mod.test_helpers[0].kill()
        mod.test_helpers[0].wait(timeout=2)
        with Session(db.get_bind()) as founder:
            with pytest.raises(control.PauseUnavailable):
                control.set_agents_paused(founder, True, actor="founder", reason="after helper death")
        assert not control.agents_paused(db)
    control.set_agents_paused(db, True, actor="founder", reason="after admission")
    with pytest.raises(mod.Refused):
        with mod.pause_guard():
            pytest.fail("a queued request bypassed the committed founder pause")
