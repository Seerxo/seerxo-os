"""Read-only GitHub adapter; run one reconciliation with python -m app.repo_guardian_github.

This belongs to the control plane. Never execute code from an observed repository.
No raw CI logs, credential values, webhook ingress or model calls are collected.
"""
import argparse
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import subprocess
from urllib.parse import quote

import yaml
from sqlalchemy import select

from app import commit_statuses, enums, models, repo_guardian


def api(path: str, expression: str):
    try:
        result = subprocess.run(["gh", "api", "--method", "GET", path, "--jq", expression],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("GitHub read unavailable or timed out") from error
    if result.returncode or len(result.stdout) > 2_000_000:
        raise RuntimeError("GitHub read failed; no checkpoint advanced")
    return json.loads(result.stdout)


BRANCH = re.compile(r"^(?!.*\.\.)(?!/)[A-Za-z0-9._/-]{1,100}(?<!/)$")


def collect(repository: str, base_sha: str | None, workflows: set[str], branch: str | None = None, *,
            publisher: dict | None = None, status_contexts: tuple[str, ...] = ()) -> dict:
    """Observe `branch` (the project's tracked branch from Git policy). Without it, GitHub's
    default branch: wrong for youtube-auto, whose server builds from server-master.
    `status_contexts` are the trusted publisher's commit statuses (CircleCI) read for the same SHA;
    missing, pending, failed, spoofed or incomplete statuses make the observation UNKNOWN."""
    if repo_guardian.repository_name(repository) != repository:
        raise ValueError("canonical repository required")
    if base_sha and not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        raise ValueError("invalid checkpoint")
    if branch is None:
        branch = api(f"repos/{repository}", "{default_branch}")["default_branch"]
    if not BRANCH.fullmatch(branch):
        raise ValueError("invalid branch name")
    commit = api(f"repos/{repository}/commits/{quote(branch, safe='')}", "{sha}")
    sha = commit["sha"]
    # Validate SHA before it is ever used in a URL or persisted.
    repo_guardian.Snapshot(repository=repository, main_sha=sha, observed_at=datetime.now(timezone.utc))
    files, complete = [], base_sha == sha
    if base_sha and base_sha != sha:
        comparison = api(f"repos/{repository}/compare/{base_sha}...{sha}",
                         '{status, total_commits, files: [.files[].filename]}')
        files = comparison["files"][:100]
        complete = comparison["status"] in {"ahead", "identical"} and len(comparison["files"]) < 100
    runs = api(f"repos/{repository}/actions/runs?branch={quote(branch, safe='')}&head_sha={sha}&per_page=100",
               '{total_count, workflow_runs: [.workflow_runs[] | {id, workflow_id, name, path, head_sha, head_branch, status, conclusion, run_number, run_attempt, event}]}') if workflows else {
                   "total_count": 0, "workflow_runs": []}
    latest = {}
    for run in runs["workflow_runs"]:
        if run["path"] not in workflows or run["head_sha"] != sha or run["head_branch"] != branch or run["event"] == "pull_request":
            continue
        key = run["path"]
        order = (run["run_number"], run["run_attempt"])
        if key not in latest or order > (latest[key]["run_number"], latest[key]["run_attempt"]):
            latest[key] = run
    checks = [{k: run[k] for k in ("id", "name", "head_sha", "status", "conclusion")} for run in latest.values()]
    statuses = {"statuses": {}, "status_ids": {}, "statuses_complete": True, "status_problems": []}
    if status_contexts:
        if publisher is None:
            raise ValueError("commit statuses need a trusted publisher")
        raw = api(f"repos/{repository}/commits/{sha}/statuses?per_page={commit_statuses.STATUS_PAGE}",
                  commit_statuses.STATUS_QUERY)
        statuses = commit_statuses.latest_statuses(raw, tuple(status_contexts), publisher)
        for check in commit_statuses.status_checks(statuses["statuses"], tuple(status_contexts)):
            checks.append({"id": statuses["status_ids"][check["name"]], "name": check["name"], "head_sha": sha,
                           "status": check["status"], "conclusion": check["conclusion"]})
    statuses_ok = (statuses["statuses_complete"] and not statuses["status_problems"]
                   and set(statuses["statuses"]) == set(status_contexts))
    return {"repository": repository, "main_sha": sha, "diff_base_sha": base_sha,
            "observed_at": datetime.now(timezone.utc).isoformat(), "changed_files": files,
            "diff_complete": complete, "checks": checks,
            "checks_complete": type(runs["total_count"]) is int
                               and 0 <= runs["total_count"] == len(runs["workflow_runs"]) < 100 and workflows == set(latest)
                               and bool(checks) and all(c["status"] == "completed" for c in checks)
                               and statuses_ok}


def load_policy(path: Path) -> dict:
    class UniquePolicyLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
            if len(keys) != len(set(keys)):
                raise ValueError("guardian policy contains duplicate keys")
            return super().construct_mapping(node, deep=deep)

    policy = yaml.load(path.read_text(), Loader=UniquePolicyLoader)
    if not isinstance(policy, dict) or type(policy.get("enabled")) is not bool:
        raise ValueError("guardian policy requires a literal enabled boolean")
    repositories = policy.get("repositories")
    workflows = policy.get("workflows")
    if not isinstance(repositories, list) or not repositories or len(repositories) > 2 or any(repo_guardian.repository_name(r) != r for r in repositories):
        raise ValueError("guardian policy requires one or two canonical allowed repositories")
    # Empty is explicit: CircleCI commit statuses (merge_statuses) are then the whole CI evidence.
    if not isinstance(workflows, list) or any(not isinstance(w, str) or not w.startswith(".github/workflows/") or ".." in w for w in workflows):
        raise ValueError("guardian policy requires explicit workflow paths")
    # Every repository names the branch the business runs; GitHub's default is never assumed.
    branches = policy.get("branches")
    if (not isinstance(branches, dict) or set(branches) != set(repositories)
            or not all(isinstance(b, str) and BRANCH.fullmatch(b) for b in branches.values())):
        raise ValueError("guardian policy requires a tracked branch for every repository")
    # Optional: the fork Stage C pushes task branches to and opens PRs from (B5: the agent has only Read
    # on the repository itself). Absent means the repository's own branches, as before.
    forks = policy.get("publish_forks", {})
    if (not isinstance(forks, dict) or not set(forks) <= set(repositories)
            or not all(isinstance(f, str) and repo_guardian.repository_name(f) == f and f != r
                       for r, f in forks.items())):
        raise ValueError("guardian policy publish_forks must map an allowed repository to one canonical fork")
    return policy


def tracked_branch(policy: dict, repository: str) -> str:
    """The branch Guardian observes and Stage C opens PRs against for this repository."""
    return policy["branches"][repository]


def publish_repository(policy: dict, repository: str) -> str:
    """The only repository a Stage C PR head for `repository` may come from: its configured fork, else itself."""
    return policy.get("publish_forks", {}).get(repository, repository)


def merge_statuses(policy: dict) -> tuple[dict, tuple[str, ...]]:
    """(trusted publisher, required commit-status contexts) for Stage C merge eligibility.

    Fail closed by construction: a missing or malformed declaration raises. The status list may be empty
    only when the policy also requires Actions workflows; with neither CI source it raises. Guardian itself
    does not read commit statuses; it ignores these keys.
    """
    publisher = policy.get("status_publisher")
    contexts = policy.get("required_statuses")
    if (not isinstance(publisher, dict) or set(publisher) != {"login", "id"}
            or not isinstance(publisher["login"], str) or not publisher["login"]
            or not isinstance(publisher["id"], int) or isinstance(publisher["id"], bool) or publisher["id"] <= 0):
        raise ValueError("merge gate requires a status_publisher with a login and a numeric id")
    if (not isinstance(contexts, list) or len(set(contexts)) != len(contexts)
            or not all(isinstance(c, str) and 0 < len(c) <= 255 for c in contexts)):
        raise ValueError("merge gate requires unique required_statuses")
    # At least one CI source: statuses may be empty only while required Actions workflows are listed.
    if not contexts and not policy.get("workflows"):
        raise ValueError("merge gate requires required workflows or required statuses")
    return publisher, tuple(contexts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist only when Git policy explicitly enables Guardian")
    args = parser.parse_args()
    # Runtime policy comes from this Git checkout, never an arbitrary CLI file.
    policy = load_policy(Path(__file__).resolve().parents[1] / "agents/repository_guardian.yaml")
    if args.apply and not policy["enabled"]:
        print(json.dumps({"skipped": "disabled", "created": 0}))
        return
    from app.db import SessionLocal
    with SessionLocal() as db:
        projects = list(db.scalars(select(models.Project).where(models.Project.status == enums.ProjectStatus.ACTIVE).order_by(models.Project.key)))
        seen = set()
        for project in projects:
            repository = repo_guardian.repository_name(project.repository)
            if repository not in policy["repositories"] or repository in seen:
                continue
            seen.add(repository)
            row = db.get(models.ControlState, repo_guardian.checkpoint_key(repository))
            base = (row.value or {}).get("last_seen_main_sha") if row else None
            try:
                publisher, contexts = merge_statuses(policy)
                facts = collect(repository, base, set(policy["workflows"]), tracked_branch(policy, repository),
                                publisher=publisher, status_contexts=contexts)
                result = repo_guardian.reconcile(db, project.key, facts, allowlist=set(policy["repositories"]),
                                                 enabled=True, apply=args.apply)
                print(json.dumps({"project": project.key, "repository": repository, "head_sha": facts["main_sha"],
                                  "ci": "observed" if facts["checks_complete"] else "UNKNOWN", **result}))
            except Exception as error:
                db.rollback()
                # Do not dump provider/HTTP errors or CI logs that may contain secrets.
                print(json.dumps({"repository": repository, "error_type": type(error).__name__, "created": 0}))
                raise SystemExit(1) from None


if __name__ == "__main__":
    main()
