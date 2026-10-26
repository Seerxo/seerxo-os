"""Notebook memory and its compaction (§53A).

The founder's "defter" idea, made controlled. Three rules shape this module:

1. A note is cheap and may be wrong. A learning has survived review. They are different
   tables for that reason, and a note becomes a learning only through review.
2. The notebook must not grow forever, and it must not be emptied by deleting. Notes are
   archived with a reason (§155 prefers append-only history).
3. An agent never promotes its own note into permanent rules. Compaction proposes;
   the supervisor and, for high impact, the founder decide.

Deduplication is deterministic (§36A): normalise, hash, count. No model is asked whether
two notes are the same thing.
"""
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import enums, models

# §53A defaults, overridable per agent through registry.yaml memory_policy.
DEFAULT_RETENTION_DAYS = 30
DEFAULT_PROMOTE_AFTER = 2

_WORD = re.compile(r"[a-z0-9]+")
# Words that carry no meaning for "is this the same lesson?".
_STOP = frozenset("""
a an the and or but if then than that this these those is are was were be been being
to of in on at by for with from as it its we our you your i they them their he she
do does did done have has had will would can could should may might must not no
""".split())


def normalise(note: str) -> str:
    """Reduce a note to its meaningful words, sorted.

    Word order and phrasing vary between runs; the lesson does not. Sorting makes
    "run migrations before restart" and "before restart, run the migrations" identical.
    """
    words = sorted({w for w in _WORD.findall(note.lower()) if w not in _STOP and len(w) > 2})
    return " ".join(words)


def fingerprint(note: str) -> str:
    return hashlib.sha256(normalise(note).encode()).hexdigest()[:32]


def record(db: Session, *, note: str, agent_key: str | None = None,
           task_key: str | None = None, tags: str | None = None, commit: bool = True) -> models.AgentNote:
    """Write a note, merging it with an equivalent one instead of duplicating.

    Merging is what makes the reference count meaningful: a lesson learned twice by two
    different runs is worth promoting; the same lesson written twice is not news.
    """
    fp = fingerprint(note)
    agent = task = None
    if agent_key:
        agent = db.execute(select(models.Agent).where(models.Agent.key == agent_key)).scalar_one_or_none()
    if task_key:
        task = db.execute(select(models.Task).where(models.Task.key == task_key)).scalar_one_or_none()

    existing = db.execute(
        select(models.AgentNote).where(
            models.AgentNote.fingerprint == fp,
            models.AgentNote.archived_at.is_(None),
            models.AgentNote.agent_id == (agent.id if agent else None),
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.reference_count += 1
        existing.last_seen_at = datetime.now(timezone.utc)
        if task and existing.task_id is None:
            existing.task_id = task.id
        if commit:
            db.commit()
        else:
            db.flush()
        return existing

    row = models.AgentNote(
        note=note.strip(), fingerprint=fp, tags=tags,
        agent_id=agent.id if agent else None,
        task_id=task.id if task else None,
        project_id=task.project_id if task else None,
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


@dataclass
class CompactionPlan:
    """What a review would do. Produced without changing anything, so it can be shown
    to a human before it runs."""
    promote: list[models.AgentNote]
    archive: list[models.AgentNote]
    keep: list[models.AgentNote]

    def summary(self) -> dict:
        return {"promote": [{"id": n.id, "refs": n.reference_count, "note": n.note[:120]}
                            for n in self.promote],
                "archive": [{"id": n.id, "note": n.note[:80]} for n in self.archive],
                "keep": len(self.keep)}


def plan_compaction(db: Session, *, promote_after: int = DEFAULT_PROMOTE_AFTER,
                    retention_days: int = DEFAULT_RETENTION_DAYS,
                    now: datetime | None = None) -> CompactionPlan:
    """Decide, deterministically, what to promote and what to archive.

    Promote: referenced often enough to be a pattern rather than an anecdote.
    Archive: old and referenced only once — it never came up again, so it was noise.
    Keep: everything else, including recent single-reference notes that may yet recur.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    promote: list[models.AgentNote] = []
    archive: list[models.AgentNote] = []
    keep: list[models.AgentNote] = []

    for n in db.execute(
        select(models.AgentNote).where(models.AgentNote.archived_at.is_(None))
    ).scalars().all():
        if n.promoted_learning_id is not None:
            keep.append(n)
            continue
        if n.reference_count >= promote_after:
            promote.append(n)
            continue
        seen = n.last_seen_at
        if seen is not None and seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if seen is not None and seen < cutoff:
            archive.append(n)
            continue
        keep.append(n)

    return CompactionPlan(promote=promote, archive=archive, keep=keep)


def apply_compaction(db: Session, plan: CompactionPlan, *, actor: str = "compaction") -> dict:
    """Execute a plan.

    Promotion creates a learning marked as a *candidate*: §53A routes it through the
    supervisor, and for high impact the founder, before it becomes a permanent rule.
    Nothing here writes to the constitution, and nothing is deleted.
    """
    now = datetime.now(timezone.utc)
    promoted = 0
    for n in plan.promote:
        learning = models.Learning(
            title=f"Candidate rule ({n.reference_count} runs): {n.note[:100]}",
            lesson=n.note,
            evidence=f"Promoted from notebook after {n.reference_count} independent runs "
                     f"recorded an equivalent note between "
                     f"{n.created_at:%Y-%m-%d} and {n.last_seen_at:%Y-%m-%d}.",
            project_id=n.project_id, tags=n.tags, created_by=actor,
            status=enums.LearningStatus.CANDIDATE,
        )
        db.add(learning)
        db.flush()
        if learning.id is None:
            # Without the id the note keeps promoted_learning_id NULL and would be
            # promoted again at every review, multiplying candidate learnings silently.
            raise RuntimeError(
                f"learning for note {n.id} has no id after flush — refusing to leave "
                f"the note unmarked, which would re-promote it every week"
            )
        n.promoted_learning_id = learning.id
        promoted += 1

    for n in plan.archive:
        n.archived_at = now
        n.archived_reason = f"single reference, unseen for the retention window"

    db.commit()
    return {"promoted": promoted, "archived": len(plan.archive), "kept": len(plan.keep)}
