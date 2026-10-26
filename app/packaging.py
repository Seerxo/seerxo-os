"""YouTube packaging review (TASK-0023): title + thumbnail text checked before upload.

    youtube-auto creates a package  ->  POST /webhooks/youtube-packaging
        -> deterministic findings (exact / first-word / title-template repeats)
             found: REVISE immediately, no model call
             none : one autonomous review task for the Research agent
    youtube-auto, before upload     ->  GET /youtube/packaging/{job}?hash=
        -> PENDING | APPROVED | REVISE | FAILED | UNKNOWN

Decidable repetition is code (§36A); only the judgement "is this packaging distinct and
clickable without being misleading" goes to a model, once per package version.
"""
import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import enums, keys, models

HISTORY_WINDOW = 10      # deterministic checks look this far back
PROMPT_HISTORY = 15      # the reviewer sees this many past packages
APPROVE, REVISE = "VERDICT: APPROVE", "VERDICT: REVISE"


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def package_hash(title: str, thumb: str) -> str:
    return hashlib.sha256(f"{title}\n{thumb}".encode()).hexdigest()[:16]


def findings(title: str, thumb: str, history: list[dict]) -> list[str]:
    """Repetition a rule can decide. Empty list means: let the reviewer judge."""
    recent = history[:HISTORY_WINDOW]
    out = []
    t_words = _words(thumb)
    past_thumbs = [_words(h.get("thumbnailText", "")) for h in recent]
    if t_words and t_words in past_thumbs:
        out.append(f"thumbnail text '{thumb}' was already used in the last {HISTORY_WINDOW} videos")
    if t_words:
        same_first = sum(1 for p in past_thumbs if p and p[0] == t_words[0])
        if same_first >= 2:
            out.append(f"thumbnail starts with '{t_words[0].upper()}' like {same_first} of the last "
                       f"{HISTORY_WINDOW} thumbnails")
    head = _words(title)[:2]
    if len(head) == 2:
        same_head = sum(1 for h in recent if _words(h.get("title", ""))[:2] == head)
        if same_head >= 2:
            out.append(f"title opens with '{' '.join(head)}' like {same_head} recent titles")
    return out


def _key(job: str, h: str) -> str:
    return f"packaging:{job}:{h}"


def submit(db: Session, job: str, title: str, thumb: str, history: list[dict]) -> dict:
    """Record a package version; create at most one review task per version."""
    h = package_hash(title, thumb)
    rules = findings(title, thumb, history)
    if rules:
        return {"job": job, "hash": h, "status": "REVISE", "reasons": rules, "task": None}
    existing = db.execute(select(models.Task).where(models.Task.dedupe_key == _key(job, h))).scalar_one_or_none()
    if existing:
        return {"job": job, "hash": h, **_verdict(existing)}
    research = db.execute(select(models.Agent).where(models.Agent.key == "research")).scalar_one()
    unit = db.execute(select(models.BusinessUnit).where(models.BusinessUnit.key == "youtube")).scalar_one_or_none()
    package = {"job": job, "title": title, "thumbnailText": thumb,
               "recent_packages": history[:PROMPT_HISTORY]}
    task = models.Task(
        title=f"Packaging review: {title[:120]}",
        objective="Decide whether this YouTube title and thumbnail text are distinct from the "
                  "channel's recent packages and likely to earn a click without misleading.",
        description="Package and recent history (internal data):\n" + json.dumps(package, ensure_ascii=False),
        success_criteria=f"Evidence contains exactly one of '{APPROVE}' or '{REVISE}'. On REVISE, "
                         "evidence names the repeated pattern and gives three alternative "
                         "thumbnail texts (1-3 words) and one alternative title (max 72 chars).",
        status=enums.TaskStatus.READY, priority=enums.Priority.P2, risk_level=enums.RiskLevel.LOW,
        assigned_agent_id=research.id, business_unit_id=unit.id if unit else None,
        created_by="youtube-auto", dedupe_key=_key(job, h), max_attempts=2,
        context={"autonomous": True, "scope": "youtube-packaging",
                 "verify": {"one_of": [APPROVE, REVISE]}, "packaging": {"job": job, "hash": h}})
    try:
        keys.add_with_key(db, task, "TASK")
    except keys.DuplicateOpenWork:  # a concurrent submit of the same package won
        db.rollback()
        existing = db.execute(select(models.Task).where(models.Task.dedupe_key == _key(job, h))).scalar_one()
        return {"job": job, "hash": h, **_verdict(existing)}
    db.commit()
    return {"job": job, "hash": h, "status": "PENDING", "reasons": [], "task": task.key}


def _verdict(task: models.Task) -> dict:
    if task.status != enums.TaskStatus.REVIEW and task.status != enums.TaskStatus.DONE:
        # A cancelled review is final, like a failed one: reporting PENDING made youtube-auto wait on it forever.
        final = task.status in (enums.TaskStatus.FAILED, enums.TaskStatus.CANCELLED)
        reasons = ["review cancelled"] if task.status == enums.TaskStatus.CANCELLED else [task.error] if task.error else []
        return {"status": "FAILED" if final else "PENDING", "reasons": reasons, "task": task.key}
    evidence = (task.artifacts or {}).get("evidence") or []
    text = " ".join([task.result or "", *evidence])
    verdict = "REVISE" if REVISE in text else "APPROVED" if APPROVE in text else "PENDING"
    return {"status": verdict, "reasons": evidence if verdict == "REVISE" else [], "task": task.key}


def status(db: Session, job: str, h: str) -> dict:
    task = db.execute(select(models.Task).where(models.Task.dedupe_key == _key(job, h))).scalar_one_or_none()
    if task is None:
        return {"job": job, "hash": h, "status": "UNKNOWN", "reasons": [], "task": None}
    return {"job": job, "hash": h, **_verdict(task)}
