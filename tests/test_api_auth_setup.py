"""scripts/api-auth-setup.sh, run as shipped against stand-in .env files and a stub docker."""
import os
import re
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "api-auth-setup.sh"


def _run(tmp_path, *args):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text("#!/usr/bin/env bash\necho '10.0.6.1 '\n")
    (bin_dir / "docker").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CO": str(tmp_path / "co.env"),
           "YT": str(tmp_path / "yt.env"), "BK": str(tmp_path)}
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)


def _env(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def test_read_only_by_default_and_apply_adds_tokens_without_printing_them(tmp_path):
    co, yt = tmp_path / "co.env", tmp_path / "yt.env"
    co.write_text("POSTGRES_PASSWORD=keep\nAPI_AUTH_MODE=log")      # no final newline
    yt.write_text("YOUTUBE_X=1\n")
    yt.chmod(0o644)  # youtube-auto's container reads it as non-root
    assert _run(tmp_path).returncode == 0 and co.read_text().endswith("API_AUTH_MODE=log")
    out = _run(tmp_path, "--apply")
    assert out.returncode == 0, out.stdout + out.stderr
    c, y = _env(co), _env(yt)
    # Break caught: the appended line is glued onto "API_AUTH_MODE=log".
    assert c["API_AUTH_MODE"] == "log" and c["POSTGRES_PASSWORD"] == "keep"
    for n in ("ADMIN", "DESK", "YOUTUBE"):
        assert re.fullmatch(r"[0-9a-f]{64}", c[f"COMPANY_API_TOKEN_{n}"])
        # Break caught: a token lands in the terminal (and so in a chat transcript or log).
        assert c[f"COMPANY_API_TOKEN_{n}"] not in out.stdout + out.stderr
    assert c["API_HOST_IPS"] == "10.0.6.1" and y["COMPANY_OS_TOKEN"] == c["COMPANY_API_TOKEN_YOUTUBE"]
    assert len({c["COMPANY_API_TOKEN_ADMIN"], c["COMPANY_API_TOKEN_DESK"], c["COMPANY_API_TOKEN_YOUTUBE"]}) == 3
    assert stat.S_IMODE(yt.stat().st_mode) == 0o644 and len(list(tmp_path.glob("env-*"))) == 2


def test_apply_is_idempotent_and_never_replaces_a_value(tmp_path):
    co, yt = tmp_path / "co.env", tmp_path / "yt.env"
    co.write_text("COMPANY_API_TOKEN_DESK=" + "d" * 64 + "\n")
    yt.write_text("")
    _run(tmp_path, "--apply")
    first = co.read_text()
    assert _env(co)["COMPANY_API_TOKEN_DESK"] == "d" * 64
    # Break caught: unset now means enforce, so the next deploy would refuse callers mid-rollout.
    assert _env(co)["API_AUTH_MODE"] == "log"
    _run(tmp_path, "--apply")
    assert co.read_text() == first and yt.read_text().count("COMPANY_OS_TOKEN=") == 1
