"""Advisory security scanners: pinned, read-only, secret-free, on the dedicated CI runner.
Event filters and existing required gates remain unchanged."""
import re
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"
SCANNERS = ["dependency-scan.yml", "workflow-lint.yml", "prowler.yml", "secret-scan.yml"]
POLICY = yaml.safe_load((ROOT / "agents/repository_guardian.yaml").read_text())


def load(name):
    text = (WORKFLOWS / name).read_text()
    workflow = yaml.safe_load(text)
    return text, workflow, workflow.get("on", workflow.get(True))


@pytest.mark.parametrize("name", SCANNERS)
def test_scanner_is_pinned_read_only_secret_free_and_advisory(name):
    text, workflow, _ = load(name)
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in text and "sez-deploy" not in text
    assert f".github/workflows/{name}" not in POLICY["workflows"]
    for job in workflow["jobs"].values():
        assert job["runs-on"] == "ubuntu-24.04"
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
            if step.get("uses", "").startswith("actions/checkout"):
                assert step["with"]["persist-credentials"] is False
            if "docker run" in step.get("run", ""):
                assert "@sha256:" in step["run"]                  # images run directly are digest-pinned


def test_scanners_run_only_when_their_inputs_change_or_on_a_schedule():
    _, _, on = load("dependency-scan.yml")
    assert "requirements*.txt" in on["pull_request"]["paths"] and "desk/package-lock.json" in on["pull_request"]["paths"]
    assert on["schedule"]
    _, _, on = load("workflow-lint.yml")
    assert on["pull_request"]["paths"] == [".github/workflows/**", ".github/actions/**"]
    _, _, on = load("prowler.yml")
    assert set(on) == {"schedule", "workflow_dispatch"}


def test_workflow_lint_pins_both_tools_and_never_uploads_sarif():
    _, workflow, _ = load("workflow-lint.yml")
    steps = {s.get("name"): s for s in workflow["jobs"]["lint"]["steps"]}
    assert re.fullmatch(r"\d+\.\d+\.\d+", steps["zizmor"]["with"]["version"])
    assert steps["zizmor"]["with"]["advanced-security"] is False
    assert "rhysd/actionlint:1.7.12@sha256:" in steps["actionlint"]["run"]


def test_dependency_pr_compares_the_exact_base_and_head_without_hiding_scan_errors(tmp_path):
    _, workflow, _ = load("dependency-scan.yml")
    steps = {s.get("name"): s for s in workflow["jobs"]["osv"]["steps"]}
    checkout = workflow["jobs"]["osv"]["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0
    assert "github.event.pull_request.head.sha" in checkout["with"]["ref"]
    assert steps["Checkout PR base"]["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert steps["Checkout scan head"]["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    report = steps["Report vulnerabilities"]["with"]["scan-args"]
    assert "--old=/github/runner_temp/osv-old.json" in report
    assert "--new=/github/runner_temp/osv-new.json" in report
    assert "--fail-on-vuln=true" in report
    assert "upload-sarif" not in report

    # Execute the real completion guard: a scanner crash must not become a clean comparison.
    guard = steps["Require completed scans"]["run"]
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "EVENT_NAME": "pull_request"}
    for old, new, success in [(None, None, False), ({"results": []}, None, False),
                              ({"results": []}, {"error": "scan failed"}, False),
                              ({"results": []}, {"results": []}, True)]:
        for name, value in [("old", old), ("new", new)]:
            path = tmp_path / f"osv-{name}.json"
            path.unlink(missing_ok=True)
            if value is not None:
                path.write_text(json.dumps(value))
        result = subprocess.run(["bash", "-e", "-c", guard], env=env, capture_output=True)
        assert (result.returncode == 0) is success, result.stderr.decode()


def test_prowler_never_sends_results_to_cloud_or_code_scanning():
    _, workflow, _ = load("prowler.yml")
    step = next(s for s in workflow["jobs"]["iac"]["steps"] if s.get("name") == "Prowler Security Scan")
    assert step["with"]["provider"] == "iac"
    assert step["with"]["push-to-cloud"] == "false"
    assert step["with"]["upload-sarif"] == "false"
