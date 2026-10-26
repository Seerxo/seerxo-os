"""Notebook memory and compaction (§53A).

The rules being protected: a note is not a learning, deduplication is deterministic,
the notebook never grows forever, and nothing is ever deleted.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import models, notebook


# --- normalisation and fingerprinting --------------------------------------------

def test_word_order_does_not_create_a_second_note():
    a = notebook.fingerprint("Run migrations before restarting the service")
    b = notebook.fingerprint("Before restarting the service, run the migrations")
    assert a == b


def test_stopwords_and_case_are_ignored():
    assert notebook.fingerprint("The API returns an error for this input") == \
           notebook.fingerprint("api returns error for input")


def test_different_lessons_stay_different():
    a = notebook.fingerprint("Run migrations before restart")
    b = notebook.fingerprint("Never deploy on a Friday")
    assert a != b


def test_normalise_drops_short_tokens_and_sorts():
    assert notebook.normalise("Deploy A B service now") == "deploy now service"


def test_fingerprint_is_short_and_stable():
    fp = notebook.fingerprint("anything at all here")
    assert len(fp) == 32
    assert fp == notebook.fingerprint("anything at all here")


# --- compaction planning ----------------------------------------------------------

def _note(id_, refs, days_old, promoted=False, archived=False):
    n = models.AgentNote(note=f"note {id_}", fingerprint=f"fp{id_}")
    n.id = id_
    n.reference_count = refs
    n.created_at = datetime.now(timezone.utc) - timedelta(days=days_old + 1)
    n.last_seen_at = datetime.now(timezone.utc) - timedelta(days=days_old)
    n.promoted_learning_id = 1 if promoted else None
    n.archived_at = datetime.now(timezone.utc) if archived else None
    n.project_id = None
    n.tags = None
    return n


class _Session:
    """Stand-in for a SQLAlchemy session. `flush` assigns primary keys, as the real one
    does — without that, promotion silently leaves notes unmarked."""

    def __init__(self, notes):
        self._notes = [n for n in notes if n.archived_at is None]
        self._pending: list = []
        self._next_id = 100

    def execute(self, _stmt): return self
    def scalars(self): return self
    def all(self): return [n for n in self._notes if isinstance(n, models.AgentNote)]
    def add(self, obj): self._notes.append(obj); self._pending.append(obj)

    def flush(self):
        for obj in self._pending:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1
        self._pending.clear()

    def commit(self): self.flush()


class _SessionThatNeverAssignsIds(_Session):
    def flush(self): pass


def test_recurring_note_is_promoted():
    plan = notebook.plan_compaction(_Session([_note(1, refs=2, days_old=1)]))
    assert [n.id for n in plan.promote] == [1]
    assert plan.archive == []


def test_single_old_note_is_archived_as_noise():
    # It was written once and never came up again in the retention window.
    plan = notebook.plan_compaction(_Session([_note(1, refs=1, days_old=40)]))
    assert [n.id for n in plan.archive] == [1]


def test_single_recent_note_is_kept_because_it_may_yet_recur():
    plan = notebook.plan_compaction(_Session([_note(1, refs=1, days_old=2)]))
    assert [n.id for n in plan.keep] == [1]
    assert plan.promote == [] and plan.archive == []


def test_already_promoted_note_is_not_promoted_twice():
    plan = notebook.plan_compaction(_Session([_note(1, refs=9, days_old=1, promoted=True)]))
    assert plan.promote == []
    assert [n.id for n in plan.keep] == [1]


def test_an_old_but_recurring_note_is_promoted_not_archived():
    # Recurrence beats age: a lesson that keeps coming back is the most valuable kind.
    plan = notebook.plan_compaction(_Session([_note(1, refs=5, days_old=90)]))
    assert [n.id for n in plan.promote] == [1]
    assert plan.archive == []


def test_promote_threshold_is_configurable():
    notes = [_note(1, refs=2, days_old=1)]
    assert notebook.plan_compaction(_Session(notes), promote_after=3).promote == []
    assert len(notebook.plan_compaction(_Session(notes), promote_after=2).promote) == 1


# --- applying ---------------------------------------------------------------------

def test_compaction_archives_rather_than_deletes():
    n = _note(1, refs=1, days_old=40)
    session = _Session([n])
    plan = notebook.plan_compaction(session)
    notebook.apply_compaction(session, plan)
    assert n.archived_at is not None
    assert n.archived_reason
    assert n in session._notes          # §155: history is append-only


def test_promotion_creates_a_candidate_not_a_rule():
    n = _note(1, refs=3, days_old=2)
    session = _Session([n])
    plan = notebook.plan_compaction(session)
    notebook.apply_compaction(session, plan)
    learning = [x for x in session._notes if isinstance(x, models.Learning)][0]
    # §53A: the supervisor, and the founder for high impact, still decide. The wording
    # must not imply the rule is already adopted.
    assert learning.title.startswith("Candidate rule")
    assert "3 independent runs" in learning.evidence
    assert n.promoted_learning_id is not None


def test_promotion_refuses_to_leave_a_note_unmarked():
    # If the learning has no id, marking the note is impossible and it would be promoted
    # again at every review. Failing loudly beats multiplying candidate learnings.
    session = _SessionThatNeverAssignsIds([_note(1, refs=3, days_old=2)])
    plan = notebook.plan_compaction(session)
    with pytest.raises(RuntimeError, match="re-promote it every week"):
        notebook.apply_compaction(session, plan)


def test_apply_reports_what_it_did():
    session = _Session([_note(1, refs=2, days_old=1), _note(2, refs=1, days_old=40),
                        _note(3, refs=1, days_old=1)])
    result = notebook.apply_compaction(session, notebook.plan_compaction(session))
    assert result == {"promoted": 1, "archived": 1, "kept": 1}


def test_dry_run_plan_changes_nothing():
    n = _note(1, refs=1, days_old=40)
    session = _Session([n])
    notebook.plan_compaction(session)
    assert n.archived_at is None        # planning alone must not mutate


def test_plan_summary_is_json_safe():
    session = _Session([_note(1, refs=2, days_old=1)])
    summary = notebook.plan_compaction(session).summary()
    import json
    json.dumps(summary)
    assert summary["promote"][0]["refs"] == 2
