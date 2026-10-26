"""Which agent proposals may run on their own (§36A, §72–74). Deterministic; no model decides.

The policy rests on one structural fact: a Stage B run has no side effect. The CLIs run in
company-reasoner (no database, GitHub or Telegram credential), Claude with no tools, Codex in
a read-only sandbox, egress limited to the provider hosts. A proposal executed there can
only spend subscription quota and write text that lands in REVIEW. Side effects happen only
through Stage C (its own admission) or a human. So this decides quota and routing, and keeps
anything worded as a side effect away from automation, whatever `kind` the model claimed:

    FORBIDDEN  destructive wording                     -> not created
    APPROVAL   deploy / publish / money / DNS / ...     -> BACKLOG, approval_required
    AUTO       kind research|analysis, no such wording  -> READY for the research agent
    BACKLOG    anything else (planning, code, ops)      -> BACKLOG, as before

AUTO needs PROPOSAL_AUTO_READY=true (compose default on, founder decision 2026-10-06);
off, AUTO falls back to BACKLOG. Bounds stay elsewhere: 3 proposals per run and depth 1
(task_materializer; plus one terminal research -> developer code_change hand-off), the research agent's daily run budget (registry), the supervisor.
"""
import os
import re
from dataclasses import dataclass

AUTO_KINDS = {"research", "analysis"}
AUTO_AGENT = "research"
# ponytail: word lists, not semantics; a wording that slips past still only runs tool-less Stage B.
FORBIDDEN = re.compile(r"\b(delete|drop|truncate|wipe|purge|destroy)\b|rm -rf", re.I)
APPROVAL = re.compile(
    r"\b(deploy\w*|publish\w*|release|launch|billing|payments?|pay|spend\w*|purchase\w*|buy|"
    r"refund\w*|pricing change|dns|migrat\w*|merge\w*|push\w*|credential\w*|secrets?|"
    r"tokens?|password\w*|upgrade\w*|subscribe|email (customers|users)|send (emails?|messages?))\b",
    re.I)


@dataclass(frozen=True)
class Decision:
    route: str              # AUTO | APPROVAL | BACKLOG | FORBIDDEN
    reason: str


def auto_ready_enabled() -> bool:
    return os.environ.get("PROPOSAL_AUTO_READY", "false").lower() == "true"


def classify(title: str, reason: str, kind: str) -> Decision:
    text = f"{title} {reason}"
    if FORBIDDEN.search(text):
        return Decision("FORBIDDEN", f"destructive wording: {FORBIDDEN.search(text).group(0)!r}")
    if APPROVAL.search(text):
        return Decision("APPROVAL", f"side-effect wording: {APPROVAL.search(text).group(0)!r}")
    if kind in AUTO_KINDS:
        if auto_ready_enabled():
            return Decision("AUTO", f"read-only {kind}")
        return Decision("BACKLOG", "PROPOSAL_AUTO_READY is off")
    if kind == "code_change":
        return Decision("BACKLOG", "code change: Stage C admission decides")
    return Decision("BACKLOG", f"{kind}: founder or a later policy makes it READY")
