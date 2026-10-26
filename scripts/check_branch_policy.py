#!/usr/bin/env python3
"""Deterministic checks for the Git workflow policy (docs/GIT_WORKFLOW.md). Standard library only.

Advisory: these checks run in CI and in the CLI. They are NOT GitHub-enforced branch protection,
which this private repository cannot use without a paid plan. The merge gate is scripts/merge-pr.

Usage:
  python3 scripts/check_branch_policy.py branch NAME
  python3 scripts/check_branch_policy.py title "PR TITLE"
"""
from __future__ import annotations

import re
import sys

# Task-scoped prefixes. A new branch uses one of these and a lowercase slug.
BRANCH = re.compile(r"^(feat|fix|docs|test|ci|chore|refactor|perf|hardening|security)/[a-z0-9][a-z0-9._-]{0,80}$")
# Names that must never be created or deleted by automation.
PROTECTED = re.compile(r"^(main|master|develop|release/.*|hotfix/.*)$")
# Conventional Commits 1.0 subject: type(scope)!: description
TITLE = re.compile(
    r"^(feat|fix|docs|test|ci|chore|refactor|perf|build|style|revert)(\([a-z0-9._/-]+\))?!?: \S.{0,99}$"
)


def check_branch(name: str) -> list[str]:
    if PROTECTED.match(name):
        return [f"branch '{name}' is a protected or permanent name; do not create it"]
    if not BRANCH.match(name):
        return [f"branch '{name}' must match <prefix>/<lowercase-slug>, prefix in feat|fix|docs|test|ci|chore|refactor|perf|hardening|security"]
    return []


def check_title(title: str) -> list[str]:
    if not TITLE.match(title):
        return [f"PR title '{title}' is not a Conventional Commit subject (type(scope): description)"]
    return []


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("branch", "title"):
        print(__doc__, file=sys.stderr)
        return 2
    errors = check_branch(argv[2]) if argv[1] == "branch" else check_title(argv[2])
    for e in errors:
        print(f"ERROR {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
