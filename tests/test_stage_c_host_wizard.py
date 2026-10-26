"""The host wizard is only checked by hand on the host, so pin its structure here.

Regression: an edit once swallowed the "Offline checks job" stage. The prompt never appeared, so
`launcher` and `offline_checks` were never proven and two real acceptance runs recorded nothing."""
import re
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "stage-c-host-wizard.sh").read_text()
COMPOSE = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
PREP, ACCEPT = SCRIPT.split("# ═══════════════════════════════ ACCEPT", 1)


def total(mode):
    return int(re.search(rf"{mode}\) TOTAL_STAGES=(\d+)", SCRIPT).group(1))


def stages(text):
    return re.findall(r'^stage "', text, re.M)


def test_announced_stage_counts_match_the_stages_that_exist():
    assert len(stages(PREP.split("MODE=", 1)[1])) == total("prep")
    assert len(stages(ACCEPT)) == total("accept")


def test_every_acceptance_check_has_a_setter_and_is_required_by_the_attestation():
    need = re.search(r"^NEED=\((.*?)\)", ACCEPT, re.M | re.S).group(1).split()
    from app.stage_c_worker import ACCEPTANCE_CHECKS
    assert set(need) == ACCEPTANCE_CHECKS
    for key in need:
        assert re.search(rf"OK\[{key}\]=", ACCEPT), f"nothing in the wizard can prove {key}"


def test_the_offline_checks_stage_runs_without_a_prompt():
    stage = ACCEPT.split('stage "Offline checks job"', 1)[1].split("\nstage ", 1)[0]
    assert "confirm" not in stage and '"kind":"checks"' in stage and "OK[offline_checks]=true" in stage


def test_the_wizard_never_asks_for_danger_full_access_or_a_looser_container():
    for loosened in ("danger-full-access", "seccomp=unconfined", "apparmor=unconfined", "--cap-add SYS_ADMIN"):
        assert loosened not in SCRIPT


def test_worker_token_setup_includes_the_permission_needed_for_circleci_results():
    stage = PREP.split('stage "Scoped GitHub token', 1)[1].split("\nstage ", 1)[0]
    assert "Commit statuses READ" in stage


def test_codex_login_uses_the_persistent_reviewer_volume_not_a_coder_volume():
    stage = PREP.split('stage "Codex independent reviewer login', 1)[1].split("\nstage ", 1)[0]
    assert "company-reasoner" in stage and "company-worker-codex" in stage
    reasoner = COMPOSE.split("  company-reasoner:", 1)[1].split("\n  company-", 1)[0]
    assert "company-worker-codex:/home/worker/.codex" in reasoner
    assert "company-coder-codex:/home/coder/.codex" not in reasoner
    assert "codex login --device-auth" in stage
    assert "company-coder-codex" not in stage and "volume create" not in stage and "volume rm" not in stage
    assert "device code login is not enabled" in stage.lower()


def test_provider_probe_is_explicit_codex_only_and_does_not_echo_provider_errors():
    stage = ACCEPT.split('stage "Real Claude edit through the launcher, and the reviewer probe"', 1)[1].split(
        '\nstage "Offline checks job"', 1)[0]
    assert 'confirm "Run one explicit Codex reviewer model probe now' in stage
    assert "--probe-provider codex" in stage
    assert 'say "probe: ${PROBE' not in stage and 'ev "probe=' not in stage
    assert "$PROBE" not in "\n".join(line for line in stage.splitlines() if "ev " in line)


def test_provider_credentials_and_device_codes_never_enter_evidence_logs():
    evidence = "\n".join(line for line in SCRIPT.splitlines() if re.search(r"\bev\s+", line))
    for secret_value in ("$PROBE", "${PROBE", "$CLAUDE_CODE_OAUTH_TOKEN_IN", "auth.json"):
        assert secret_value not in evidence
    login = PREP.split('stage "Codex independent reviewer login', 1)[1].split("\nstage ", 1)[0]
    assert "ev \"Codex reviewer device login completed (no auth output logged)\"" in login
    assert "ev \"Codex reviewer device login failed rc=$rc (provider output not logged)\"" in login


def test_acceptance_credential_check_expects_codex_coder_refusal():
    assert 'Codex code profile | refused as expected (reviewer-only)' in ACCEPT
    assert 'bad.append("Codex code profile was unexpectedly accepted")' in ACCEPT


def test_claude_setup_token_explains_team_entitlement_separately_from_expiry():
    stage = PREP.split('stage "Claude Code subscription token', 1)[1].split('\nstage "Codex reviewer login', 1)[0]
    assert "claude setup-token" in stage
    assert "Team" in stage and "token expiry" in stage.lower()
    assert "API key" in stage and "metered" in stage.lower()


def test_the_credential_profile_check_allows_exactly_the_envs_the_launcher_sets(tmp_path):
    """Stage 6 asserts only allowed env reach the container. It must know every env the launcher's
    profiles set (a new one, like TMPDIR for the checks profile, would otherwise fail acceptance)."""
    import importlib.machinery, importlib.util
    path = Path(__file__).resolve().parents[1] / "scripts" / "company-coder-launch"
    loader = importlib.machinery.SourceFileLoader("launch_for_wizard_test", str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    mod.CLAUDE_ENV = tmp_path / "claude.env"
    mod.CLAUDE_ENV.write_text("CLAUDE_CODE_OAUTH_TOKEN=fixture-only\n")
    cfg = {"coder_image": "sha256:" + "0" * 64, "checks_images": {"sez-company": "sha256:" + "1" * 64},
           "checks_commands": {"sez-company": ["python", "-m", "pytest", "-q"]}, "network": "n",
           "proxy": "http://p:8888"}
    allowed = re.search(r"allowed = \((.*?)\)\n", ACCEPT, re.S).group(1)
    allowed = re.findall(r'"([A-Za-z_]+=)"', allowed)
    for req in ({"kind": "code", "provider": "claude", "timeout_s": 1},
                {"kind": "checks", "repo": "sez-company", "timeout_s": 1}):
        for arg in mod.docker_argv(req, cfg, Path("/x"), "n"):
            if arg.startswith("--env="):
                assert arg[6:].startswith(tuple(allowed)), f"{arg} is not in the wizard's allowed list {allowed}"


def test_the_coder_image_is_labelled_so_coolifys_nightly_cleanup_keeps_it():
    # Regression: Coolify (0 0 * * *) removed the unused coder image every night; preflight lost coder_image.
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    coder = dockerfile.split("AS coder", 1)[1].split("\nFROM ", 1)[0]
    assert "LABEL coolify.managed=true" in coder
    reasoner = dockerfile.split("AS reasoner", 1)[1].split("\nFROM ", 1)[0]
    assert "@openai/codex" not in coder and "@openai/codex" in reasoner


def test_pinned_images_are_anchored_against_coolify_cleanup():
    """Regression: Coolify's nightly cleanup deleted the pinned coder image twice (its label check never works),
    and the next accept died at 'No such image'. A stopped anchor container blocks that rmi."""
    body = SCRIPT.split("anchor() {", 1)[1].split("\n}", 1)[0]
    assert "--label coolify.managed=false" in body and "--network none" in body and "docker create" in body
    assert "docker run" not in body and "docker start" not in body          # never started
    pin = ACCEPT.split('stage "Pin the images and install the launcher"', 1)[1].split("\nstage ", 1)[0]
    assert 'anchor company-coder-anchor "$CODER_ID"' in pin and 'anchor company-checks-anchor "$CHECKS_ID"' in pin
    assert "anchor company-coder-anchor" in PREP
