"""Deterministic branch and PR-title policy checks (scripts/check_branch_policy.py)."""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_branch_policy", REPO / "scripts" / "check_branch_policy.py")
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)


@pytest.mark.parametrize("name", ["feat/add-goal-metric", "fix/deploy-follow-reconnect", "docs/session-continuity", "ci/desk-tests"])
def test_task_branches_are_accepted(name):
    assert policy.check_branch(name) == []


@pytest.mark.parametrize("name", ["main", "master", "develop", "release/1.0", "hotfix/x"])
def test_protected_names_are_rejected(name):
    errors = policy.check_branch(name)
    assert errors and "protected" in errors[0]


@pytest.mark.parametrize("name", ["integration/company-os-readiness", "agent1/release", "Feat/Upper", "fix/", "fix/bad space"])
def test_non_conforming_names_are_rejected(name):
    assert policy.check_branch(name)


@pytest.mark.parametrize("title", ["fix(worker): recover claim", "feat: add metric", "chore!: drop flag", "docs(git): workflow"])
def test_conventional_titles_are_accepted(title):
    assert policy.check_title(title) == []


@pytest.mark.parametrize("title", ["Fix the worker", "update stuff", "fix:", "feat(Bad Scope): x"])
def test_non_conventional_titles_are_rejected(title):
    assert policy.check_title(title)
