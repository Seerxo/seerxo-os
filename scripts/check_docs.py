#!/usr/bin/env python3
"""Lightweight checks for the session-continuity documents. Standard library only.

Errors (exit 1): broken relative links or repo-path references, missing HANDOFF fields,
invalid roadmap statuses, and DONE milestones without a PR or commit in their Evidence line.
Warnings (exit 0): stale HANDOFF date or stale CURRENT_STATE snapshot. Staleness is time-based,
so it is reported, never asserted in tests.

Every document in LINK_DOCS is required. --public-profile (the published repository) allows exactly the internal
handoff set in PRIVATE_DOCS to be absent; anything else missing is still an error.

Usage: python3 scripts/check_docs.py [--repo PATH] [--public-profile]
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

LINK_DOCS = ["README.md", "CONTRIBUTING.md", "SECURITY.md", "START_HERE.md", "CLAUDE.md", "docs/GIT_WORKFLOW.md", "docs/HANDOFF.md", "docs/DEVELOPMENT_PLAN.md", "docs/HANDOFF_HISTORY.md", "docs/AUTONOMY_ROADMAP.md"]
PRIVATE_DOCS = {"docs/HANDOFF.md", "docs/DEVELOPMENT_PLAN.md", "docs/HANDOFF_HISTORY.md", "docs/AUTONOMY_ROADMAP.md"}
HANDOFF_FIELDS = ["Last updated", "Main SHA", "Deployed API SHA", "Live state", "Tests run", "Blocked", "Next executable step"]
STATUSES = {"NOT_STARTED", "IN_PROGRESS", "IMPLEMENTED", "BLOCKED", "DONE"}
REPO_PATH = re.compile(r"`((?:docs|scripts|app|tests|agents|skills|migrations|desk|api)/[A-Za-z0-9_./-]+)`")
MD_LINK = re.compile(r"\]\(([^)\s]+)\)")
EVIDENCE_REF = re.compile(r"#\d+|\b[0-9a-f]{7,40}\b")
DATE = re.compile(r"Last updated:\*\*\s*(\d{4}-\d{2}-\d{2})")
MILESTONE = re.compile(r"^### (P\d-\d) ", re.M)
STATUS = re.compile(r"^- \*\*Status:\*\* `([A-Z_]+)`", re.M)
EVIDENCE = re.compile(r"^- \*\*Evidence:\*\* (.+)$", re.M)
GENERATED = re.compile(r"Generated at UTC: `([^`]+)`|^generated (\S+)", re.M)


def check_links(repo: Path, rel: str) -> list[str]:
    path = repo / rel
    text = path.read_text(encoding="utf-8")
    errors = []
    for target in MD_LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target_path = target.split("#", 1)[0]
        if target_path and not (path.parent / target_path).exists():
            errors.append(f"{rel}: broken link -> {target}")
    for ref in REPO_PATH.findall(text):
        if not (repo / ref).exists():
            errors.append(f"{rel}: referenced path does not exist -> {ref}")
    return errors


def check_handoff(repo: Path) -> tuple[list[str], list[str]]:
    rel = "docs/HANDOFF.md"
    text = (repo / rel).read_text(encoding="utf-8")
    errors = [f"{rel}: missing field '{f}'" for f in HANDOFF_FIELDS if not re.search(rf"^- \*\*{re.escape(f)}:\*\*", text, re.M)]
    warnings = []
    match = DATE.search(text)
    if not match:
        errors.append(f"{rel}: 'Last updated' must be an ISO date")
    else:
        age = datetime.now(timezone.utc).date() - datetime.strptime(match.group(1), "%Y-%m-%d").date()
        if age > timedelta(days=7):
            warnings.append(f"{rel}: checkpoint is {age.days} days old; verify live state and update it")
    return errors, warnings


def check_plan(repo: Path) -> list[str]:
    rel = "docs/DEVELOPMENT_PLAN.md"
    text = (repo / rel).read_text(encoding="utf-8")
    errors = []
    starts = list(MILESTONE.finditer(text))
    if not starts:
        return [f"{rel}: no milestones found (expected '### P0-1 ...' headings)"]
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        block = text[m.start():end]
        name = m.group(1)
        status = STATUS.search(block)
        if not status:
            errors.append(f"{rel}: {name} has no '- **Status:** `...`' line")
            continue
        if status.group(1) not in STATUSES:
            errors.append(f"{rel}: {name} has unknown status {status.group(1)}")
            continue
        if status.group(1) == "DONE":
            evidence = EVIDENCE.search(block)
            if not evidence or not EVIDENCE_REF.search(evidence.group(1)):
                errors.append(f"{rel}: {name} is DONE but its Evidence line cites no PR or commit")
    return errors


def check_snapshot(repo: Path) -> list[str]:
    path = repo / "docs/CURRENT_STATE.md"
    if not path.exists():
        return []
    match = GENERATED.search(path.read_text(encoding="utf-8"))
    if not match:
        return ["docs/CURRENT_STATE.md: no generation timestamp found"]
    stamp = match.group(1) or match.group(2)
    generated = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if datetime.now(timezone.utc) - generated > timedelta(hours=24):
        return [f"docs/CURRENT_STATE.md: snapshot from {stamp} is stale; live state is UNKNOWN until verified"]
    return []


def run(repo: Path, public_profile: bool = False) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for rel in LINK_DOCS:
        if (repo / rel).exists():
            errors += check_links(repo, rel)
        elif not (public_profile and rel in PRIVATE_DOCS):
            errors.append(f"{rel}: required document is missing")
    if (repo / "docs/HANDOFF.md").exists():
        handoff_errors, handoff_warnings = check_handoff(repo)
        errors += handoff_errors
        warnings += handoff_warnings
    if (repo / "docs/DEVELOPMENT_PLAN.md").exists():
        errors += check_plan(repo)
    warnings += check_snapshot(repo)
    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--public-profile", action="store_true", help="allow only PRIVATE_DOCS to be absent")
    args = parser.parse_args()
    errors, warnings = run(args.repo, args.public_profile)
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    print(f"check_docs: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
