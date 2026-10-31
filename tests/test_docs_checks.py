"""Checks for the session-continuity documents (scripts/check_docs.py)."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_docs", REPO / "scripts" / "check_docs.py")
check_docs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_docs)

HANDOFF_OK = """# HANDOFF
- **Last updated:** 2026-10-12 (UTC)
- **Main SHA:** `abc1234`
- **Deployed API SHA:** UNKNOWN
- **Live state:** UNKNOWN
- **Tests run:** none
- **Blocked:** none
- **Next executable step:** none
"""

PLAN_OK = """# PLAN
### P0-1 Thing
- **Status:** `BLOCKED`
- **Evidence:** pending

### P0-2 Done thing
- **Status:** `DONE`
- **Evidence:** PR #245 merged; pytest -q passed
"""


def _repo(tmp_path: Path, plan: str = PLAN_OK, handoff: str = HANDOFF_OK) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "START_HERE.md").write_text("[plan](docs/DEVELOPMENT_PLAN.md)\n")
    for name in ("CLAUDE.md", "README.md", "CONTRIBUTING.md", "SECURITY.md"):
        (tmp_path / name).write_text("ok\n")
    (tmp_path / "docs/HANDOFF.md").write_text(handoff)
    (tmp_path / "docs/HANDOFF_HISTORY.md").write_text("history\n")
    (tmp_path / "docs/GIT_WORKFLOW.md").write_text("workflow\n")
    (tmp_path / "docs/AUTONOMY_ROADMAP.md").write_text("roadmap\n")
    (tmp_path / "docs/DEVELOPMENT_PLAN.md").write_text(plan)
    return tmp_path


def test_repository_docs_pass_checks():
    errors, _ = check_docs.run(REPO, public_profile=True)
    assert errors == []


def test_a_missing_required_document_is_an_error_by_default(tmp_path):
    repo = _repo(tmp_path)
    (repo / "docs/HANDOFF_HISTORY.md").unlink()
    errors, _ = check_docs.run(repo)
    assert "docs/HANDOFF_HISTORY.md: required document is missing" in errors
    assert check_docs.run(repo, public_profile=True)[0] == []


def test_the_public_profile_exempts_only_the_private_handoff_set(tmp_path):
    repo = _repo(tmp_path)
    (repo / "SECURITY.md").unlink()
    errors, _ = check_docs.run(repo, public_profile=True)
    assert "SECURITY.md: required document is missing" in errors


def test_clean_fixture_has_no_errors(tmp_path):
    errors, _ = check_docs.run(_repo(tmp_path))
    assert errors == []


def test_broken_link_is_an_error(tmp_path):
    repo = _repo(tmp_path)
    (repo / "START_HERE.md").write_text("[gone](docs/NOPE.md)\n")
    errors, _ = check_docs.run(repo)
    assert any("broken link -> docs/NOPE.md" in e for e in errors)


def test_missing_repo_path_reference_is_an_error(tmp_path):
    repo = _repo(tmp_path, plan=PLAN_OK + "\nSee `docs/MISSING.md`.\n")
    errors, _ = check_docs.run(repo)
    assert any("docs/MISSING.md" in e for e in errors)


def test_missing_handoff_field_is_an_error(tmp_path):
    handoff = HANDOFF_OK.replace("- **Blocked:** none\n", "")
    errors, _ = check_docs.run(_repo(tmp_path, handoff=handoff))
    assert any("missing field 'Blocked'" in e for e in errors)


def test_done_without_pr_or_commit_is_an_error(tmp_path):
    plan = PLAN_OK.replace("PR #245 merged; pytest -q passed", "looks finished to me")
    errors, _ = check_docs.run(_repo(tmp_path, plan=plan))
    assert any("P0-2 is DONE but its Evidence line cites no PR or commit" in e for e in errors)


def test_unknown_status_is_an_error(tmp_path):
    plan = PLAN_OK.replace("`BLOCKED`", "`FINISHED`")
    errors, _ = check_docs.run(_repo(tmp_path, plan=plan))
    assert any("unknown status FINISHED" in e for e in errors)


def test_stale_handoff_is_a_warning_not_an_error(tmp_path):
    handoff = HANDOFF_OK.replace("2026-10-12", "2020-01-01")
    errors, warnings = check_docs.run(_repo(tmp_path, handoff=handoff))
    assert errors == []
    assert any("days old" in w for w in warnings)
