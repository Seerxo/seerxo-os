"""The self-hosted deploy runner does not re-run tests: it deploys only a commit CI already passed: each
required Actions workflow's push run on main for that exact commit (frontend and python-tests on Blacksmith),
plus any required commit statuses from the trusted publisher. Anything less (spoofed, pending, failed,
cancelled, skipped, missing, other-commit, truncated, unconfigured) refuses."""
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "ci-status-gate.py"
SPEC = importlib.util.spec_from_file_location("ci_status_gate", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
POLICY = yaml.safe_load((ROOT / "agents" / "repository_guardian.yaml").read_text())
PUBLISHER = POLICY["status_publisher"]
WORKFLOWS = tuple(POLICY["workflows"])
# The status path is tested with its own context: the live policy currently requires no statuses.
CONTEXTS = ("ci/example: python-tests",)
SHA = "a" * 40


def status(i, context, state, creator=PUBLISHER):
    return {"id": i, "context": context, "state": state, "creator": dict(creator)}


def green():
    return [status(i + 1, c, "success") for i, c in enumerate(CONTEXTS)]


def run(**overrides):
    return {"id": 1, "path": WORKFLOWS[0], "head_sha": SHA, "head_branch": "main", "event": "push",
            "status": "completed", "conclusion": "success", "run_number": 1, "run_attempt": 1, **overrides}


def runs(*rows, total=None):
    rows = list(rows) if rows else [run()]
    return {"total_count": len(rows) if total is None else total, "workflow_runs": rows}


def all_green():
    """A green push run on main for every required workflow."""
    return runs(*[run(id=i + 1, path=path) for i, path in enumerate(WORKFLOWS)])


ONE = WORKFLOWS[:1]   # run-filter semantics are tested on one workflow; all_green() covers the full set


def test_policy_requires_the_frontend_and_python_workflows_and_no_statuses():
    assert WORKFLOWS == (".github/workflows/frontend.yml", ".github/workflows/python-tests.yml")
    assert POLICY["required_statuses"] == []


def test_every_required_context_green_from_circleci_is_allowed():
    assert gate.evaluate(green(), CONTEXTS, PUBLISHER) == (True, [])


@pytest.mark.parametrize("raw, why", [
    ([], "missing"),
    (green() + [status(99, CONTEXTS[0], "pending")], "pending"),
    (green() + [status(99, CONTEXTS[-1], "failure")], "failure"),
    (green()[1:] + [status(9, CONTEXTS[0], "success", {"id": 1, "login": "someone"})], "untrusted publisher"),
    (green() + [status(100 + i, "other", "success") for i in range(100 - len(CONTEXTS))], "incomplete"),
    ({"not": "a list"}, "malformed"),
])
def test_anything_short_of_trusted_success_is_refused(raw, why):
    allowed, reasons = gate.evaluate(raw, CONTEXTS, PUBLISHER)
    assert not allowed and why in "; ".join(reasons)


def test_status_requirements_need_a_trusted_publisher():
    assert gate.evaluate(green(), CONTEXTS, None)[0] is False
    # No status source alone is not a refusal here; main() refuses when no workflows are required either.
    assert gate.evaluate([], (), PUBLISHER) == (True, [])


def test_a_green_push_run_on_main_for_the_exact_commit_is_allowed():
    assert gate.evaluate_runs(all_green(), WORKFLOWS, SHA) == (True, [])
    assert gate.evaluate_runs(runs(), ONE, SHA) == (True, [])
    # A failed first attempt re-run to success: the latest attempt decides.
    assert gate.evaluate_runs(runs(run(conclusion="failure"), run(id=2, run_attempt=2)), ONE, SHA)[0]


@pytest.mark.parametrize("python_run, why", [
    (None, "no push run"),
    ({"conclusion": "failure"}, "not completed/success"),
    ({"conclusion": "cancelled"}, "not completed/success"),
    ({"conclusion": "skipped"}, "not completed/success"),
    ({"status": "in_progress", "conclusion": None}, "not completed/success"),
    ({"head_sha": "b" * 40}, "no push run"),
    ({"event": "pull_request"}, "no push run"),
])
def test_the_python_workflow_is_required_on_the_exact_main_commit(python_run, why):
    rows = [run(id=1, path=WORKFLOWS[0])]
    if python_run is not None:
        rows.append(run(id=2, path=WORKFLOWS[1], **python_run))
    allowed, reasons = gate.evaluate_runs(runs(*rows), WORKFLOWS, SHA)
    assert not allowed and why in "; ".join(reasons) and WORKFLOWS[1] in "; ".join(reasons)


@pytest.mark.parametrize("raw, why", [
    (runs(run(head_sha="b" * 40)), "no push run"),                         # another commit
    (runs(run(event="pull_request")), "no push run"),                      # PR run is not main evidence
    (runs(run(head_branch="feature")), "no push run"),
    (runs(run(path=".github/workflows/other.yml")), "no push run"),         # another workflow
    ({"total_count": 0, "workflow_runs": []}, "no push run"),
    (runs(run(status="in_progress", conclusion=None)), "not completed/success"),
    (runs(run(conclusion="failure")), "not completed/success"),
    (runs(run(conclusion="cancelled")), "not completed/success"),
    (runs(run(conclusion="skipped")), "not completed/success"),
    (runs(run(), run(id=2, run_number=2, conclusion="failure")), "not completed/success"),  # newer run wins
    (runs(run(), total=101), "incomplete"),
    (runs(run(), total=-1), "incomplete"),
    (runs(run(), total=0), "incomplete"),
    (runs(*[run(id=i, run_number=i) for i in range(1, 101)]), "incomplete"),
    (runs(run(run_number="1")), "malformed"),
    ({"workflow_runs": []}, "malformed"),
    ([], "malformed"),
])
def test_anything_short_of_a_green_main_push_run_is_refused(raw, why):
    allowed, reasons = gate.evaluate_runs(raw, ONE, SHA)
    assert not allowed and why in "; ".join(reasons)


def cli(tmp_path, statuses, run_evidence, policy=None):
    args = [sys.executable, str(SCRIPT), "--sha", SHA]
    if run_evidence is not None:
        path = tmp_path / "runs.json"
        path.write_text(run_evidence if isinstance(run_evidence, str) else json.dumps(run_evidence))
        args += ["--runs", str(path)]
    if policy is not None:
        (tmp_path / "policy.yaml").write_text(yaml.safe_dump(policy))
        args += ["--policy", str(tmp_path / "policy.yaml")]
    return subprocess.run(args, input=statuses if isinstance(statuses, str) else json.dumps(statuses),
                          text=True, capture_output=True)


@pytest.mark.parametrize("statuses", ["", "not JSON", "[null]"])
def test_actions_only_cli_does_not_parse_unused_status_input(tmp_path, statuses):
    result = cli(tmp_path, statuses, all_green())
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("contexts", [None, "", [None], ["ci", "ci"]])
def test_cli_refuses_malformed_status_declarations(tmp_path, contexts):
    assert cli(tmp_path, [], all_green(), {**POLICY, "required_statuses": contexts}).returncode == 1


def test_cli_refuses_an_absent_status_declaration(tmp_path):
    policy = {key: value for key, value in POLICY.items() if key != "required_statuses"}
    assert cli(tmp_path, [], all_green(), policy).returncode == 1


def test_actions_only_deploy_ci_step_does_not_fetch_unused_statuses(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(s["run"] for s in workflow["jobs"]["ci-gate"]["steps"] if "ci-status-gate.py" in s.get("run", ""))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    gh = bindir / "gh"
    gh.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                  'with open(os.environ["GH_TEST_LOG"], "a") as out: out.write(json.dumps(sys.argv[1:]) + "\\n")\n'
                  'if "/statuses" in sys.argv[2]: sys.exit(37)\n'
                  f'print({json.dumps(all_green())!r})\n')
    for binary in bindir.iterdir():
        binary.chmod(0o755)
    log = tmp_path / "gh.log"
    result = subprocess.run(["bash", "-c", step], cwd=ROOT, text=True, capture_output=True,
                            env={**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                                 "RUNNER_TEMP": str(tmp_path), "DEPLOY_SHA": SHA,
                                 "GITHUB_REPOSITORY": "seerxo/os", "GH_TEST_LOG": str(log)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any("/statuses" in json.loads(line)[1] for line in log.read_text().splitlines())


@pytest.mark.parametrize("statuses, run_evidence, code", [
    ([], all_green(), 0),                            # both workflows green; no statuses required
    ([status(1, "ci/circleci: python-tests", "failure")], all_green(), 0),   # a status nobody requires is ignored
    ([], runs(run(id=1, path=WORKFLOWS[0])), 1),     # python-tests missing
    ([], runs(run(id=1, path=WORKFLOWS[1])), 1),     # frontend missing
    ([], runs(run(id=1, path=WORKFLOWS[0]), run(id=2, path=WORKFLOWS[1], conclusion="failure")), 1),
    ([], runs(run(id=1, path=WORKFLOWS[0]), run(id=2, path=WORKFLOWS[1], conclusion="cancelled")), 1),
    ([], None, 1),                                   # run evidence omitted
    ([], "{", 1),
    ("{", all_green(), 0),                         # unused status input is not CI evidence
])
def test_the_cli_exits_nonzero_unless_allowed(tmp_path, statuses, run_evidence, code):
    result = cli(tmp_path, statuses, run_evidence)
    assert result.returncode == code, result.stdout + result.stderr
    assert ("DEPLOY ALLOWED" if code == 0 else "DEPLOY REFUSED") in result.stdout


@pytest.mark.parametrize("workflows", [None, "x", ["../evil.yml"]])
def test_the_cli_refuses_a_malformed_workflow_policy(tmp_path, workflows):
    policy = {**POLICY, "workflows": workflows}
    assert cli(tmp_path, [], all_green(), policy).returncode == 1


def test_the_cli_refuses_a_policy_with_no_ci_evidence(tmp_path):
    policy = {**POLICY, "workflows": [], "required_statuses": []}
    result = cli(tmp_path, [], runs(), policy)
    assert result.returncode == 1 and "no CI evidence is configured" in result.stdout


@pytest.mark.parametrize("statuses, code", [
    ([], 1), ([status(1, CONTEXTS[0], "pending")], 1), ([status(1, CONTEXTS[0], "success", {"id": 1, "login": "x"})], 1),
    ([status(1, CONTEXTS[0], "success")], 0),
])
def test_the_cli_still_enforces_statuses_when_a_policy_requires_them(tmp_path, statuses, code):
    policy = {**POLICY, "required_statuses": list(CONTEXTS)}
    assert cli(tmp_path, statuses, all_green(), policy).returncode == code
