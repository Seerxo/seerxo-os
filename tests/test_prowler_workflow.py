"""The Prowler scan is advisory, pinned, read-only and credential-free (IaC of this repo only)."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
TEXT = (ROOT / ".github/workflows/prowler.yml").read_text()
WORKFLOW = yaml.safe_load(TEXT)


def test_prowler_scans_repo_iac_pinned_read_only_without_secrets():
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert "secrets." not in TEXT and "sez-deploy" not in TEXT
    steps = WORKFLOW["jobs"]["iac"]["steps"]
    for step in (s for s in steps if "uses" in s):
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    scan = steps[-1]["with"]
    assert scan["provider"] == "iac" and "extra-env" not in scan and scan.get("push-to-cloud") in (None, "false")
    assert re.fullmatch(r"\d+\.\d+\.\d+", scan["image-tag"])
    assert scan["fail-on-findings"] == "false"
    policy = yaml.safe_load((ROOT / "agents/repository_guardian.yaml").read_text())
    assert ".github/workflows/prowler.yml" not in policy["workflows"]
