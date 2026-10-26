"""B5 interim detective control: main may only move through PRs merged by the founder; anything else alerts, and
an unrunnable check reports DEGRADED instead of passing silently."""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import main_guard, models, notify


def sha(n: int) -> str:
    return f"{n:040x}"


class GitHub:
    """Fake read-only GitHub: a first-parent main history plus PR records."""

    def __init__(self, chain, prs, fail=None):
        self.chain, self.prs, self.fail, self.calls = chain, prs, fail, []   # chain: newest first

    def __call__(self, path):
        self.calls.append(path)
        if self.fail and self.fail in path:
            raise RuntimeError("GitHub 502")
        if path.startswith(f"/repos/{main_guard.REPOSITORY}/commits?"):
            return [{"sha": s, "parents": [{"sha": p}] if p else []}
                    for s, p in zip(self.chain, self.chain[1:] + [None])]
        if path.endswith("/pulls") and "/commits/" in path:
            c = path.split("/commits/")[1].split("/")[0]
            return [{"number": n, "merge_commit_sha": c, "merged_at": "2026-10-19T00:00:00Z"}
                    for n, (m, _) in self.prs.items() if m == c]
        n = int(path.rsplit("/", 1)[1])
        return {"number": n, "merged_by": {"login": self.prs[n][1]} if self.prs[n][1] else None}


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    sent = []
    monkeypatch.setattr(notify.notifier, "dispatch", lambda db, ev: sent.append(ev.event_type) or (True, None))
    with Session(engine) as s:
        s.sent = sent
        yield s


def events(db):
    return [(e.event_type, e.payload["message"]) for e in db.scalars(select(models.Event).order_by(models.Event.id))]


def test_first_run_records_a_baseline_and_judges_nothing(db):
    out = main_guard.run(db, GitHub([sha(2), sha(1)], {}))
    assert out == {"status": "OK", "head": sha(2), "problems": []} and events(db) == []
    assert db.get(models.ControlState, main_guard.CHECKPOINT).value == {"head": sha(2)}


def test_founder_merged_prs_pass_and_advance_the_checkpoint(db):
    main_guard.run(db, GitHub([sha(1)], {}))
    gh = GitHub([sha(3), sha(2), sha(1)], {10: (sha(2), "semihbugrasezer"), 11: (sha(3), "semihbugrasezer")})
    out = main_guard.run(db, gh)
    assert out["status"] == "OK" and out["head"] == sha(3) and events(db) == [] and db.sent == []


@pytest.mark.parametrize("prs, why", [
    ({}, "direct push"),                                            # no PR at all
    ({10: (sha(2), "seerxo-agent")}, "merged by seerxo-agent"),     # the agent merged its own PR
    ({10: (sha(2), None)}, "merged by unknown"),                    # no merged_by recorded
])
def test_anything_but_a_founder_merge_alerts_critically(db, prs, why):
    main_guard.run(db, GitHub([sha(1)], {}))
    out = main_guard.run(db, GitHub([sha(2), sha(1)], prs))
    assert out["status"] == "ALERT"
    (kind, message), = events(db)
    assert kind == "MAIN_UNEXPECTED_CHANGE" and why in message and sha(2)[:12] in message
    assert db.sent == ["MAIN_UNEXPECTED_CHANGE"]
    assert notify.notifier.category(db.scalars(select(models.Event)).one()) == "critical"   # reaches Telegram


def test_one_bad_commit_among_good_ones_is_named(db):
    main_guard.run(db, GitHub([sha(1)], {}))
    gh = GitHub([sha(4), sha(3), sha(2), sha(1)], {10: (sha(2), "semihbugrasezer"), 12: (sha(4), "semihbugrasezer")})
    out = main_guard.run(db, gh)
    assert out["status"] == "ALERT" and len(out["problems"]) == 1 and out["problems"][0].startswith(sha(3)[:12])


@pytest.mark.parametrize("gh", [
    GitHub([sha(9), sha(8)], {}),                                   # previous head no longer on main (rewritten)
    GitHub([sha(2), sha(1)], {}, fail="/commits/"),                 # GitHub error while checking
    GitHub([], {}),                                                 # empty answer
])
def test_an_unrunnable_check_is_degraded_never_a_silent_pass(db, gh):
    main_guard.run(db, GitHub([sha(1)], {}))
    out = main_guard.run(db, gh)
    assert out["status"] == "DEGRADED"
    assert [k for k, _ in events(db)] == ["MAIN_GUARD_DEGRADED"]
    assert db.get(models.ControlState, main_guard.CHECKPOINT).value == {"head": sha(1)}    # not advanced
    assert notify.notifier.category(db.scalars(select(models.Event)).one()) == "action_required"


def test_it_only_reads_github():
    import inspect
    src = inspect.getsource(main_guard)
    assert ".post(" not in src and ".put(" not in src and ".patch(" not in src and ".delete(" not in src
