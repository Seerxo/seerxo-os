"""The four protected services can start only the exact bound image IDs, proven with the real Compose binary.

The driver's own functions (compose_env -> compose_check -> compose_bound -> run_compose, taken verbatim from the
REMOTE block) render this repository's docker-compose.yml with real `docker compose config`, under hostile inputs.
The assertion is on the RESOLVED image of each service, not on an exit code. Only `compose config` reaches the real
binary; every other Compose call is recorded and never executed, so nothing can start here.

Limit, by design: Compose interpolation (`${VAR:?}`) checks presence, not format, so a raw `docker compose`
run outside the driver takes any reference it is given. That path needs root on the host; after bootstrap the host
verifier compares the running image IDs with the ledger's bound IDs.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SOURCE = (ROOT / "scripts" / "deploy-company-os.sh").read_text()
REMOTE = SOURCE[SOURCE.index("<<'REMOTE'\n"):SOURCE.index("\nREMOTE\n")]
SERVICES = ("company-api", "company-worker", "company-reasoner", "company-reasoner-egress")
PINS = {"api": "sha256:" + "a" * 64, "worker": "sha256:" + "b" * 64,
        "reasoner": "sha256:" + "c" * 64, "egress": "sha256:" + "d" * 64}
WANT = dict(zip(SERVICES, (PINS["api"], PINS["worker"], PINS["reasoner"], PINS["egress"])))
# Non-secret stand-ins for the two unrelated required variables; never for an image ID.
DUMMIES = {"POSTGRES_PASSWORD": "test-dummy", "CLOUDFLARE_TUNNEL_TOKEN": "test-dummy"}


def compose_binary():
    if shutil.which("docker") and subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0:
        return "docker compose"
    if shutil.which("docker-compose"):
        return "docker-compose"
    return None


COMPOSE = compose_binary()
if COMPOSE is None and os.environ.get("GITHUB_ACTIONS") == "true":
    raise RuntimeError("real Compose is required in CI: this proof must not silently become a skip")
pytestmark = pytest.mark.skipif(COMPOSE is None, reason="no real Compose binary on this machine")


def fn(name):
    start = REMOTE.index(f"\n{name}() {{") + 1
    return REMOTE[start:REMOTE.index("\n}\n", start) + 3]


def run(tmp_path, body, env=None):
    """Run `body` with the driver's compose functions in a copy of the project directory. Returns (rc, calls)."""
    (tmp_path / "docker-compose.yml").write_text((ROOT / "docker-compose.yml").read_text())
    calls = tmp_path / "calls"
    shim = f'''docker() {{
  [ "$1" = compose ] || return 0
  shift
  case " $* " in *" config "*) {COMPOSE} "$@" ;; *) printf '%s\\n' "$*" >> "{calls}" ;; esac
}}'''
    funcs = "\n".join(fn(n) for n in ("compose_env", "compose_check", "compose_bound", "run_compose"))
    out = subprocess.run(["bash", "-c", f"set -u\n{shim}\n{funcs}\n{body}"], cwd=tmp_path, text=True,
                         capture_output=True, env=os.environ | DUMMIES | (env or {}))
    return out, (calls.read_text().splitlines() if calls.exists() else [])


def bound_env(**over):
    env = {"BOOTSTRAP": "yes", "BOUND_API": PINS["api"], "BOUND_WORKER": PINS["worker"],
           "BOUND_REASONER": PINS["reasoner"], "BOUND_EGRESS": PINS["egress"]}
    return env | over


def resolved(tmp_path, body, env):
    out, _ = run(tmp_path, body, env)
    assert out.returncode == 0, out.stderr
    services = json.loads(out.stdout)["services"]
    return {s: services[s]["image"] for s in SERVICES}


def test_a_hostile_sha_and_tag_cannot_select_a_mutable_image(tmp_path):
    env = bound_env(SHA="latest", COMPANY_OS_IMAGE_TAG="latest")
    assert resolved(tmp_path, "run_compose config --format json", env) == WANT


def test_a_poisoned_env_file_cannot_override_the_bound_ids(tmp_path):
    poison = "".join(f"COMPANY_OS_IMAGE_{v}=company-os-{v.lower()}:latest\n" for v in ("API", "WORKER", "REASONER", "EGRESS"))
    (tmp_path / ".env").write_text(poison + "COMPANY_OS_IMAGE_TAG=latest\n")    # read by Compose from the project dir
    body = "set -a; . ./.env; set +a\nrun_compose config --format json"         # and sourced, as the driver does
    assert resolved(tmp_path, body, bound_env()) == WANT


@pytest.mark.parametrize("missing", ["BOUND_API", "BOUND_WORKER", "BOUND_REASONER", "BOUND_EGRESS"])
def test_a_missing_identity_fails_closed_and_starts_nothing(tmp_path, missing):
    out, calls = run(tmp_path, "run_compose up -d --no-deps --no-build --pull never " + " ".join(SERVICES),
                     bound_env(**{missing: ""}))
    assert out.returncode != 0 and calls == []


def test_plain_compose_without_the_bound_ids_refuses(tmp_path):
    out, _ = run(tmp_path, "unset COMPANY_OS_IMAGE_API COMPANY_OS_IMAGE_WORKER COMPANY_OS_IMAGE_REASONER "
                           "COMPANY_OS_IMAGE_EGRESS\ndocker compose config -q", {"COMPANY_OS_IMAGE_TAG": "latest"})
    assert out.returncode != 0 and "bound image ID required" in out.stderr


def test_a_compose_file_that_swaps_in_a_mutable_image_is_refused_before_up(tmp_path):
    (tmp_path / "mutable.yml").write_text("services:\n  company-api:\n    image: company-os-api:latest\n")
    out, calls = run(tmp_path, "run_compose -f docker-compose.yml -f mutable.yml up -d --no-deps company-api",
                     bound_env())
    assert out.returncode != 0 and calls == []                          # the check rendered the same files: no `up`
    assert "resolved company-api image is 'company-os-api:latest'" in out.stderr


def test_an_overlay_that_keeps_the_bound_ids_still_starts(tmp_path):
    # The pause-fence overlay (environment and a read-only bind, no image) is checked with the base file, then used.
    (tmp_path / "fence.yml").write_text("services:\n  company-api:\n    environment:\n      CODER_PAUSE_FENCE_ENABLED: \"true\"\n")
    out, calls = run(tmp_path, "run_compose -f docker-compose.yml -f fence.yml up -d --no-deps company-api", bound_env())
    assert out.returncode == 0, out.stderr
    assert calls == ["-f docker-compose.yml -f fence.yml up -d --no-deps company-api"]


@pytest.mark.parametrize("late", ["-f mutable.yml", "--file mutable.yml", "--file=mutable.yml"])
def test_a_compose_file_given_after_the_command_is_refused(tmp_path, late):
    (tmp_path / "mutable.yml").write_text("services:\n  company-api:\n    image: company-os-api:latest\n")
    out, calls = run(tmp_path, f"run_compose up -d {late} company-api", bound_env())
    assert out.returncode != 0 and calls == [] and "compose files must lead the call" in out.stderr
