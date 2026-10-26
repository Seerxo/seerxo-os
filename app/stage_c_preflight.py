"""Stage C preflight: is every prerequisite for unattended code writes proven? Zero model calls.

Every prerequisite is reported on its own as PASS, FAIL or UNKNOWN. Only PASS counts: a fact
that could not be observed is UNKNOWN and keeps the report NOT READY. The code-write flag is
reported but is not a prerequisite; it is the last switch, flipped only after this is READY.

Two kinds of evidence meet here. This process (the worker container) sees the database, its
own environment, the jobs directory and the repository policy. The host facts (systemd
launcher, images, egress proxy, coder logins) come from scripts/stage-c-host-facts.py on stdin,
the same collect-on-host, judge-in-app split as the watchdog:

    ssh company-os-host sudo /opt/company-os/scripts/companyctl stage-c preflight
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from app import models, project_profiles, providers, repo_guardian_github, stage_c_credential_boundary, stage_c_worker

ACCEPTANCE_CHECKS = stage_c_worker.ACCEPTANCE_CHECKS
WORKER_UID = 10001  # scripts/company-coder-launch: the only uid that owns the jobs tree
SHA = re.compile(r"[a-f0-9]{40}")
CHECKS_COMMAND = ["python", "-m", "pytest", "-q"]


def _tri(*values) -> str:
    """PASS only if every fact is literally True; any False fails; anything else is UNKNOWN."""
    return "FAIL" if any(v is False for v in values) else "PASS" if all(v is True for v in values) else "UNKNOWN"


def _owned(path: Path, uid: int) -> tuple[str, str]:
    try:
        st = path.stat()
    except OSError:
        return "FAIL", f"{path} is missing"
    if not stat.S_ISDIR(st.st_mode):
        return "FAIL", f"{path} is not a directory"
    if st.st_uid != uid:
        return "FAIL", f"{path} is owned by uid {st.st_uid}, expected {uid}"
    if st.st_mode & 0o022:
        return "FAIL", f"{path} is group/other writable (mode {stat.S_IMODE(st.st_mode):o})"
    if not os.access(path, os.W_OK):
        return "FAIL", f"{path} is not writable by this process"
    return "PASS", f"owned by uid {uid}, mode {stat.S_IMODE(st.st_mode):o}"


def _github_read(path: str, *, actions: bool, env: dict, run) -> tuple[str, str]:
    """Probe the worker's actual read access; never surface GitHub bodies or credentials."""
    try:
        result = run(["gh", "api", "--method", "GET", path], env=env,
                     capture_output=True, text=True, timeout=10)
        if result.returncode:
            code = re.search(r"\bHTTP (401|403|404)\b", result.stderr or "")
            return ("FAIL", f"GitHub refused this read (HTTP {code[1]})") if code else (
                "UNKNOWN", "GitHub read unavailable; check connectivity and rate limits")
        data = json.loads(result.stdout)
        if actions:
            rows = data.get("workflow_runs") if isinstance(data, dict) else None
            valid = isinstance(rows, list) and all(
                isinstance(row, dict) and type(row.get("id")) is int
                and isinstance(row.get("path"), str) and isinstance(row.get("status"), str) for row in rows)
        else:
            valid = isinstance(data, list) and all(
                isinstance(row, dict) and type(row.get("id")) is int and isinstance(row.get("context"), str)
                and isinstance(row.get("state"), str) and row["state"] in {"pending", "success", "failure", "error"}
                and isinstance(row.get("creator"), dict) for row in data)
        return ("PASS", "read access verified; this does not assert CI passed") if valid else (
            "UNKNOWN", "GitHub returned an unexpected response")
    except (OSError, ValueError, subprocess.SubprocessError):
        return "UNKNOWN", "GitHub read unavailable; no CI access proof"


def evaluate(db, *, host: dict | None, health: dict | None, jobs: Path, flag: bool,
             environ: dict | None = None, uid: int = WORKER_UID, policy: Path | None = None,
             profiles: Path | None = None,
             binaries=shutil.which, run=None) -> dict:
    env = os.environ if environ is None else environ
    host = host if isinstance(host, dict) else {}
    checks = []

    def add(name, status, detail=""):
        checks.append({"name": name, "status": status, "detail": detail})

    sha = env.get("COMPANY_OS_GIT_SHA", "")
    add("source_sha", "PASS" if SHA.fullmatch(sha) else "FAIL", sha or "COMPANY_OS_GIT_SHA is not set")

    api, worker = host.get("api_sha"), host.get("worker_sha")
    if not (isinstance(api, str) and isinstance(worker, str) and SHA.fullmatch(api) and SHA.fullmatch(worker)):
        add("api_worker_sha_agree", "UNKNOWN", "host did not report both deployed SHAs")
    elif api == worker == sha:
        add("api_worker_sha_agree", "PASS", sha)
    else:
        add("api_worker_sha_agree", "FAIL", f"api {api[:7]}, worker {worker[:7]}, this process {sha[:7] or '?'}")

    row = db.get(models.ControlState, "stage-c-host-acceptance")
    value = row.value if row and isinstance(row.value, dict) else {}
    add("acceptance_recorded", "PASS" if row and row.updated_by == "founder" else "FAIL",
        "founder attestation" if row else "no stage-c-host-acceptance row")
    add("acceptance_source_sha", "PASS" if SHA.fullmatch(sha) and value.get("source_sha") == sha else "FAIL",
        "acceptance must name the deployed source SHA")
    attested = value.get("checks") if isinstance(value.get("checks"), dict) else {}
    for name in sorted(ACCEPTANCE_CHECKS):
        # Present but not literally true (false, "true", null) fails, as in stage_c_worker.ready.
        add(f"acceptance:{name}", ("PASS" if attested[name] is True else "FAIL") if name in attested else "UNKNOWN")

    add("launcher", _tri(host.get("launcher_path_active"), host.get("launcher_config_ok")),
        "systemd poll timer active and /etc/company-coder/launcher.json valid")
    add("coder_image", _tri(host.get("coder_image_present")), "image ID from launcher.json exists locally")
    add("checks_image", _tri(host.get("checks_image_present")), "checks image ID from launcher.json exists locally")
    fingerprints = host.get("checks_command_fingerprints")
    fingerprint = fingerprints.get("sez-company") if isinstance(fingerprints, dict) else None
    expected = hashlib.sha256(json.dumps(CHECKS_COMMAND, separators=(",", ":")).encode()).hexdigest()
    observed = isinstance(fingerprint, str) and re.fullmatch(r"[a-f0-9]{64}", fingerprint) is not None
    add("checks_command", "UNKNOWN" if not observed else "PASS" if fingerprint == expected else "FAIL",
        "host checks command must match the supported complete pytest suite")
    add("egress_proxy", _tri(host.get("egress_running")), "company-egress container running")
    add("coder_logins", _tri(host.get("claude_env_ok")),
        "separate claude token file (Codex only reviews; it holds no coder login)")
    add("codex_reviewer_volume", _tri(host.get("codex_reviewer_volume_mounted")),
        "company-reasoner must mount company-worker-codex at /home/worker/.codex")
    add("claude_auth", _tri(host.get("claude_auth_ok")),
        "zero-model credential status in the hardened coder container; live access requires the attested Claude edit")

    add("jobs_directory", *_owned(jobs, uid))
    add("repository_mirror", *_owned(jobs / ".control/sez-company.git", uid))
    have_bins = all(binaries(b) for b in ("git", "gh"))
    has_token = bool(env.get("GH_TOKEN"))
    add("github_credential", "PASS" if has_token and have_bins else "FAIL",
        ("GH_TOKEN present (value never shown)" if has_token else "GH_TOKEN is not set in the worker")
        + ("" if have_bins else "; git or gh missing"))

    if health is None:
        add("independent_providers", "UNKNOWN", "provider health unavailable")
    else:
        configured = sorted(n for n, h in health.items()
                            if h in {state.value for state in providers.RUNNABLE_HEALTH})
        add("independent_providers", "PASS" if len(configured) >= 2 else "FAIL",
            f"credentials configured: {', '.join(configured) or 'none'}; live access requires exact-SHA acceptance:claude_edit and acceptance:codex_review_probe")

    status_contexts = None
    try:
        loaded = repo_guardian_github.load_policy(policy or stage_c_worker.POLICY)
        _, status_contexts = repo_guardian_github.merge_statuses(loaded)
        add("guardian_policy", "PASS", f"valid; enabled={loaded['enabled']}")
        repos = loaded["repositories"]
        add("allowed_repository", "PASS" if repos == [stage_c_worker.REPOSITORY] else "FAIL",
            ", ".join(repos))
        add("allowed_branch", "PASS" if loaded["branches"].get(stage_c_worker.REPOSITORY) == "main" else "FAIL",
            str(loaded["branches"].get(stage_c_worker.REPOSITORY)))
    except Exception as error:
        add("guardian_policy", "FAIL", f"{type(error).__name__}")
        add("allowed_repository", "UNKNOWN")
        add("allowed_branch", "UNKNOWN")
        loaded = None

    try:
        profile_policy = project_profiles.load(profiles)
        executor = project_profiles.current_executor(profile_policy, stage_c_worker.REPOSITORY, "sez-company")
        supported = (executor is not None
                     and set(executor["allowed_paths"]) <= stage_c_worker.SAFE_FILES
                     and loaded is not None
                     and loaded["branches"].get(stage_c_worker.REPOSITORY) == executor["target_branch"])
        add("project_profiles", "PASS" if supported else "FAIL",
            "one supported writer; other registered repositories are read-only" if supported else
            "profile registry does not match the supported Stage C writer")
    except Exception as error:
        add("project_profiles", "FAIL", f"{type(error).__name__}")

    # Whose token the worker holds AND what it may do upstream. "Not the founder" alone is not enough: an agent
    # account later granted Write could merge (GitHub Free has no branch protection), so readiness also needs
    # GitHub's own answer that the token's upstream role is read-only. Scopes stay informational: a classic
    # `repo` scope is bounded by that role. Gated behind a valid policy: an invalid one makes no GitHub read.
    if loaded is None:
        add("worker_identity", "UNKNOWN", "not checked: guardian policy invalid, so no GitHub read is made")
    elif has_token:
        boundary = stage_c_credential_boundary.evaluate(env, run=run or subprocess.run,
                                                        repository=stage_c_worker.REPOSITORY)
        found = {c["name"]: c for c in boundary["checks"]}
        parts = [found["worker_token_not_founder"], found["worker_upstream_role"]]
        worst = next((s for s in ("FAIL", "UNKNOWN") if any(c["status"] == s for c in parts)), "PASS")
        add("worker_identity", worst,
            "; ".join(c["detail"] for c in parts) + f"; {found['worker_token_scopes']['detail']}")
    else:
        add("worker_identity", "UNKNOWN", "no GH_TOKEN: the worker's GitHub identity is not observable")

    can_read = loaded and has_token and have_bins and SHA.fullmatch(sha) and repos == [stage_c_worker.REPOSITORY]
    runner = run or subprocess.run
    root = f"repos/{stage_c_worker.REPOSITORY}"
    if loaded and status_contexts == ():
        add("github_commit_statuses", "PASS", "not required by configured CI policy; no commit-status read attempted")
    elif can_read:
        state, detail = _github_read(f"{root}/commits/{sha}/statuses?per_page=1",
                                     actions=False, env=env, run=runner)
        add("github_commit_statuses", state, detail + "; requires Commit statuses: Read-only on this repository")
    else:
        add("github_commit_statuses", "UNKNOWN", "read access not checked: prerequisite unavailable")
    if loaded and loaded["workflows"]:
        if can_read:
            state, detail = _github_read(f"{root}/actions/runs?per_page=1", actions=True, env=env, run=runner)
            add("github_actions", state, detail + "; requires Actions: Read-only on this repository")
        else:
            add("github_actions", "UNKNOWN", "read access not checked: prerequisite unavailable")

    return {"ready": all(c["status"] == "PASS" for c in checks), "code_write_flag": bool(flag),
            "source_sha": sha or None, "checks": checks}


def render(report: dict) -> str:
    lines = [f"STAGE C PREFLIGHT: {'READY' if report['ready'] else 'NOT READY'}   "
             f"code-write flag: {'ON' if report['code_write_flag'] else 'OFF'}   "
             f"source {(report['source_sha'] or 'unknown')[:7]}"]
    for c in report["checks"]:
        lines.append(f"  {c['status']:<7} {c['name']:<34} {c['detail']}".rstrip())
    return "\n".join(lines)


def main() -> int:
    from app import worker
    from app.db import SessionLocal

    host = None
    if not sys.stdin.isatty():
        try:
            host = json.loads(sys.stdin.read() or "null")
        except ValueError:
            host = None
    try:
        health = {n: p.health_check()[0].value for n, p in providers.default_providers().items()}
    except Exception:
        health = None
    with SessionLocal() as db:
        report = evaluate(db, host=host, health=health, jobs=stage_c_worker.jobs_root(),
                          flag=worker.ENABLE_AUTONOMOUS_CODE_WRITE)
    print(json.dumps(report, indent=2) if "--json" in sys.argv else render(report))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
