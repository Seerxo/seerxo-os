"""Release gateway service (G3), end to end on real git repositories with throwaway keys: a low-risk commit on
main becomes a signed manifest the host verifier accepts; anything else stops with a recorded reason, and no
check or model call is repeated for the same content."""
import json
import hashlib
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import release_gateway as rg, release_gateway_service as svc

pytestmark = pytest.mark.skipif(not (shutil.which("ssh-keygen") and shutil.which("git")), reason="needs git, ssh-keygen")

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
DRIVER = "ef" * 32


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def bound(sha: str) -> dict:
    """The four bound image IDs a build of `sha` produces, one per component, content addressed."""
    return {c: "sha256:" + hashlib.sha256(f"{sha}:{c}".encode()).hexdigest() for c in rg.BOUND}


def keypair(d: Path, name: str):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(d / name)], check=True)
    return d / name, (d / f"{name}.pub").read_text().strip()


class World:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.keys = {n: keypair(tmp, n) for n in ("founder", "gateway", "reviewer", "agent")}
        self.work = tmp / "work"
        sh(tmp, "git", "init", "-q", "-b", "main", str(self.work))
        for k, v in (("user.email", "a@b"), ("user.name", "a")):
            sh(self.work, "git", "config", k, v)
        (self.work / "docs").mkdir()
        (self.work / "app").mkdir()
        (self.work / "docs/HANDOFF.md").write_text("old\n")
        (self.work / "app/main.py").write_text("x = 1\n")
        self.builds = tmp / "builds"
        self.builds.mkdir()
        self.base = self.commit("base")
        self.base_inputs = self.inputs_of(self.base)
        self.bound = bound(self.base_inputs)      # images are addressed by the inputs they were built from
        self.origin = tmp / "origin.git"
        sh(tmp, "git", "clone", "-q", "--bare", str(self.work), str(self.origin))
        sh(self.work, "git", "remote", "add", "origin", str(self.origin))
        self.clone = tmp / "gateway-clone"
        sh(tmp, "git", "clone", "-q", str(self.origin), str(self.clone))
        policy = json.loads((ROOT / "agents/release_policy.json").read_text()) | {
            "gateway_key": self.keys["gateway"][1], "reviewer_keys": {"codex": self.keys["reviewer"][1]}}
        raw = rg.canonical(policy)
        (tmp / "policy.json").write_bytes(raw)
        (tmp / "policy.sig").write_text(rg.sign(raw, self.keys["founder"][0], rg.NS_POLICY))
        (tmp / "founder.pub").write_text(self.keys["founder"][1])
        (tmp / "ledger.json").write_text(json.dumps({"last_sha": self.base, "last_sequence": 6, "seen": [],
                                                     "min_policy_version": 1, "bound_images": self.bound,
                                                     "bound_inputs": self.base_inputs,
                                                     "bound_driver_sha256": DRIVER}))
        self.report(self.base)
        self.cfg = svc.Config({
            "repository": "seerxo/os", "branch": "main", "clone": str(self.clone), "fetch_url": str(self.origin),
            "policy": str(tmp / "policy.json"), "policy_sig": str(tmp / "policy.sig"),
            "founder_key": str(tmp / "founder.pub"), "gateway_key": str(self.keys["gateway"][0]),
            "ledger": str(tmp / "ledger.json"), "state": str(tmp / "state.json"), "outbox": str(tmp / "outbox"),
            "founder_queue": str(tmp / "queue"), "review_signer_socket": str(tmp / "signer.sock"),
            "builds": str(self.builds),
            "checks_command": ["true"], "identity_report": str(tmp / "identity.json")})
        self.check_calls, self.review_calls = [], []
        self.checks_pass, self.verdict, self.review_key, self.review_error = True, "APPROVE", "reviewer", None

    def report(self, last_sha, ok=True, pending=False, at=None, problem=None):
        """The host's identity verdict as root writes it; the gateway only reads it."""
        (self.tmp / "identity.json").write_text(json.dumps({
            "checked_at": (at or NOW).isoformat(), "ok": ok, "problem": problem, "last_sha": last_sha,
            "pending": pending}))

    def commit(self, message, **files):
        for path, text in files.items():
            p = self.work / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        sh(self.work, "git", "add", "-A")
        sh(self.work, "git", "commit", "-qm", message)
        sha = sh(self.work, "git", "rev-parse", "HEAD")
        self.prepare(sha)                     # the host built images for this commit before the gateway looked
        return sha

    def inputs_of(self, sha, repo=None):
        return rg.inputs_digest(rg.release_archive(repo or self.work, sha))

    def prepare(self, sha, repo=None):
        """What root's `prepare` writes: a ready build record for exactly this commit's image inputs. Image IDs are
        addressed by those inputs, so two commits with the same tree name the same four images (as production does)."""
        try:
            digest = self.inputs_of(sha, repo)
        except (rg.Refused, subprocess.CalledProcessError):
            return None                       # an archive that would ship other bytes: no record, and no release
        (self.builds / f"{digest}.json").write_text(json.dumps({
            "inputs_digest": digest, "built_from_sha": sha, "repository": "seerxo/os", "recipe": "fixture",
            "bootstrap": False, "attempt": 1, "built_at": NOW.isoformat(), "status": "ready",
            "images": bound(digest)}))
        return digest

    def push(self, force=False):
        sh(self.work, "git", "push", "-q", *(["--force"] if force else []), "origin", "main")

    def checks(self, sha):
        self.check_calls.append(sha)
        return {"passed": self.checks_pass, "summary": "fixture"}

    def review(self, packet):
        self.review_calls.append(packet["nonce"])
        if self.review_error:
            raise self.review_error
        out = {"verdict": self.verdict, "fingerprint": packet["fingerprint"], "findings": []}
        att = rg.attestation("codex", packet, out, NOW.isoformat())
        return {"attestation": att, "signature": rg.sign(rg.canonical(att), self.keys[self.review_key][0], rg.NS_REVIEW)}

    def tick(self, clock=None):
        return svc.tick(self.cfg, now=NOW, clock=clock or (lambda: NOW), checks=self.checks, review=self.review)

    def host_verify(self, name):
        """What the production host does with the outbox entry, from its own clone (here: the work tree)."""
        raw = (self.cfg.outbox / f"{name}.json").read_bytes()
        sig = (self.cfg.outbox / f"{name}.sig").read_text()
        m = json.loads(raw)
        ledger = json.loads(self.cfg.ledger.read_text())
        policy = rg.load_policy(self.cfg.policy.read_bytes(), self.cfg.policy_sig.read_text(), self.keys["founder"][1])
        digest = self.inputs_of(m["sha"])
        record, _ = svc.read_build_record(self.builds, digest)
        facts = {"repository": "seerxo/os", "branch": "main", "sha": m["sha"],
                 "payload_digest": rg.payload_digest(rg.payload_listing(self.work, m["sha"])),
                 "changed_paths": rg.git_changed_paths(self.work, ledger["last_sha"], m["sha"]),
                 "ancestor_of_parent": True, "diff": rg.git_release_diff(self.work, ledger["last_sha"], m["sha"]),
                 "inputs_digest": digest, "build_record": record, "driver_bound": True, "driver_sha256": DRIVER,
                 "images_present": True, "anchored_images": True}
        return rg.verify_release(raw, sig, policy=policy, founder_key=self.keys["founder"][1], host_facts=facts,
                                 state=ledger, now=NOW)


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def test_a_low_risk_commit_becomes_a_manifest_the_host_accepts(w):
    sha = w.commit("docs", **{"docs/HANDOFF.md": "new\n"})
    w.push()
    out = w.tick()
    assert out["outcome"] == "SIGNED" and out["sha"] == sha
    assert w.host_verify(out["detail"])["sha"] == sha
    assert len(w.check_calls) == 1 and len(w.review_calls) == 1
    assert not w.cfg.founder_queue.exists()


def test_the_same_commit_is_never_checked_or_reviewed_twice(w):
    w.commit("docs", **{"docs/HANDOFF.md": "new\n"})
    w.push()
    w.tick()
    for _ in range(3):
        assert w.tick()["outcome"] == "ALREADY_DECIDED"
    assert len(w.check_calls) == 1 and len(w.review_calls) == 1               # no repeated model call


@pytest.mark.parametrize("files", [{"app/main.py": "x = 2\n"}, {"migrations/versions/a.py": "rev = 1\n"},
                                   {".github/workflows/ci.yml": "on: push\n"}, {"app/api_auth/__init__.py": "\n"}])
def test_a_high_risk_commit_is_queued_for_the_founder_never_signed(w, files):
    sha = w.commit("risky", **files)
    w.push()
    out = w.tick()
    assert out["outcome"] == "FOUNDER_REQUIRED" and out["queued"] is True
    assert not w.cfg.outbox.exists() or not list(w.cfg.outbox.iterdir())
    draft = json.loads(next(w.cfg.founder_queue.iterdir()).read_text())
    assert draft["sha"] == sha and draft["approver"] == "founder"


def test_failing_checks_stop_before_any_model_call(w):
    w.commit("docs", **{"docs/HANDOFF.md": "new\n"})
    w.push()
    w.checks_pass = False
    assert w.tick()["outcome"] == "CHECKS_FAILED"
    assert w.review_calls == [] and not w.cfg.outbox.exists()


@pytest.mark.parametrize("setup", [lambda w: setattr(w, "verdict", "REJECT"),
                                   lambda w: setattr(w, "review_key", "agent")])     # a forged reviewer signature
def test_a_rejected_or_forged_review_is_never_signed(w, setup):
    w.commit("docs", **{"docs/HANDOFF.md": "new\n"})
    w.push()
    setup(w)
    assert w.tick()["outcome"] == "REVIEW_REFUSED"
    assert not w.cfg.outbox.exists()


def test_an_unreachable_reviewer_is_retried_a_bounded_number_of_times(w):
    w.commit("docs", **{"docs/HANDOFF.md": "new\n"})
    w.push()
    w.review_error = svc.ReviewNotAsked("signer socket missing")      # the model was certainly not asked
    outcomes = [w.tick()["outcome"] for _ in range(svc.MAX_ATTEMPTS + 2)]
    assert outcomes == ["RETRY_LATER"] * svc.MAX_ATTEMPTS + ["GIVEN_UP"] * 2
    assert len(w.review_calls) == svc.MAX_ATTEMPTS and not w.cfg.outbox.exists()


def test_a_rewritten_main_is_refused(w):
    sh(w.work, "git", "reset", "-q", "--hard", "HEAD")
    sh(w.work, "git", "checkout", "-q", "--orphan", "other")
    (w.work / "docs/HANDOFF.md").write_text("rewritten\n")
    sh(w.work, "git", "add", "-A")
    sh(w.work, "git", "commit", "-qm", "unrelated history")
    sh(w.work, "git", "push", "-q", "--force", "origin", "other:main")
    out = w.tick()
    assert out["outcome"] == "REFUSED" and "descend" in out["detail"] and w.check_calls == []


@pytest.mark.parametrize("ledger, why", [({"last_sequence": 0, "seen": []}, "not bootstrapped"),
                                         ({"last_sha": "x", "pending": "abc"}, "pending")])
def test_without_an_applied_release_or_with_a_pending_one_nothing_happens(w, ledger, why):
    w.cfg.ledger.write_text(json.dumps(ledger))
    w.commit("docs", **{"docs/HANDOFF.md": "new\n"})
    w.push()
    out = w.tick()
    assert out["outcome"] == "WAITING" and why in out["reason"] and w.check_calls == []


def test_nothing_new_is_idle(w):
    assert w.tick()["outcome"] == "IDLE" and w.check_calls == []


def test_a_tampered_policy_stops_the_gateway(w):
    w.cfg.policy.write_bytes(w.cfg.policy.read_bytes().replace(b'"auto_release":true', b'"auto_release":false'))
    with pytest.raises(rg.Refused, match="founder key"):
        w.tick()


def test_the_payload_listing_is_the_one_the_deploy_script_ships(w):
    deploy = (ROOT / "scripts/deploy-company-os.sh").read_text()
    line = next(l for l in deploy.splitlines() if l.startswith("PATHS="))
    assert tuple(line.split("=", 1)[1].strip('"').split()) == rg.DEPLOY_PATHS
    sha = w.commit("app", **{"app/main.py": "x = 3\n", "scripts/s.sh": "echo\n"})
    shell = subprocess.run(["bash", "-c", "git ls-tree -r \"$1\" --format='%(objectname) %(path)' -- app scripts "
                            "| while read -r obj p; do printf '%s  %s\\n' \"$(git cat-file -p \"$obj\" | shasum -a 256 "
                            "| cut -c1-64)\" \"$p\"; done", "_", sha], cwd=w.work, capture_output=True, text=True,
                           check=True).stdout
    assert rg.payload_digest(shell) == rg.payload_digest(rg.payload_listing(w.work, sha))


def test_a_review_that_takes_longer_than_the_clock_skew_still_releases(w):
    # Review finding: `now` was fixed at the start of the tick, so a verdict issued 10 minutes later was "future-dated"
    # and the commit was refused for good.
    from datetime import timedelta
    later = NOW + timedelta(minutes=10)
    w.commit("docs", **{"docs/HANDOFF.md": "new\n"})
    w.push()
    real_review = w.review

    def slow_review(packet):
        out = {"verdict": "APPROVE", "fingerprint": packet["fingerprint"], "findings": []}
        att = rg.attestation("codex", packet, out, later.isoformat())
        w.review_calls.append(packet["nonce"])
        return {"attestation": att, "signature": rg.sign(rg.canonical(att), w.keys["reviewer"][0], rg.NS_REVIEW)}
    w.review = slow_review
    times = iter([later])
    out = svc.tick(w.cfg, now=NOW, clock=lambda: next(times), checks=w.checks, review=w.review)
    assert out["outcome"] == "SIGNED"
    manifest = json.loads((w.cfg.outbox / f"{out['detail']}.json").read_text())
    assert manifest["issued_at"] == later.isoformat()


def test_after_a_rejection_the_next_candidate_on_the_same_base_goes_to_the_founder(w):
    # Review finding: a trivial new commit bought a fresh roll of the reviewer for the same diff.
    w.commit("docs", **{"docs/HANDOFF.md": "new\n"})
    w.push()
    w.verdict = "REJECT"
    assert w.tick()["outcome"] == "REVIEW_REFUSED"
    w.verdict = "APPROVE"
    w.commit("noop", **{"docs/other.md": "x\n"})
    w.push()
    out = w.tick()
    assert out["outcome"] == "FOUNDER_REQUIRED" and "rejected" in out["detail"]
    assert not w.cfg.outbox.exists() or not list(w.cfg.outbox.iterdir())


@pytest.mark.parametrize("content", ["[]", "{not json", '{"done": []}'])
def test_a_malformed_state_file_refuses_instead_of_crashing(w, content):
    w.cfg.state.write_text(content)
    with pytest.raises(rg.Refused, match="state"):
        w.tick()


def test_ticks_are_serialised_by_a_lock(w):
    import fcntl
    w.commit("docs", **{"docs/HANDOFF.md": "new\n"})
    w.push()
    held = []

    def checks(sha):
        with open(w.cfg.state.with_name(w.cfg.state.name + ".lock"), "a") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.append(sha)
        return {"passed": True}
    svc.tick(w.cfg, now=NOW, clock=lambda: NOW, checks=checks, review=w.review)
    assert held


@pytest.mark.parametrize("report, why", [
    (dict(ok=False, problem="worker: running image differs"), "not exactly the recorded release"),
    (dict(pending=True), "not exactly the recorded release"),
    (dict(at=NOW - timedelta(hours=1)), "stale"),
    (dict(at=NOW + timedelta(minutes=5)), "stale"),                         # future-dated
    (dict(at=NOW.replace(tzinfo=None)), "malformed"),                       # naive time
    ("missing", "not verified by the host"),
    ("other", "not exactly the recorded release"),
])
def test_the_gateway_prepares_nothing_unless_the_host_verified_production_identity(w, report, why):
    w.commit("docs", **{"docs/HANDOFF.md": "new\n"})
    w.push()
    if report == "missing":
        (w.tmp / "identity.json").unlink()
    elif report == "other":
        w.report("9" * 40)
    else:
        w.report(w.base, **report)
    out = w.tick()
    assert out["outcome"] == "WAITING" and why in out["reason"]
    assert w.check_calls == [] and w.review_calls == []
    assert json.loads((w.tmp / "identity.json").read_text()) if report != "missing" else True   # never rewritten


def test_a_docs_only_tip_on_top_of_unreleased_backend_changes_is_judged_on_the_whole_delta(w):
    w.commit("backend", **{"app/main.py": "x = 2\n"})
    w.commit("migration", **{"migrations/versions/a.py": "rev = 1\n"})
    w.commit("docs", **{"docs/HANDOFF.md": "new\n"})                         # the newest commit is docs-only
    w.push()
    out = w.tick()
    assert out["outcome"] == "FOUNDER_REQUIRED"
    draft = json.loads(next(w.cfg.founder_queue.iterdir()).read_text())
    assert draft["parent_sha"] == w.base                                       # from the verified baseline
    assert {"app/main.py", "migrations/versions/a.py", "docs/HANDOFF.md"} <= set(draft["changed_paths"])


@pytest.mark.parametrize("where", ["sign", "outbox", "founder_queue"])
def test_a_failure_after_the_review_is_bounded_and_never_reruns_the_model_unboundedly(w, monkeypatch, where):
    # Anything that fails after checks and review (signing, the outbox or founder-queue write) must count as an
    # attempt that was recorded BEFORE the model was asked, so it is bounded by MAX_ATTEMPTS.
    files = {"docs/HANDOFF.md": "new\n"} if where != "founder_queue" else {"app/main.py": "x = 2\n"}
    w.commit("change", **files)
    w.push()
    if where == "sign":
        real = rg.sign
        monkeypatch.setattr(svc.rg, "sign", lambda data, key, ns: (_ for _ in ()).throw(OSError("hsm gone"))
                            if ns == rg.NS_RELEASE else real(data, key, ns))
    else:
        getattr(w.cfg, where).parent.mkdir(parents=True, exist_ok=True)
        getattr(w.cfg, where).write_text("not a directory")
    outcomes = []
    for _ in range(svc.MAX_ATTEMPTS + 2):
        try:
            outcomes.append(w.tick()["outcome"])
        except Exception as error:                # an escaping error is acceptable only if it was counted first
            outcomes.append(type(error).__name__)
    assert len(w.review_calls) <= svc.MAX_ATTEMPTS, outcomes
    assert outcomes[-1] == "GIVEN_UP", outcomes
    assert json.loads(w.cfg.state.read_text())["attempts"]


def test_the_gateway_refuses_a_commit_whose_archive_differs_from_the_reviewed_bytes(w):
    sha = w.commit("subject becomes code", **{".gitattributes": "app/main.py export-subst\n",
                                              "app/main.py": "$Format:%s$\n"})
    w.push()
    out = w.tick()
    assert out["outcome"] == "REFUSED" and "hashed payload" in out["detail"] and out["sha"] == sha
    assert w.check_calls == [] and w.review_calls == []                     # decided before any check or model call



@pytest.mark.parametrize("error", [TimeoutError("read timed out"), RuntimeError("review signer answered HTTP 502"),
                                   ConnectionResetError("reset after send")])
def test_an_unknown_review_outcome_counts_as_a_rejection_on_that_base(w, error):
    # The signer may have asked the model (and an answer may exist) before the transport failed: retrying would be a
    # fresh reviewer roll. The commit is decided, and later candidates on the same base go to the founder.
    w.commit("docs", **{"docs/HANDOFF.md": "new\n"})
    w.push()
    w.review_error = error
    assert w.tick()["outcome"] == "REVIEW_UNKNOWN"
    assert w.tick()["outcome"] == "ALREADY_DECIDED" and len(w.review_calls) == 1
    w.review_error = None
    w.commit("docs again", **{"docs/HANDOFF.md": "newer\n"})
    w.push()
    assert w.tick()["outcome"] == "FOUNDER_REQUIRED"


@pytest.mark.parametrize("status, kind", [(503, "not asked"), (400, "not asked"), (403, "not asked"),
                                          (409, "not asked"), (502, "unknown"), (500, "unknown")])
def test_signer_statuses_are_classified(monkeypatch, w, status, kind):
    class Resp:
        def __init__(self):
            self.status = status

        def read(self):
            return b"{}"

    class Conn:
        def __init__(self, *a, **k):
            pass

        def connect(self):
            pass

        def request(self, *a, **k):
            pass

        def getresponse(self):
            return Resp()

        def close(self):
            pass
    monkeypatch.setattr(svc, "_UnixConnection", Conn)
    packet = rg.review_packet("seerxo/os", w.base, w.base, "", "ab" * 16)
    expected = svc.ReviewNotAsked if kind == "not asked" else RuntimeError
    with pytest.raises(expected) as raised:
        svc.default_review(w.cfg, packet)
    assert (type(raised.value) is svc.ReviewNotAsked) == (kind == "not asked")


def test_a_missing_signer_socket_means_the_review_was_not_asked(w):
    packet = rg.review_packet("seerxo/os", w.base, w.base, "", "ab" * 16)
    with pytest.raises(svc.ReviewNotAsked):
        svc.default_review(w.cfg, packet)                               # tmp/signer.sock does not exist


def test_gateway_state_is_fsynced_with_its_directory(w, monkeypatch):
    synced = []
    real = os.fsync

    def fsync(fd):
        synced.append(os.fstat(fd).st_mode)
        real(fd)
    monkeypatch.setattr(svc.os, "fsync", fsync)
    w.tick()
    import stat as st
    assert any(st.S_ISREG(m) for m in synced) and any(st.S_ISDIR(m) for m in synced)
