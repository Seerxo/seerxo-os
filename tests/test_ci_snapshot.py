"""tests/fixtures/ci_snapshot is exactly the repository's CI: what the CI-contract tests assert is visible to the
release reviewer, whose packet covers only the deployed paths (not .github/)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests/fixtures/ci_snapshot"


def test_the_snapshot_equals_the_real_workflows_byte_for_byte():
    real = {p.name: p.read_bytes() for p in (ROOT / ".github/workflows").glob("*.yml")}
    copy = {p.name: p.read_bytes() for p in SNAPSHOT.glob("*.yml")}
    assert real and copy == real


def test_no_circleci_and_no_jules_workflow():
    assert not (ROOT / ".circleci").exists()
    assert not any("jules" in p.name.lower() for p in (ROOT / ".github/workflows").iterdir())
