"""Stage C credential boundary probe: what can the worker's GitHub token prove about itself? Zero model calls.

Run it as the worker (same environment as app.stage_c_worker):  python -m app.stage_c_credential_boundary

Only facts that code can observe are PASS/FAIL. The token's effective role on the upstream repository is
observable: GitHub answers `permissions` for the authenticated caller. Merging needs push (write) or higher, so
a verified role without push/maintain/admin proves the token cannot merge upstream; anything less stays UNKNOWN
and needs the founder's GitHub-side attestation (docs/STAGE_C_FOUNDER_AUTHORIZATION.md). UNKNOWN never counts
as a pass. The worker's own fork is not checked here: writing there is its job.
"""
import json
import os
import re
import subprocess
import sys

FOUNDER = "semihbugrasezer"
UPSTREAM = "seerxo/seerxo-os"
# Roles GitHub reports for the caller; any of the write-or-higher ones can push or merge upstream.
WRITE_ROLES = ("admin", "maintain", "push")
ROLES = (*WRITE_ROLES, "pull")
# Pinned so GH_HOST cannot point the probe at another host while git still sends GH_TOKEN to github.com.
HOST = ["--hostname", "github.com"]
# A classic token with any of these can merge, rewrite workflows or administer the repository.
BROAD_SCOPES = {"repo", "workflow", "admin:org", "admin:repo_hook", "delete_repo", "write:packages"}


def _whoami(env: dict, run) -> tuple[str | None, set[str] | None]:
    """(login, classic scopes) from one `gh api -i user`; (None, None) when it cannot be read."""
    try:
        out = run(["gh", "api", *HOST, "-i", "user"], env=env, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None, None
    if out.returncode:
        return None, None
    head, _, body = out.stdout.replace("\r\n", "\n").partition("\n\n")
    login = re.search(r'"login"\s*:\s*"([^"]+)"', body)
    scopes = re.search(r"(?im)^x-oauth-scopes:[ \t]*(.*)$", head)
    return (login[1] if login else None,
            None if scopes is None else {s.strip() for s in scopes[1].split(",") if s.strip()})


def _upstream_role(env: dict, run, repository: str) -> tuple[str, str]:
    """(status, detail) for the token's effective role on `repository`. Never echoes the response body."""
    try:
        out = run(["gh", "api", *HOST, f"repos/{repository}"], env=env, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN", f"could not read the token's role on {repository}"
    if out.returncode:
        return "UNKNOWN", f"GitHub refused or failed the role read on {repository} (401/403/404/network)"
    try:
        answer = json.loads(out.stdout)
        name, perms = answer.get("full_name"), answer.get("permissions")
    except (ValueError, AttributeError):
        return "FAIL", f"malformed GitHub answer for {repository}"
    # A rename redirect or a wrong answer would otherwise prove the role on some other repository.
    if not isinstance(name, str) or name.lower() != repository.lower():
        return "FAIL", f"GitHub answered for a different repository than {repository}"
    if perms is None:
        return "UNKNOWN", f"GitHub returned no permissions object for {repository}"
    if not isinstance(perms, dict) or any(type(perms.get(r)) is not bool for r in ROLES):
        return "FAIL", f"malformed permissions for {repository}: admin/maintain/push/pull must all be booleans"
    held = [r for r in WRITE_ROLES if perms[r]]
    if held:
        return "FAIL", f"the token can write {repository} ({', '.join(held)}): it could push or merge upstream"
    if not perms["pull"]:
        return "FAIL", f"the token cannot read {repository}"
    return "PASS", f"the token is read-only on {repository} (admin/maintain/push false)"


def upstream_role(env: dict | None = None, run=subprocess.run, repository: str = UPSTREAM) -> tuple[str, str]:
    """The upstream-role check alone, for the moment of use (the worker calls it right before a push)."""
    env = os.environ if env is None else env
    if not env.get("GH_TOKEN"):
        return "UNKNOWN", "no token: the upstream role is not observable"
    return _upstream_role(env, run, repository)


def evaluate(env: dict | None = None, run=subprocess.run, founder: str = FOUNDER,
             repository: str = UPSTREAM) -> dict:
    env = os.environ if env is None else env
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    if not env.get("GH_TOKEN"):
        add("worker_token_present", "FAIL", "GH_TOKEN is not set in the worker")
        login = scopes = None
        role = ("UNKNOWN", "no token: the upstream role is not observable")
    else:
        add("worker_token_present", "PASS", "GH_TOKEN present (value never shown)")
        login, scopes = _whoami(env, run)
        role = _upstream_role(env, run, repository)

    if login is None:
        add("worker_token_not_founder", "UNKNOWN", "could not read the token's GitHub identity")
    elif login.lower() == founder.lower():
        add("worker_token_not_founder", "FAIL",
            f"the worker authenticates as {login}: it holds the founder's merge authority")
    else:
        add("worker_token_not_founder", "PASS", f"worker identity is {login}, not the founder")

    if scopes is None:
        add("worker_token_scopes", "UNKNOWN",
            "fine-grained or unreadable token: permissions are not observable; founder must attest them")
    elif scopes & BROAD_SCOPES:
        add("worker_token_scopes", "FAIL", "classic token with broad scopes: " + ", ".join(sorted(scopes & BROAD_SCOPES)))
    else:
        add("worker_token_scopes", "UNKNOWN", "classic token without broad scopes; effective repository rights unproven")

    # Scopes are an upper bound only: a classic `repo` token is still bounded by the account's role, which is
    # what GitHub enforces. The role is the deciding fact.
    add("worker_upstream_role", *role)
    if role[0] == "PASS":
        add("worker_token_cannot_merge", "PASS", "merging needs push or higher; " + role[1])
    else:
        add("worker_token_cannot_merge", "UNKNOWN",
            "upstream role not verified read-only, so push + PR rights may reach the merge API")
    return {"separated": all(c["status"] == "PASS" for c in checks), "checks": checks}


def main() -> int:
    report = evaluate()
    print("STAGE C CREDENTIAL BOUNDARY: " + ("SEPARATED" if report["separated"] else "NOT PROVEN"))
    for c in report["checks"]:
        print(f"  {c['status']:<7} {c['name']:<28} {c['detail']}")
    return 0 if report["separated"] else 1


if __name__ == "__main__":
    sys.exit(main())
