"""The concrete cutover installer preserves the ledger/old code and stages a non-operational bootstrap bundle."""
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_bundle", ROOT / "scripts/install-release-bundle.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def installation(monkeypatch):
    with tempfile.TemporaryDirectory(prefix=".bundle-test-", dir=ROOT) as directory:
        root = Path(directory)
        clone = root / "clone"
        clone.mkdir()
        subprocess.run(["git", "init", "-q", str(clone)], check=True)
        for name in set(installer.HOST_FILES) | set(installer.GATEWAY_FILES):
            target = clone / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
        subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
        subprocess.run(["git", "-C", str(clone), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "-c", "commit.gpgsign=false", "commit", "-qm", "disposable bundle fixture"], check=True)
        sha = subprocess.check_output(["git", "-C", str(clone), "rev-parse", "HEAD"], text=True).strip()
        ledger = root / "state/ledger/ledger.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({"last_sha": sha}))
        monkeypatch.setattr(installer, "LEDGER", ledger)
        monkeypatch.setattr(installer, "HOSTLIB", root / "host")
        monkeypatch.setattr(installer, "GATEWAYLIB", root / "gateway")
        for library in (installer.HOSTLIB, installer.GATEWAYLIB):
            (library / "app").mkdir(parents=True)
            (library / "app/old.py").write_text("previous verifier\n")
        yield root, sha


def test_staging_checks_every_member_and_cannot_operate_the_current_host(installation):
    root, sha = installation
    bundle = installer.stage_bundle(root / "clone", sha, root / "next")
    assert set(json.loads((bundle / "BUNDLE.json").read_bytes())["files"]) == set(installer.HOST_FILES)
    check = subprocess.run([sys.executable, "-I", "-c", "import sys;sys.path.insert(0,sys.argv[1]);"
                            "from app import release_host as h;"
                            "assert h.bundle_problem(allow_staged=True) is None;"
                            "assert 'staged code cannot operate' in h.bundle_problem()", str(bundle)])
    assert check.returncode == 0
    assert not (installer.HOSTLIB / "current").exists()


def controls(calls):
    def run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["systemctl", "is-active", "--quiet"]:
            return subprocess.CompletedProcess(args, 0 if args[-1] == "release-host.timer" else 3)
        if args[0] == "pgrep":
            return subprocess.CompletedProcess(args, 1)
        if args[0] == "runuser" and args[-2] == "-w":
            return subprocess.CompletedProcess(args, 1)
        return subprocess.CompletedProcess(args, 0)
    return run


def test_install_switches_complete_bundles_and_preserves_old_flat_files_and_ledger(installation):
    root, sha = installation
    raw = installer.LEDGER.read_bytes()
    calls = []
    installer.install(sha, root / "clone", run=controls(calls))
    for library in (installer.HOSTLIB, installer.GATEWAYLIB):
        assert (library / "current/BUNDLE.json").is_file()
        assert (library / "app").is_symlink()
        assert next(library.glob("app.pre-bundle-*/old.py")).read_text() == "previous verifier\n"
    assert installer.LEDGER.read_bytes() == raw
    assert ["systemctl", "start", "release-host.timer"] in calls
    assert ["systemctl", "start", "release-gateway.timer"] not in calls


def test_pending_or_wrong_sha_refuses_install_and_leaves_timers_stopped(installation):
    root, sha = installation
    installer.LEDGER.write_text(json.dumps({"last_sha": sha, "pending": "a" * 64}))
    calls = []
    with pytest.raises(ValueError, match="no pending"):
        installer.install(sha, root / "clone", run=controls(calls))
    assert not (installer.HOSTLIB / "current").exists()
    assert not any(call[:2] == ["systemctl", "start"] for call in calls)


def test_replacement_git_objects_cannot_change_the_bytes_bound_to_a_source_sha(installation, monkeypatch):
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)
    root, sha = installation
    clone = root / "clone"
    driver = clone / "scripts/deploy-company-os.sh"
    original = driver.read_bytes()
    driver.write_bytes(original + b"\n# replacement object must not become the approved source\n")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
    subprocess.run(["git", "-C", str(clone), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "-qm", "replacement fixture"], check=True)
    replacement = subprocess.check_output(["git", "-C", str(clone), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(clone), "replace", sha, replacement], check=True)
    bundle = installer.stage_bundle(clone, sha, root / "next")
    assert (bundle / "scripts/deploy-company-os.sh").read_bytes() == original


def test_installer_uses_the_active_public_or_private_host_clone(installation):
    root, _ = installation
    config = root / "host-config.json"
    config.write_text(json.dumps({"clone": str(root / "clone")}))
    assert installer.configured_clone(config) == root / "clone"
    config.chmod(0o666)
    with pytest.raises(ValueError, match="untrusted"):
        installer.configured_clone(config)
