"""The TruffleHog secret scan runs pinned (action commit and scanner image digest), read-only, without secrets."""
import re
from pathlib import Path

import yaml

TEXT = (Path(__file__).parents[1] / ".github/workflows/secret-scan.yml").read_text()
WORKFLOW = yaml.safe_load(TEXT)


def test_secret_scan_is_pinned_read_only_and_secret_free():
    assert WORKFLOW.get("on", WORKFLOW.get(True)) == {"pull_request": {"branches": ["main"]},
                                                      "push": {"branches": ["main"]}}
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert "secrets." not in TEXT and "sez-deploy" not in TEXT
    steps = WORKFLOW["jobs"]["trufflehog"]["steps"]
    for step in steps:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    assert steps[0]["with"] == {"fetch-depth": 0, "persist-credentials": False}
    assert re.fullmatch(r"\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", steps[1]["with"]["version"])
