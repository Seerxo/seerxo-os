#!/usr/bin/env python3
"""Host-side facts for `companyctl stage-c preflight`. Collects only; app/stage_c_preflight.py judges.

Prints one JSON object of booleans, deployed SHAs and command fingerprints, never a secret
or a file's content. A fact the host could not observe is null (UNKNOWN), never false-by-default.
Run as root, like company-coder-launch:  sudo python3 scripts/stage-c-host-facts.py
"""
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

CONFIG = Path("/etc/company-coder/launcher.json")
CLAUDE_ENV = Path("/etc/company-coder/claude.env")
SHA = re.compile(r"[a-f0-9]{40}")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}")
METERED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")
# The launcher's sandbox contract (scripts/company-coder-launch load_config/verify_network). Duplicated, not
# imported, because the two scripts are installed separately; tests/test_stage_c_preflight.py pins them equal.
CODER_NETWORK = "company-coder-net"
CODER_PROXY = "http://company-egress:8888"
CHECKS_COMMAND = ["python", "-m", "pytest", "-q"]
CONFIG_KEYS = {"coder_image", "checks_images", "checks_commands", "network", "proxy"}
CODEX_MOUNT = '{{range .Mounts}}{{if eq .Destination "/home/worker/.codex"}}{{.Name}}{{end}}{{end}}'


def run(argv):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return done.returncode, done.stdout


def _exists(rc):
    return None if rc == 127 else rc == 0


def sha_of(runner, container):
    rc, out = runner(["docker", "exec", container, "printenv", "COMPANY_OS_GIT_SHA"])
    return out.strip() if rc == 0 and SHA.fullmatch(out.strip()) else None


def codex_reviewer_volume_mounted(runner):
    """Check the running reasoner mount, not the unused Stage C coder volume."""
    rc, out = runner(["docker", "inspect", "-f", CODEX_MOUNT, "company-reasoner"])
    if rc == 127:
        return None
    return rc == 0 and out.strip() == "company-worker-codex"


def claude_auth_ok(runner, config, claude_env):
    """Run Claude's zero-model auth status in the same hardened coder shape as production."""
    try:
        image = json.loads(Path(config).read_text())["coder_image"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    rc, out = runner(["docker", "run", "--rm", "--pull=never", "--read-only", "--network", "company-coder-net",
                      "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                      "--tmpfs", "/tmp:size=32m", "--env", "HOME=/tmp", "--env-file", str(claude_env), image,
                      "claude", "auth", "status", "--json"])
    if rc == 127:
        return None
    try:
        return rc == 0 and json.loads(out).get("loggedIn") is True
    except (ValueError, TypeError):
        return False


def claude_env_ok(path, owner):
    """Exactly one CLAUDE_CODE_OAUTH_TOKEN line, owned by `owner`, mode 0600. Reports a bool only."""
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != owner or stat.S_IMODE(st.st_mode) != 0o600:
            return False
        lines = [l for l in path.read_text().splitlines() if l.strip()]
    except OSError:
        return False
    names = [l.split("=", 1)[0] for l in lines]
    return len(lines) == 1 and names[0] == "CLAUDE_CODE_OAUTH_TOKEN" and not set(names) & set(METERED)


def contract_ok(cfg) -> bool:
    """Exactly what the launcher accepts; anything else means every launch would be refused."""
    return (isinstance(cfg, dict) and set(cfg) == CONFIG_KEYS and cfg["network"] == CODER_NETWORK
            and cfg["proxy"] == CODER_PROXY and isinstance(cfg["checks_images"], dict)
            and isinstance(cfg["checks_commands"], dict) and set(cfg["checks_commands"]) == set(cfg["checks_images"])
            and all(c == CHECKS_COMMAND for c in cfg["checks_commands"].values()))


def network_internal(runner):
    """True only if docker reports the coder network as internal; None when docker cannot be asked."""
    rc, out = runner(["docker", "network", "inspect", "--format", "{{json .}}", CODER_NETWORK])
    if rc == 127:
        return None
    try:
        return rc == 0 and json.loads(out).get("Internal") is True
    except (ValueError, AttributeError):
        return False


def collect(runner=run, config=CONFIG, claude_env=CLAUDE_ENV, owner=0):
    try:
        cfg = json.loads(Path(config).read_text())
        images = {"coder": cfg["coder_image"], "checks": cfg["checks_images"]["sez-company"]}
        config_ok = all(isinstance(i, str) and IMAGE.fullmatch(i) for i in images.values())
        commands = cfg.get("checks_commands")
        command = commands.get("sez-company") if isinstance(commands, dict) else None
        fingerprints = {"sez-company": hashlib.sha256(
            json.dumps(command, separators=(",", ":")).encode()).hexdigest()
        } if (isinstance(command, list) and command
              and all(isinstance(arg, str) and arg and "\0" not in arg for arg in command)) else None
    except (OSError, ValueError, KeyError, TypeError):
        images, config_ok, fingerprints = {}, False, None
    contract = config_ok and contract_ok(cfg if config_ok else None)
    internal = network_internal(runner) if contract else None
    launcher_config_ok = False if not contract else internal  # None (unknown) when docker cannot be asked

    def image(name):
        return _exists(runner(["docker", "image", "inspect", images[name]])[0]) if config_ok else None

    rc, running = runner(["docker", "inspect", "-f", "{{.State.Running}}", "company-egress"])
    rc_active, active = runner(["systemctl", "is-active", "company-coder-launch.timer"])
    return {
        "api_sha": sha_of(runner, "company-api"), "worker_sha": sha_of(runner, "company-worker"),
        "launcher_path_active": None if rc_active == 127 else active.strip() == "active",
        "launcher_config_ok": launcher_config_ok,
        "checks_command_fingerprints": fingerprints if config_ok else None,
        "coder_image_present": image("coder"), "checks_image_present": image("checks"),
        "egress_running": None if rc == 127 else rc == 0 and running.strip() == "true",
        "claude_env_ok": claude_env_ok(Path(claude_env), owner),
        "claude_auth_ok": claude_auth_ok(runner, config, claude_env),
        "codex_reviewer_volume_mounted": codex_reviewer_volume_mounted(runner),
    }


if __name__ == "__main__":
    print(json.dumps(collect()))
    sys.exit(0)
