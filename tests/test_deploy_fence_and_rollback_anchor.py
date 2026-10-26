"""A deploy keeps the founder's Stage C pause fence and keeps its rollback images.

Regressions (2026-10-19/20): every deploy recreated company-api from the base compose file, dropping the
pause fence overlay until the founder re-applied it by hand; and Coolify's nightly cleanup deleted the
driver's rollback-<S> images (its coolify.managed label check never works), so a rollback the next day
had no images to go back to.
"""
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SOURCE = (ROOT / "scripts" / "deploy-company-os.sh").read_text()
REMOTE = SOURCE[SOURCE.index("<<'REMOTE'\n"):SOURCE.index("\nREMOTE\n")]
OVERLAY = ROOT / "ops" / "company-coder-pause.compose.yml"
OWNER = [0, 0]


def fn(name):
    start = REMOTE.index(f"\n{name}() {{") + 1
    return REMOTE[start:REMOTE.index("\n}\n", start) + 3]


def bash(script, **env):
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True, env=os.environ | env)


def pinned_sha():
    return next(line.split("=", 1)[1] for line in REMOTE.splitlines() if line.startswith("FENCE_SHA256="))


def test_the_pinned_fence_hash_is_the_reviewed_overlay():
    assert pinned_sha() == hashlib.sha256(OVERLAY.read_bytes()).hexdigest()


@pytest.fixture
def fence(tmp_path):
    d = tmp_path / "ops"
    d.mkdir(mode=0o755)
    f = d / "company-coder-pause.compose.yml"
    f.write_bytes(OVERLAY.read_bytes())
    f.chmod(0o644)
    st = d.stat()
    OWNER[:] = [st.st_uid, st.st_gid]          # the test's own owner stands in for root:root
    return f


def verified(f, sha=None):
    script = fn("fence_verified") + f'FENCE_OVERLAY="{f}"; FENCE_SHA256={sha or pinned_sha()}\n' \
        f'fence_verified {OWNER[0]} {OWNER[1]}'
    return bash(script).returncode == 0


def test_only_the_exact_owned_unwritable_overlay_is_verified(fence, tmp_path):
    assert verified(fence)
    assert not verified(fence, sha="0" * 64)                       # different bytes
    fence.chmod(0o664)
    assert not verified(fence)                                     # group-writable file
    fence.chmod(0o644)
    fence.parent.chmod(0o775)
    assert not verified(fence)                                     # group-writable directory
    fence.parent.chmod(0o755)
    link = tmp_path / "link.yml"
    link.symlink_to(fence)
    assert not verified(link)                                      # a symlink is never the fence
    assert not verified(tmp_path / "missing.yml")
    # Production checks root:root; the test's own uid would not pass that.
    assert bash(fn("fence_verified") + f'FENCE_OVERLAY="{fence}"; FENCE_SHA256={pinned_sha()}\nfence_verified').returncode != 0


def decide(overrides, ack, ok):
    block = REMOTE[REMOTE.index("FENCE_KEEP=no\n"):REMOTE.index('echo "compose overrides:')]
    script = f'FENCE_OVERLAY=/opt/company-os/ops/company-coder-pause.compose.yml\nOVERRIDES="{overrides}"\n' \
        f'ACK_OVERRIDES="{ack}"\nfence_verified() {{ return {0 if ok else 1}; }}\n{block}\n' \
        'printf "%s|%s\\n" "$FENCE_KEEP" "$OVERRIDES"'
    out = bash(script)
    assert out.returncode == 0, out.stderr
    keep, rest = out.stdout.strip().splitlines()[-1].split("|")
    return keep, rest.split()


F = "/opt/company-os/ops/company-coder-pause.compose.yml"


@pytest.mark.parametrize("overrides, ack, ok, keep, rest", [
    (f" company-api:{F}", "", True, "yes", []),                                      # kept, nothing to ACK
    (f" company-api:{F} company-worker:/x.yml", "", True, "yes", ["company-worker:/x.yml"]),   # others still need ACK
    (f" company-api:{F}", F, True, "no", [f"company-api:{F}"]),                      # founder drops it explicitly
    (f" company-api:{F}", "", False, "no", [f"company-api:{F}"]),                    # unverified: ordinary override
    (f" company-worker:{F}", "", True, "no", [f"company-worker:{F}"]),               # only company-api carries it
    ("", "", True, "no", []),
])
def test_the_verified_fence_is_kept_and_everything_else_keeps_the_ack_path(overrides, ack, ok, keep, rest):
    assert decide(overrides, ack, ok) == (keep, rest)


def compose_calls(tmp_path, keep, ok=True):
    calls = tmp_path / "calls"
    script = fn("up_services") + f'''FENCE_OVERLAY=/fence.yml; FENCE_KEEP={keep}
fence_verified() {{ return {0 if ok else 1}; }}
docker() {{ printf '%s\\n' "$*" >> "{calls}"; }}
up_services'''
    out = bash(script)
    return out.returncode, calls.read_text().splitlines() if calls.exists() else []


def test_up_services_reapplies_the_fence_on_company_api_only(tmp_path):
    rc, calls = compose_calls(tmp_path, "yes")
    assert rc == 0 and calls == [
        "compose -f docker-compose.yml -f /fence.yml up -d --no-deps --no-build company-api",
        "compose up -d --no-deps --no-build company-worker company-reasoner-egress company-reasoner",
    ]


def test_up_services_without_the_fence_is_unchanged(tmp_path):
    rc, calls = compose_calls(tmp_path, "no")
    assert rc == 0 and calls == [
        "compose up -d --no-deps --no-build company-api company-worker company-reasoner-egress company-reasoner"]


def test_a_fence_changed_mid_deploy_is_never_applied(tmp_path):
    rc, calls = compose_calls(tmp_path, "yes", ok=False)
    assert rc != 0 and calls == []


@pytest.mark.parametrize("keep, env, ok", [("yes", "true", True), ("yes", "", False), ("yes", "false", False),
                                           ("no", "", True)])
def test_fence_live_requires_the_running_api_to_carry_the_fence(keep, env, ok):
    script = fn("fence_live") + f'FENCE_KEEP={keep}\ndocker() {{ echo "{env}"; }}\nfence_live'
    assert (bash(script).returncode == 0) is ok


def test_the_switch_fails_into_rollback_when_the_fence_is_not_live():
    switch = REMOTE[REMOTE.index("trap rollback_release ERR"):REMOTE.index("trap - ERR\nphase VERIFIED")]
    assert "up_services" in switch and "docker compose up" not in switch
    assert switch.index("fence_live ||") < switch.index('"$GATE" "$SHA"')
    assert "FENCE_KEEP" in fn("read_env")                          # recovery knows to restore the fence too
    assert 'write_env FENCE_KEEP "$FENCE_KEEP"' in REMOTE


def test_a_rollback_without_a_live_fence_is_only_partial(tmp_path):
    backup = tmp_path / "b"
    backup.mkdir()
    for name in ("host-cli.sh", "release-health-gate.sh"):
        (backup / name).write_text("#!/bin/sh\nexit 0\n")
        (backup / name).chmod(0o755)
    (backup / "cli-files-before.sha256").write_text("x\n")
    script = "\n".join((fn("up_services"), fn("fence_live"), fn("rollback_core"))) + f'''
restore_source() {{ return 0; }}
restore_cron() {{ return 0; }}
fence_verified() {{ return 0; }}
docker() {{ case "$1" in inspect) echo healthy ;; exec) echo false ;; *) return 0 ;; esac; }}
B={backup}; S=s; OLD_SHA={"a" * 40}; API_IMG=a; WORKER_IMG=w; R_OLD=; E_OLD=; FENCE_OVERLAY=/f; FENCE_KEEP=yes
rollback_core; echo "$ROLLBACK_RESULT"'''
    out = bash(script)
    assert out.stdout.strip().splitlines()[-1] == "PARTIAL"


def test_rollback_images_are_anchored_under_pending_names_and_no_image_is_removed(tmp_path):
    calls = tmp_path / "calls"
    script = "ANCHOR=company-os-rollback-anchor\n" + fn("anchor_rollback") + \
        f'docker() {{ printf "%s\\n" "$*" >> "{calls}"; }}\nanchor_rollback api company-os-api:rollback-s'
    assert bash(script).returncode == 0
    assert calls.read_text().splitlines() == [
        "rm -f company-os-rollback-anchor-pending-api",
        "create --name company-os-rollback-anchor-pending-api --label coolify.managed=false --network none "
        "--pull=never company-os-api:rollback-s true",
    ]
    for name in ("anchor_rollback", "promote_rollback_anchors", "drop_pending_anchors"):
        body = fn(name)
        assert "rmi" not in body and "docker run" not in body and "docker start" not in body
    tags = REMOTE.index('docker tag "$E_OLD" "company-os-egress:rollback-$S"')
    assert REMOTE.index("anchor_rollback api", tags) < REMOTE.index("phase SOURCE_INSTALLING")


def anchors_after(tmp_path, steps, existing):
    """Run anchor helpers against a fake docker that keeps a set of container names."""
    state = tmp_path / "containers"
    state.write_text("\n".join(existing) + "\n")
    script = "ANCHOR=company-os-rollback-anchor\n" + "\n".join(
        fn(n) for n in ("anchor_rollback", "promote_rollback_anchors", "drop_pending_anchors")) + f'''
S="{state}"
docker() {{
  case "$1" in
    create) shift; echo "$2" >> "$S" ;;
    rm) shift 2; grep -vxF "$1" "$S" > "$S.n" || true; mv "$S.n" "$S" ;;
    inspect) grep -qxF "$2" "$S" ;;
    rename) grep -qxF "$2" "$S" || return 1; grep -vxF "$2" "$S" > "$S.n" || true; mv "$S.n" "$S"; echo "$3" >> "$S" ;;
  esac
}}
{steps}'''
    out = bash(script)
    assert out.returncode == 0, out.stderr
    return sorted(n for n in state.read_text().split() if n)


STABLE = ["company-os-rollback-anchor-api", "company-os-rollback-anchor-worker"]


def test_a_failed_attempt_keeps_the_last_good_releases_anchors(tmp_path):
    # Codex P2: anchors used to be replaced before the build, so an aborted deploy unprotected release A.
    left = anchors_after(tmp_path, "anchor_rollback api img-b; anchor_rollback worker img-b; drop_pending_anchors",
                         STABLE)
    assert left == STABLE


def test_only_a_verified_release_promotes_its_anchors(tmp_path):
    left = anchors_after(tmp_path, "anchor_rollback api img-b; anchor_rollback worker img-b; promote_rollback_anchors",
                         STABLE)
    assert left == STABLE                                          # same names, now the new set (renamed)
    assert not any("pending" in n for n in left)


def test_promotion_happens_after_verified_and_failures_drop_only_pending_anchors():
    end = REMOTE[REMOTE.index("trap - ERR\nphase VERIFIED"):]
    assert end.index("phase VERIFIED") < end.index("promote_rollback_anchors")
    assert "drop_pending_anchors" in fn("cleanup") and "--apply" in fn("cleanup")   # every exit; read-only runs untouched
    assert "promote_rollback_anchors" not in REMOTE[:REMOTE.index("trap - ERR\nphase VERIFIED")].split(
        "drop_pending_anchors() {")[1]


def fence_before(overrides, ack):
    block = REMOTE[REMOTE.index("FENCE_BEFORE=no\n"):REMOTE.index('echo "compose overrides:')]
    script = f'FENCE_OVERLAY={F}\nOVERRIDES="{overrides}"\nACK_OVERRIDES="{ack}"\nfence_verified() {{ return 0; }}\n' \
        f'{block}\nprintf "%s|%s\\n" "$FENCE_BEFORE" "$FENCE_KEEP"'
    return bash(script).stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("overrides, ack, expected", [
    (f" company-api:{F}", "", "yes|yes"),
    (f" company-api:{F}", F, "yes|no"),         # the ACK drops it forward; the old release still had it
    ("", "", "no|no"),
    (f" company-worker:{F}", "", "no|no"),
])
def test_the_pre_deploy_fence_state_is_recorded_apart_from_the_forward_decision(overrides, ack, expected):
    assert fence_before(overrides, ack) == expected
    assert 'write_env FENCE_BEFORE "$FENCE_BEFORE"' in REMOTE and "FENCE_BEFORE" in fn("read_env")


def rollback(tmp_path, before, keep, live):
    backup = tmp_path / "b"
    backup.mkdir()
    for name in ("host-cli.sh", "release-health-gate.sh"):
        (backup / name).write_text("#!/bin/sh\nexit 0\n")
        (backup / name).chmod(0o755)
    (backup / "cli-files-before.sha256").write_text("x\n")
    calls = tmp_path / "calls"
    script = "\n".join((fn("up_services"), fn("fence_live"), fn("rollback_core"))) + f'''
restore_source() {{ return 0; }}
restore_cron() {{ return 0; }}
fence_verified() {{ return 0; }}
docker() {{ printf '%s\\n' "$*" >> "{calls}"; case "$1" in inspect) echo healthy ;; exec) echo {live} ;; *) return 0 ;; esac; }}
B={backup}; S=s; OLD_SHA={"a" * 40}; API_IMG=a; WORKER_IMG=w; R_OLD=; E_OLD=; FENCE_OVERLAY=/f
FENCE_BEFORE={before}; FENCE_KEEP={keep}
rollback_core; echo "$ROLLBACK_RESULT"'''
    out = bash(script)
    fenced = any(c.startswith("compose -f docker-compose.yml -f /f up") for c in calls.read_text().splitlines())
    return out.stdout.strip().splitlines()[-1], fenced


def test_rollback_restores_the_fence_the_old_release_had_even_after_a_forward_ack(tmp_path):
    # Codex P2: an ACK that dropped the fence for the candidate also dropped it from the restored old release,
    # which still reported VERIFIED.
    assert rollback(tmp_path, before="yes", keep="no", live="true") == ("VERIFIED", True)


def test_rollback_that_cannot_restore_the_old_fence_is_only_partial(tmp_path):
    assert rollback(tmp_path, before="yes", keep="no", live="false") == ("PARTIAL", True)


def test_rollback_never_adds_a_fence_the_old_release_did_not_have(tmp_path):
    assert rollback(tmp_path, before="no", keep="yes", live="") == ("VERIFIED", False)


# --------------------------------------------------------------------------- independent review of #392 (N1-N3)

def cleanup_drops(tmp_path, phase, apply="--apply", journal=True, backup_set=True, anchored=True):
    calls = tmp_path / "calls"
    backup = tmp_path / "b"
    backup.mkdir(parents=True, exist_ok=True)
    if journal:
        (backup / "phase").write_text(phase + "\n")
    script = "\n".join(REMOTE[REMOTE.index(f"\n{n}() {{") + 1:REMOTE.index("\n", REMOTE.index(f"\n{n}() {{") + 1) + 1]
                       for n in ("phase_is_clean",)) + fn("cleanup") + f'''
drop_pending_anchors() {{ echo dropped >> "{calls}"; }}
REL=/nonexistent-rel-dir-for-test; {f"B={backup}" if backup_set else "unset B"}; APPLY={apply}; {"ANCHORED=1" if anchored else "unset ANCHORED"}
cleanup'''
    out = bash(script)
    assert out.returncode == 0, out.stderr
    return calls.exists()


@pytest.mark.parametrize("phase, dropped", [
    ("ROLLED_BACK_VERIFIED", True), ("ABORTED_RESTORED", True),                        # B runs again
    ("VERIFIED", False),          # promoted already; if promotion failed they are the previous release's only anchors
    ("ROLLBACK_FAILED", False), ("SWITCHING", False), ("MIGRATED", False),             # recovery still needs B's images
    ("ROLLED_BACK_PARTIAL", False),
])
def test_pending_anchors_survive_an_exit_a_recovery_still_needs(tmp_path, phase, dropped):
    # Review N1: cleanup dropped the pre-deploy release's only anchors on every exit, so after ROLLBACK_FAILED or an
    # interrupted switch Coolify's nightly rmi could delete the images RECOVER_DIR restores.
    assert cleanup_drops(tmp_path, phase) is dropped


def test_pending_anchors_are_dropped_before_any_journal_and_never_on_read_only_runs(tmp_path):
    assert cleanup_drops(tmp_path, "", journal=False) is True
    assert cleanup_drops(tmp_path / "ro", "VERIFIED", apply="") is False


def fence_before_unverified(ok):
    block = REMOTE[REMOTE.index("FENCE_BEFORE=no\n"):REMOTE.index('echo "compose overrides:')]
    script = f'FENCE_OVERLAY={F}\nOVERRIDES=" company-api:{F}"\nACK_OVERRIDES=""\n' \
        f'fence_verified() {{ return {0 if ok else 1}; }}\n{block}\nprintf "%s|%s\\n" "$FENCE_BEFORE" "$FENCE_KEEP"'
    return bash(script).stdout.strip().splitlines()[-1]


def test_an_unreviewed_overlay_before_the_deploy_is_recorded_as_unverified():
    assert fence_before_unverified(True) == "yes|yes"
    assert fence_before_unverified(False) == "unverified|no"


def rollback_unverified(tmp_path, before, ok):
    backup = tmp_path / "b"
    backup.mkdir()
    for name in ("host-cli.sh", "release-health-gate.sh"):
        (backup / name).write_text("#!/bin/sh\nexit 0\n")
        (backup / name).chmod(0o755)
    (backup / "cli-files-before.sha256").write_text("x\n")
    calls = tmp_path / "calls"
    script = "\n".join((fn("up_services"), fn("fence_live"), fn("rollback_core"))) + f'''
restore_source() {{ return 0; }}
restore_cron() {{ return 0; }}
fence_verified() {{ return {0 if ok else 1}; }}
docker() {{ printf '%s\\n' "$*" >> "{calls}"; case "$1" in inspect) echo healthy ;; exec) echo true ;; *) return 0 ;; esac; }}
B={backup}; S=s; OLD_SHA={"a" * 40}; API_IMG=a; WORKER_IMG=w; R_OLD=; E_OLD=; FENCE_OVERLAY=/f
FENCE_BEFORE={before}; FENCE_KEEP=no
rollback_core; echo "$ROLLBACK_RESULT"'''
    out = bash(script)
    composes = [c for c in calls.read_text().splitlines() if c.startswith("compose")]
    return out.stdout.strip().splitlines()[-1], composes


@pytest.mark.parametrize("before, ok", [("yes", False), ("unverified", True), ("unverified", False)])
def test_rollback_never_strands_the_candidate_when_the_old_fence_cannot_be_verified(tmp_path, before, ok):
    # Review N2: FENCE_KEEP=yes made up_services return before any compose call, so the failed candidate kept running.
    result, composes = rollback_unverified(tmp_path, before, ok)
    assert composes == ["compose up -d --no-deps --no-build company-api company-worker company-reasoner-egress "
                        "company-reasoner"]                        # old release recreated from the base file
    assert result == "PARTIAL"                                     # its old configuration is not back


def test_a_failed_promotion_leaves_every_component_anchored(tmp_path):
    # Review N3: the stable anchor was removed before the rename, so a failed rename left that component unanchored.
    state = tmp_path / "containers"
    state.write_text("\n".join(STABLE + ["company-os-rollback-anchor-pending-api"]) + "\n")
    script = "ANCHOR=company-os-rollback-anchor\n" + fn("promote_rollback_anchors") + f'''
S="{state}"
docker() {{
  case "$1" in
    rm) shift 2; grep -vxF "$1" "$S" > "$S.n" || true; mv "$S.n" "$S" ;;
    inspect) grep -qxF "$2" "$S" ;;
    rename) [ "$2" = company-os-rollback-anchor-pending-api ] && return 1
            grep -vxF "$2" "$S" > "$S.n" || true; mv "$S.n" "$S"; echo "$3" >> "$S" ;;
  esac
}}
promote_rollback_anchors; echo "rc=$?"'''
    out = bash(script)
    assert out.stdout.strip().splitlines()[-1] == "rc=1"
    left = sorted(n for n in state.read_text().split() if n)
    assert left == sorted(STABLE + ["company-os-rollback-anchor-pending-api"])   # stable api anchor restored


def test_a_refused_retry_never_drops_an_unfinished_deploys_anchors(tmp_path):
    # Independent review of #393: a retry refused before B is set ("a previous deploy did not finish cleanly", or a
    # precheck abort) anchored nothing of its own, so the pending anchors are the unfinished deploy's.
    assert cleanup_drops(tmp_path, "", journal=False, backup_set=False) is False


def test_an_interrupted_promotion_is_repaired_before_the_old_anchor_is_discarded(tmp_path):
    # Killed after stable -> old and before pending -> stable: the next promotion must not rm the only anchor.
    # The pending -> stable rename then fails: the component must still keep an anchor.
    fail_pending_rename = (
        'anchor_rollback api img-c\n'
        'eval "real_$(declare -f docker)"\n'
        'docker() { if [ "$1" = rename ] && [ "$2" = company-os-rollback-anchor-pending-api ]; then return 1; fi; '
        'real_docker "$@"; }\n'
        'promote_rollback_anchors || true')
    left = anchors_after(tmp_path, fail_pending_rename, ["company-os-rollback-anchor-old-api"])
    assert "company-os-rollback-anchor-api" in left


def test_an_abort_after_the_backup_dir_but_before_anchoring_keeps_earlier_anchors(tmp_path):
    # Review of #397: B is set before pg_dump/readback/cron snapshot; an abort there anchored nothing of its own.
    assert cleanup_drops(tmp_path, "", journal=False, anchored=False) is False


def test_a_partial_anchoring_failure_drops_every_pending_anchor(tmp_path):
    # Behaviour, not text: the worker anchor fails after api anchored; every pending anchor must be gone.
    start = REMOTE.index("ANCHORED=1\n")
    block = REMOTE[start:REMOTE.index("\n", REMOTE.index("could not anchor the rollback images"))]
    steps = ('eval "real_$(declare -f docker)"\n'
             'docker() { if [ "$1" = create ] && [ "$3" = company-os-rollback-anchor-pending-worker ]; then return 1; fi; '
             'real_docker "$@"; }\n'
             'R_OLD=; E_OLD=\n' + block)   # S stays the fake docker's state file; it only names image tags here
    left = anchors_after(tmp_path, steps, STABLE + ["company-os-rollback-anchor-pending-egress"])
    assert left == STABLE                                     # stable kept; no pending (api's or the stale egress)


def test_anchored_is_set_after_every_abort_that_follows_the_backup_dir():
    assert REMOTE.index("ANCHORED=1") > REMOTE.index("running API has no full source SHA")
    assert REMOTE.index("ANCHORED=1") < REMOTE.index("anchor_rollback api")
    assert REMOTE.startswith("<<'REMOTE'\nset -euo pipefail\nANCHORED=")   # never inherited from the environment
