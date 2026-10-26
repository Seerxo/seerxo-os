"""Human-readable keys (TASK-0042) that stay unique and duplicate-free under concurrent writers.

The database decides, not a prior SELECT: `key` columns are unique, open tasks have a
partial unique index on `dedupe_key`, and unresolved incidents on `open_scope`. A writer
inserts inside a SAVEPOINT; a key taken by a concurrent writer is retried with the next
number, and a duplicate of open work raises DuplicateOpenWork for the caller to resolve.
"""
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

KEY_ATTEMPTS = 5
# Constraint names (PostgreSQL) or "table.column" (SQLite message) per duplicate-work rule.
OPEN_WORK_CONSTRAINTS = ("uq_tasks_open_dedupe", "tasks.dedupe_key",
                         "uq_incidents_open_scope", "incidents.open_scope")


class DuplicateOpenWork(Exception):
    """An equivalent open task or unresolved incident already exists."""


def next_key(db: Session, model, prefix: str, width: int = 4) -> str:
    n = db.execute(select(func.count()).select_from(model)).scalar_one() + 1
    while db.execute(select(model.id).where(model.key == f"{prefix}-{n:0{width}d}")).first():
        n += 1
    return f"{prefix}-{n:0{width}d}"


def _violated(error: IntegrityError) -> str:
    diag = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    return diag or str(error.orig)


def add_with_key(db: Session, obj, prefix: str, width: int = 4):
    """Insert `obj` with a fresh key. Flushes but does not commit."""
    model, table = type(obj), type(obj).__tablename__
    # Flush the caller's other pending changes first, so a rolled-back SAVEPOINT on a key
    # collision cannot discard them (e.g. a superseded decision's status).
    db.flush()
    for _ in range(KEY_ATTEMPTS):
        obj.key = next_key(db, model, prefix, width)
        try:
            with db.begin_nested():
                db.add(obj)
                db.flush()
            return obj
        except IntegrityError as e:
            violated = _violated(e)
            if any(name in violated for name in OPEN_WORK_CONSTRAINTS):
                raise DuplicateOpenWork(violated) from e
            if f"ix_{table}_key" not in violated and f"{table}.key" not in violated:
                raise
    raise RuntimeError(f"no free {prefix} key after {KEY_ATTEMPTS} attempts")
