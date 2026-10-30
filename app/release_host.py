"""Production host release entry (G1): the only path by which a commit is applied, decided on the host itself.

Design: docs/security/RELEASE_GATEWAY.md. Standard library only. Installed root-owned OUTSIDE the deploy payload
(founder step), with the founder's public key pinned at install, and run by root from a timer:
    python3 -m app.release_host

Each step takes the next manifest (gateway outbox: low risk, gateway-signed; founder inbox: founder-signed),
fetches the named commit into the host's own clone with a read-only credential, computes every fact itself
(commit, payload digest, changed paths, release diff, ancestry), and runs release_gateway.release_transaction.
Apply builds the same three artifacts the staged deploy transport ships (src.tgz, main.sha256 and the deploy
driver's root-side script) from that clone and runs the installed driver synchronously: its exit code decides.
A failure leaves the ledger `pending`, and no later release is applied until the founder reconciles the host.
No GitHub workflow, runner or SSH key is involved, so none of them can change production.

Images are built BEFORE a manifest is signed (`prepare`) and bound by content-addressed ID in the manifest; apply
never builds (`IMAGE_BINDING_DESIGN.md` §3–§5). The verifier and the driver are one installed bundle, self-checked
before every command, so a half-copied install can never run an old driver under a new verifier.
"""
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import release_gateway as rg
from app.release_identity import security_config, SET_LIKE, VOLATILE_CONFIG, VOLATILE_HOST_CONFIG, _normalise

REMOTE_START, REMOTE_END = "<<'REMOTE'\n", "\nREMOTE\n"
# The deploy driver accepts only RELEASE_DIR =~ ^/run/company-os-deploy/[0-9a-f]{40}\.[A-Za-z0-9]{8}$ (F3).
RUN_ROOT_PRODUCTION = Path("/run/company-os-deploy")
RUN_ROOT = RUN_ROOT_PRODUCTION                       # tests point this elsewhere; the config must name it exactly
# The compose file the driver deploys from (it runs in /opt/company-os); tests point this elsewhere.
HOST_COMPOSE = Path("/opt/company-os/docker-compose.yml")
# The ledger path the deploy driver reads to recognise a bootstrapped host (verify-release-manifest, B6 step 8).
LEDGER_PRODUCTION = Path("/var/lib/release-host/ledger/ledger.json")
LEDGER = LEDGER_PRODUCTION                           # tests point this elsewhere; the config must name it exactly
# Pre-sign build records (design §3): root-written, one per image-inputs digest.
BUILDS_PRODUCTION = Path("/var/lib/release-host/builds")
BUILDS = BUILDS_PRODUCTION                           # tests point this elsewhere
# Drained at install (design §4.1): every release command takes it shared; the installer exclusive.
INSTALL_LOCK_PRODUCTION = Path("/var/lib/release-host/install.lock")
INSTALL_LOCK = INSTALL_LOCK_PRODUCTION               # tests point this elsewhere
# The installed verifier+driver bundle (`current` is the symlink the systemd units work from).
BUNDLE_PRODUCTION = Path("/usr/local/lib/release-host/current")
BUNDLE = Path(__file__).resolve().parents[1]         # a staged bundle may only prepare a founder bootstrap
DRIVER_BOUND_IMAGES = "DRIVER_BOUND_IMAGES=1"
KEEP_APPLIED = 3                                     # bound `applied` sets whose anchors GC keeps
BUILD_TTL = timedelta(days=7)                        # an unapplied build record keeps its anchors this long


class RetryLater(RuntimeError):
    """Infrastructure, not the manifest: fetch, git, an unreadable policy. Nothing is moved; the next step retries."""


# The fetch remote must be the configured (and policy-signed) repository on GitHub, so a config cannot name one
# repository and fetch another (a fork with the same base). Tests use local repositories and switch this off.
PIN_REMOTE = True


def remote_is(fetch_url: str, repository: str) -> bool:
    url, repo = fetch_url.lower().removesuffix("/"), repository.lower()
    return url in (f"https://github.com/{repo}.git", f"https://github.com/{repo}", f"git@github.com:{repo}.git")


class Config:
    def __init__(self, raw: dict):
        need = {"repository", "branch", "clone", "fetch_url", "policy", "policy_sig", "founder_key", "ledger",
                "inboxes", "rejected", "run_root", "identity_report"}
        if not isinstance(raw, dict) or set(raw) != need:
            raise rg.Refused(f"host release config must have exactly {sorted(need)}")
        self.__dict__.update(raw)
        for key in ("clone", "policy", "policy_sig", "founder_key", "ledger", "rejected", "run_root", "identity_report"):
            setattr(self, key, Path(raw[key]))
        if not isinstance(raw["inboxes"], list) or not raw["inboxes"]:
            raise rg.Refused("inboxes must list the gateway outbox and the founder inbox")
        self.inboxes = [Path(p) for p in raw["inboxes"]]
        if self.run_root != RUN_ROOT:
            raise rg.Refused(f"run_root must be {RUN_ROOT}: the deploy driver refuses any other RELEASE_DIR")
        if self.ledger != LEDGER:
            raise rg.Refused(f"ledger must be {LEDGER}: the deploy driver reads only that path to know the host is bootstrapped")
        if PIN_REMOTE and not remote_is(self.fetch_url, self.repository):
            raise rg.Refused(f"fetch_url must be the GitHub remote of {self.repository}")


def _release_dir_name(sha: str) -> str:
    """<sha>.<8 hex>: exactly what the driver's RELEASE_DIR check accepts (tempfile suffixes may contain "_")."""
    return f"{sha}.{secrets.token_hex(4)}"


# refs/replace/* in a clone could make git answer about other objects than the ones named: never honoured here.
os.environ["GIT_NO_REPLACE_OBJECTS"] = "1"


def _git(cfg: Config, *args: str, text=True):
    return subprocess.run(["git", "-C", str(cfg.clone), *args], check=True, capture_output=True, text=text).stdout


MAX_INBOX_FILE = 1 << 20     # a manifest or signature is a few KiB; nothing bigger is read by root
MAX_BUILD_RECORD = 1 << 20   # a build record is smaller still


def _bounded_read(path: Path, limit: int, *, trusted_file: bool = False) -> bytes:
    """Read a file someone else may have written, as root: never through a symlink, never a FIFO or device, and
    bounded, so a planted link cannot make root read /dev/zero or a root-only file. `trusted_file` additionally
    requires a root (or this process's user) owner and no group/world write bit: a build record or bundle file."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        trusted = {0, os.geteuid()}
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise rg.Refused(f"{path.name} is not a regular file of at most {limit} bytes")
        if info.st_nlink != 1:
            raise rg.Refused(f"{path.name} has another hard link; root reads only a file with one name")
        if trusted_file and (info.st_uid not in trusted or info.st_mode & 0o022):
            raise rg.Refused(f"{path.name} is not a root-owned, unwritable file")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise rg.Refused(f"{path.name} is not a regular file of at most {limit} bytes")
    return data


def _read_inbox(path: Path) -> bytes:
    return _bounded_read(path, MAX_INBOX_FILE)


def untrusted_path(path: Path, *, writable_leaf: bool = False) -> str | None:
    """Why root must not use `path`, or None. Every existing component must be a real directory (no symlink),
    owned by root (or this process's user, for tests) and not group- or world-writable, so nobody else can swap a
    component for a link. An inbox (`writable_leaf`) may itself be owned and written by the gateway, but must still
    be a real directory under trusted parents. A missing tail is created by root later, under a trusted parent."""
    if not path.is_absolute():
        return f"untrusted path: {path} is not absolute"
    trusted = {0, os.geteuid()}
    current = Path(path.anchor)
    for i, part in enumerate(path.parts):
        current = current / part if i else current
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return None
        leaf = current == path
        if not stat.S_ISDIR(info.st_mode):
            return f"untrusted path: {current} is not a real directory"
        if leaf and writable_leaf:
            continue
        if info.st_uid not in trusted or info.st_mode & 0o022:
            return f"untrusted path: {current} is owned by uid {info.st_uid} or group/world-writable"
    return None


def _regular(path: Path) -> bool:
    """Only regular files are queue entries. A directory, FIFO or symlink named like a manifest is skipped, never
    moved into root's rejected/ (a moved gateway directory would wedge the queue on the next same-named file)."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def untrusted_layout(cfg: Config) -> str | None:
    """Every directory root reads, moves or writes in, checked before anything is touched: the ledger's and the
    identity report's (strict), rejected/ and run_root (strict), the build records and the install lock (strict),
    each inbox (may be gateway-writable itself)."""
    checks = [(cfg.ledger.parent, False), (cfg.identity_report.parent, False), (cfg.rejected, False),
              (cfg.run_root, False), (BUILDS, False), (INSTALL_LOCK.parent, False),
              *[(box, True) for box in cfg.inboxes]]
    for path, writable in checks:
        problem = untrusted_path(path, writable_leaf=writable)
        if problem:
            return problem
    return None


def candidates(cfg: Config) -> list[tuple[int, Path]]:
    """Manifests waiting in the inboxes, lowest sequence first. Names are only a hint; the content decides.
    Dot-files are the gateway's in-flight writes (renamed into place last) and are never picked up."""
    found = []
    for box in cfg.inboxes:
        for path in sorted(p for p in box.glob("*.json") if not p.name.startswith(".") and _regular(p)) \
                if box.is_dir() else []:
            try:
                seq = json.loads(_read_inbox(path)).get("sequence")
            except (OSError, ValueError, AttributeError, rg.Refused):
                seq = None
            found.append((seq if isinstance(seq, int) and not isinstance(seq, bool) else 10**12, path))
    return sorted(found)


# --------------------------------------------------------------------------- installed bundle

BUNDLE_FIELDS = {"bundle_version", "source_sha", "files"}
BUNDLE_FILES = {"app/__init__.py", "app/release_gateway.py", "app/release_host.py", "app/release_identity.py",
                "scripts/deploy-company-os.sh"}
DRIVER_BUNDLE_VERSION = re.compile(r"^DRIVER_BUNDLE_VERSION=(\d+)$", re.M)


def bundle_problem(*, allow_staged: bool = False) -> str | None:
    """Why the installed verifier+driver bundle must not run, or None. A half-copied install (one file's digest
    differs), a missing BUNDLE.json, a non-root-owned or group-writable directory, a driver without bound mode or a
    version mismatch would otherwise let an old driver run under a new verifier. Called first in every command."""
    try:
        root = BUNDLE.resolve(strict=True)
    except OSError:
        return f"release bundle inconsistent: {BUNDLE} is missing"
    problem = untrusted_path(BUNDLE.parent) or untrusted_path(root)
    if problem:
        return f"release bundle inconsistent: {problem}"
    if not allow_staged and root != BUNDLE_PRODUCTION.resolve():
        return "release bundle inconsistent: staged code cannot operate the installed release host"
    try:
        doc = json.loads(_bounded_read(root / "BUNDLE.json", MAX_BUILD_RECORD, trusted_file=True))
    except (OSError, ValueError, rg.Refused) as error:
        return f"release bundle inconsistent: BUNDLE.json is unreadable ({type(error).__name__})"
    if not isinstance(doc, dict) or set(doc) != BUNDLE_FIELDS:
        return "release bundle inconsistent: BUNDLE.json has the wrong shape"
    if not rg._int(doc.get("bundle_version")) or doc["bundle_version"] != rg.BUNDLE_VERSION:
        return "release bundle inconsistent: BUNDLE.json names another bundle version"
    files = doc.get("files")
    if not isinstance(doc.get("source_sha"), str) or not rg.SHA.fullmatch(doc["source_sha"]):
        return "release bundle inconsistent: BUNDLE.json has no valid source SHA"
    if not isinstance(files, dict) or set(files) != BUNDLE_FILES:
        return "release bundle inconsistent: BUNDLE.json must list the complete verifier and driver"
    from app import release_identity
    if any(Path(module.__file__).resolve() != root / name for module, name in (
            (sys.modules[__name__], "app/release_host.py"), (rg, "app/release_gateway.py"),
            (release_identity, "app/release_identity.py"))):
        return "release bundle inconsistent: the running verifier is outside the checked bundle"
    for name, want in files.items():
        if not isinstance(name, str) or not isinstance(want, str) or not rg.DIGEST.fullmatch(want) \
                or not rg.SAFE_PATH.fullmatch(name):
            return f"release bundle inconsistent: BUNDLE.json has a malformed entry ({name!r})"
        path = root / name
        try:
            data = _bounded_read(path, 64 << 20, trusted_file=True)
        except (OSError, rg.Refused) as error:
            return f"release bundle inconsistent: {name} is unreadable ({type(error).__name__})"
        if hashlib.sha256(data).hexdigest() != want:
            return f"release bundle inconsistent: {name} differs from BUNDLE.json"
    if "scripts/deploy-company-os.sh" not in files:
        return "release bundle inconsistent: the deploy driver is not in the bundle"
    driver = _bounded_read(root / "scripts/deploy-company-os.sh", 64 << 20, trusted_file=True).decode(
        "utf-8", "surrogateescape")
    if DRIVER_BOUND_IMAGES not in driver:
        return "release bundle inconsistent: the driver does not speak bound mode"
    version = DRIVER_BUNDLE_VERSION.search(driver)
    if not version or int(version.group(1)) != rg.BUNDLE_VERSION:
        return "release bundle inconsistent: the driver's bundle version differs from the verifier's"
    return None


# --------------------------------------------------------------------------- facts

def _build_record_path(digest: str) -> Path:
    return BUILDS / f"{digest}.json"


def read_build_record(digest: str) -> tuple[dict | None, str | None]:
    """The root-written record for exactly these image inputs, or (None, None) when there is none. A symlink, a
    second hard link, another owner, a group-writable directory or a record whose inner digest differs is a problem,
    never silently "no record": a copied record must not authorise another tree's images."""
    problem = untrusted_path(BUILDS)
    if problem:
        return None, problem
    try:
        raw = _bounded_read(_build_record_path(digest), MAX_BUILD_RECORD, trusted_file=True)
    except FileNotFoundError:
        return None, None
    except (OSError, rg.Refused) as error:
        return None, f"the build record is unreadable ({type(error).__name__})"
    try:
        record = json.loads(raw)
    except ValueError:
        return None, "the build record is not valid JSON"
    if not isinstance(record, dict):
        return None, "the build record is not an object"
    if record.get("inputs_digest") != digest:
        return record, "the build record is for different image inputs"
    if record.get("status") not in ("building", "ready", "failed"):
        return record, "the build record has an unknown status"
    if not rg._int(record.get("attempt")) or record["attempt"] < 1:
        return record, "the build record has no valid attempt number"
    return record, None


def _bounded_call(docker, args: list[str]) -> subprocess.CompletedProcess:
    """Run a Docker read as root. A transport error is infrastructure (retry), never a verdict."""
    try:
        return docker(["docker", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        raise RetryLater(f"docker {args[0]} failed: {type(error).__name__}") from None


def image_present(image_id: str, docker=subprocess.run) -> bool:
    """`docker image inspect --format {{.Id}} ID` answers exactly the ID (never a tag resolution)."""
    result = _bounded_call(docker, ["image", "inspect", "--format", "{{.Id}}", image_id])
    return result.returncode == 0 and result.stdout.strip() == image_id


def anchored_images(docker=subprocess.run) -> set[str]:
    """The image IDs held by every `company-os-bound-*` anchor container (never started; Coolify's cleanup only
    removes tagged images no container uses). A failing listing is infrastructure: GC and verify retry later.
    --no-trunc here and in gc_anchors: without it `docker ps` prints a 12-character ID that never equals a bound one."""
    result = _bounded_call(docker, ["ps", "-a", "--no-trunc", "--filter", "name=company-os-bound-",
                                    "--format", "{{.Names}}\t{{.Image}}"])
    if result.returncode != 0:
        raise RetryLater("docker ps failed while reading the release anchors")
    held = set()
    for line in result.stdout.splitlines():
        name, sep, image = line.partition("\t")
        if sep and name.startswith("company-os-bound-") and image:
            held.add(image)
    return held


def observe_bound(docker=subprocess.run) -> dict | None:
    """The four bound image IDs as containers run them now, or None when any is unavailable. Egress is bound in the
    manifest but stays outside the three-component identity (design §3)."""
    observed = {}
    for name in rg.BOUND:
        result = _bounded_call(docker, ["inspect", "--type", "container", "--format", "{{.Image}}",
                                        CONTAINERS[name]])
        if result.returncode != 0:
            return None
        observed[name] = result.stdout.strip()
    return observed if rg.images_ok(observed) else None


def images_present(ids: dict, docker=subprocess.run) -> bool:
    return rg.images_ok(ids) and all(image_present(ids[name], docker) for name in rg.BOUND)


def images_anchored(ids: dict, docker=subprocess.run) -> bool:
    return rg.images_ok(ids) and set(ids.values()) <= anchored_images(docker)


def host_facts(cfg: Config, manifest: dict, ledger: dict, docker=subprocess.run) -> dict:
    """Everything verify_release compares, computed here from the host's own clone. Also what apply needs to know
    about images before `pending`: their inputs digest, the build record for it, local presence, anchors, and the
    candidate driver's bound-mode marker."""
    sha, parent = manifest.get("sha"), manifest.get("parent_sha")
    if not (isinstance(sha, str) and rg.SHA.fullmatch(sha) and isinstance(parent, str) and rg.SHA.fullmatch(parent)):
        raise rg.Refused("manifest names no valid commit")
    try:
        _git(cfg, "fetch", "--no-tags", "--quiet", cfg.fetch_url,
             f"+refs/heads/{cfg.branch}:refs/remotes/origin/{cfg.branch}")
    except (OSError, subprocess.CalledProcessError) as error:
        raise RetryLater(f"fetch failed: {type(error).__name__}") from None
    on_branch = subprocess.run(["git", "-C", str(cfg.clone), "merge-base", "--is-ancestor", sha,
                                f"refs/remotes/origin/{cfg.branch}"], capture_output=True).returncode == 0
    if not on_branch:
        raise rg.Refused(f"commit {sha} is not on {cfg.branch} (another repository or branch, or unknown)")
    last = rg.chain_parent(ledger)
    descends = bool(last) and subprocess.run(["git", "-C", str(cfg.clone), "merge-base", "--is-ancestor", last, sha],
                                             capture_output=True).returncode == 0
    # What changes is computed from what RUNS (ledger last_sha), never from the manifest's word; after a rebase the
    # running commit comes from the other history (fetched into this clone by the cutover) and only the payload counts.
    running, scope = ledger.get("last_sha") or parent, rg.diff_scope(ledger)
    tar = rg.release_archive(cfg.clone, sha)           # shipped bytes == hashed bytes, refused BEFORE pending
    digest = rg.inputs_digest(tar)
    try:
        host_compose = hashlib.sha256(HOST_COMPOSE.read_bytes()).hexdigest()
    except OSError:
        host_compose = None
    try:                                               # a v2 manifest must never reach a pre-binding driver
        driver = _git(cfg, "show", f"{sha}:scripts/deploy-company-os.sh")
        bound = DRIVER_BOUND_IMAGES in driver
    except (OSError, subprocess.CalledProcessError):
        bound = False
    wanted = manifest.get("images") if rg.images_ok(manifest.get("images")) else ledger.get("bound_images")
    if not rg.images_ok(wanted):
        record, _ = read_build_record(digest)
        wanted = record.get("images") if isinstance(record, dict) else None
    record, record_problem = read_build_record(digest)
    facts = {"repository": cfg.repository if on_branch else None, "branch": cfg.branch if on_branch else None,
             "sha": sha, "payload_digest": rg.payload_digest(rg.payload_listing(cfg.clone, sha)),
             "changed_paths": rg.git_changed_paths(cfg.clone, running, sha, scope), "ancestor_of_parent": descends,
             "diff": rg.git_release_diff(cfg.clone, running, sha, scope),
             "compose": {"host": host_compose, "parent": rg.git_blob_sha256(cfg.clone, running, "docker-compose.yml"),
                         "release": rg.git_blob_sha256(cfg.clone, sha, "docker-compose.yml")},
             "migration_heads": sorted(rg.alembic_heads(cfg.clone, sha)),
             "parent_migration_heads": sorted(rg.alembic_heads(cfg.clone, running)),
             "nonregular_paths": rg.git_nonregular_paths(cfg.clone, running, sha, scope),
             "inputs_digest": digest, "build_record": record, "build_record_problem": record_problem,
             "driver_bound": bound, "driver_sha256": rg.git_blob_sha256(cfg.clone, sha, "scripts/deploy-company-os.sh"),
             "images_present": images_present(wanted, docker) if rg.images_ok(wanted) else False,
             "anchored_images": images_anchored(wanted, docker) if rg.images_ok(wanted) else False}
    return facts


def deploy_acknowledgements(manifest: dict, facts: dict) -> str:
    """B-7/B-8, decided BEFORE `pending`. Returns the driver's ACK_COMPOSE ("" when compose is unchanged).
    - The host's docker-compose.yml must be exactly the applied release's (parent_sha): anything else is a manual
      change nobody signed, so nothing is applied (BLOCKED, the manifest stays).
    - A compose change ships only in a founder-signed release (signature checked in the transaction; compose is
      always HIGH, rg.FOUNDER_ONLY_PATHS). The driver then gets the host file's own short hash, which it recomputes
      and compares itself; the new file's bytes are bound by payload_digest.
    - A symlink or submodule entry anywhere in the diff needs the founder's signature, whatever its path.
    - A release that moves the Alembic head must name, in migration_to, the single head the host reads at that
      commit; a migration_to that is not that head is refused too (the driver would abort after `pending`)."""
    if facts["nonregular_paths"] and manifest.get("approver") != "founder":
        raise rg.Refused("a symlink or submodule change needs the founder's signature: "
                         + ", ".join(facts["nonregular_paths"])[:200])
    compose = facts["compose"]
    if not compose["host"] or compose["host"] != compose["parent"]:
        raise rg.Refused("BLOCKED: the host's docker-compose.yml is not the applied release's; "
                         "the founder reconciles the host before any release")
    ack = ""
    if compose["release"] != compose["parent"]:
        if manifest.get("approver") != "founder":
            raise rg.Refused("a docker-compose.yml change needs the founder's signature")
        ack = compose["host"][:12]
    migration_to = manifest.get("migration_to")
    schema_change = facts["migration_heads"] != facts["parent_migration_heads"]
    if (schema_change or migration_to) and facts["migration_heads"] != [migration_to]:
        raise rg.Refused(f"migration_to must be the single Alembic head at this commit "
                         f"({', '.join(facts['migration_heads']) or 'none'}), signed by the founder")
    return ack


def build_release(cfg: Config, sha: str, directory: Path) -> None:
    """The staged transport's three files, from the host clone at exactly `sha` (same commands as the driver).
    The root-side driver comes from the installed bundle, never the candidate tree. Only the explicit founder
    `prepare --bootstrap` operation may execute staged code; applying a release always uses the installed bundle."""
    try:
        tar = rg.release_archive(cfg.clone, sha)      # checked in host_facts already; again here, after pending
    except rg.Refused as error:
        raise RuntimeError(str(error)) from None      # after `pending`: APPLY_FAILED, never a silent reject
    listing = rg.payload_listing(cfg.clone, sha)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(tar)
    (directory / "src.tgz").write_bytes(buf.getvalue())
    (directory / "main.sha256").write_text(listing)
    try:
        driver = _bounded_read(BUNDLE.resolve(strict=True) / "scripts" / "deploy-company-os.sh", 64 << 20,
                               trusted_file=True).decode("utf-8", "surrogateescape")
    except (OSError, rg.Refused) as error:
        raise RuntimeError(f"the installed deploy driver is unreadable ({type(error).__name__})") from None
    if REMOTE_START not in driver or REMOTE_END not in driver:
        raise rg.Refused("the installed deploy driver has no root-side script")
    remote = driver[driver.index(REMOTE_START) + len(REMOTE_START):driver.index(REMOTE_END)]
    (directory / "company-os-deploy.sh").write_text(remote + "\n")
    for name in ("src.tgz", "main.sha256", "company-os-deploy.sh"):
        os.chmod(directory / name, 0o600)


CONTAINERS = {"api": "company-api", "worker": "company-worker", "reasoner": "company-reasoner",
              "egress": "company-reasoner-egress"}

def observe_production(run=subprocess.run) -> dict:
    """What actually runs, read by root from Docker on this host: per component the content-addressed image ID
    (`.Image`, immutable), the security_config digest, and COMPANY_OS_GIT_SHA from the container's config (a
    consistency check, not identity). `--type container`: an image with the same name is never inspected.
    A component that cannot be inspected is left out, so the identity check fails closed."""
    observed = {}
    for name, container in CONTAINERS.items():
        try:
            out = run(["docker", "inspect", "--type", "container", "--format", "{{json .}}", container],
                      capture_output=True, text=True, timeout=30, check=True).stdout
            info = json.loads(out)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if not isinstance(info, dict):
            continue
        env = dict(e.split("=", 1) for e in (info.get("Config") or {}).get("Env") or [] if "=" in e)
        if (info.get("State") or {}).get("Running") is True:
            observed[name] = {"image": info.get("Image"), "config": security_config(info),
                              "git_sha": env.get("COMPANY_OS_GIT_SHA")}
    return observed


def host_id() -> str:
    try:
        return Path("/etc/machine-id").read_text().strip()
    except OSError:
        return ""


def write_identity_report(cfg: Config, now: datetime, observed: dict) -> dict:
    """Root's own verdict on production identity, for the gateway to READ (never write): fresh, and whether
    production is exactly the ledger's release. The gateway cannot fix a mismatch; only the founder can."""
    try:
        ledger = rg.read_ledger(cfg.ledger)
        problem = ((rg.identity_problem(observed, ledger.get("last_sha"), ledger.get("images"), ledger.get("configs"))
                    or rg.history_problem(ledger, rg.read_history(cfg.ledger)))
                   if ledger.get("last_sha") else "host is not bootstrapped")
    except rg.Refused as error:
        ledger, problem = {}, str(error)
    report = {"checked_at": now.isoformat(), "ok": problem is None, "problem": problem,
              "last_sha": ledger.get("last_sha"), "pending": bool(ledger.get("pending"))}
    rg._write_atomic(cfg.identity_report, report)
    return report


def default_run(env: dict, script: Path) -> int:
    """The existing root-side deploy, attached (not DETACH): its exit code is the release result."""
    return subprocess.run(["bash", str(script)], env={**os.environ, **env}).returncode


def default_spawn(env: dict, script: Path) -> tuple[int, str]:
    """Run a release command and capture its output (prepare needs the driver's BOUND line)."""
    result = subprocess.run(["bash", str(script)], env={**os.environ, **env}, capture_output=True, text=True)
    return result.returncode, result.stdout


# --------------------------------------------------------------------------- file locks

def _ensure_builds(cfg: Config) -> None:
    problem = untrusted_path(BUILDS)
    if problem:
        raise rg.Refused(f"BLOCKED: {problem}")
    try:
        BUILDS.mkdir(mode=0o750, parents=True, exist_ok=True)
        os.chown(BUILDS, os.geteuid(), cfg.ledger.parent.stat().st_gid)
        os.chmod(BUILDS, 0o2750)
    except OSError as error:
        raise RetryLater(f"could not create the build record directory: {type(error).__name__}") from None
    problem = untrusted_path(BUILDS)
    if problem:
        raise rg.Refused(f"BLOCKED: {problem}")


@contextmanager
def _lock(path: Path, *, shared: bool = False):
    """A root-only lock file. `shared` for every release command (the installer takes it exclusive), exclusive for
    the build records and the ledger-like serialisation. Waiting is bounded by rg.LOCK_TIMEOUT_S; on timeout nothing
    has been read or written and the caller gets LockBusy."""
    trusted = {0, os.geteuid()}
    directory = os.stat(path.parent)
    if directory.st_uid not in trusted or directory.st_mode & 0o022:
        raise rg.Refused(f"BLOCKED: lock directory {path.parent} is not root-only")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in trusted:
            raise rg.Refused("BLOCKED: lock is not a root-owned regular file with one name")
        os.fchmod(fd, 0o600)
        mode = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
        deadline = time.monotonic() + rg.LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, mode)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise rg.LockBusy("a release command holds the lock: nothing was done; retry later") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- prepare (pre-sign builds)

def _recipe_changed(cfg: Config, running: str, sha: str) -> bool:
    """The timer builds only when the RUN steps are the reviewed recipe production runs: Dockerfile and
    requirements.txt byte-identical between the running release and the candidate (design §4.2)."""
    for path in ("Dockerfile", "requirements.txt"):
        if rg.git_blob_sha256(cfg.clone, running, path) != rg.git_blob_sha256(cfg.clone, sha, path):
            return True
    return False


def _bounds_line(output: str) -> dict | None:
    """The driver's PREPARE result line: `BOUND inputs=<64 hex> api=<ID> worker=<ID> reasoner=<ID> egress=<ID>`."""
    for line in reversed(output.splitlines()):
        fields = line.strip().split()
        if not fields or fields[0] != "BOUND":
            continue
        values = {}
        for field in fields[1:]:
            key, sep, value = field.partition("=")
            if not sep or key in values:
                return None
            values[key] = value
        return values if set(values) == {"inputs", *rg.BOUND} else None
    return None


def prepare(cfg: Config, sha: str, *, founder: bool = False, retry: bool = False, bootstrap: bool = False,
            now: datetime | None = None, run=default_spawn, docker=subprocess.run) -> dict:
    """Build the four images for one commit's image inputs and write the one build record for them. The timer calls
    this only for an unchanged recipe; `--retry` and `--bootstrap` are founder-only. Write-once per attempt: a
    crashed `building` record is never continued by the timer, only moved aside by `--retry`."""
    if (retry or bootstrap) and not founder:
        raise rg.Refused("--retry and --bootstrap are founder-only (run as root with --founder)")
    now = now or datetime.now(timezone.utc)
    problem = untrusted_layout(cfg)
    if problem:
        raise rg.Refused(f"BLOCKED: {problem}")
    try:
        with _lock(INSTALL_LOCK, shared=True):
            _ensure_builds(cfg)
            with _lock(BUILDS / ".lock"):
                out = _prepare_locked(cfg, sha, founder=founder, retry=retry, bootstrap=bootstrap, now=now, run=run,
                                      docker=docker)
                gc_anchors(cfg, rg.read_ledger(cfg.ledger), now=now, docker=docker)
        return out
    except rg.LockBusy:
        raise


def _prepare_locked(cfg: Config, sha: str, *, founder: bool, retry: bool, bootstrap: bool, now: datetime,
                    run=default_spawn, docker=subprocess.run) -> dict:
    problem = bundle_problem(allow_staged=founder and bootstrap)
    if problem:
        raise rg.Refused(f"BLOCKED: {problem}")
    if not isinstance(sha, str) or not rg.SHA.fullmatch(sha):
        raise rg.Refused("prepare needs a 40-hex commit")
    state = rg.read_ledger(cfg.ledger)
    if state.get("pending"):
        raise rg.Refused("a release is pending: the founder must reconcile it before prepare")
    if not state.get("last_sha"):
        raise rg.Refused("host is not bootstrapped: the founder must sign the production baseline first")
    if not founder and _recipe_changed(cfg, state["last_sha"], sha):
        raise rg.Refused("this commit changes Dockerfile or requirements.txt: only the founder may build it "
                         "(`python3 -m app.release_host prepare --founder <sha>`)")
    try:
        _git(cfg, "fetch", "--no-tags", "--quiet", cfg.fetch_url,
             f"+refs/heads/{cfg.branch}:refs/remotes/origin/{cfg.branch}")
    except (OSError, subprocess.CalledProcessError) as error:
        raise RetryLater(f"fetch failed: {type(error).__name__}") from None
    if subprocess.run(["git", "-C", str(cfg.clone), "merge-base", "--is-ancestor", sha,
                       f"refs/remotes/origin/{cfg.branch}"], capture_output=True).returncode != 0:
        raise rg.Refused(f"commit {sha} is not on {cfg.branch}: nothing is built")
    parent = rg.chain_parent(state)
    if parent and subprocess.run(["git", "-C", str(cfg.clone), "merge-base", "--is-ancestor", parent, sha],
                                 capture_output=True).returncode != 0:
        raise rg.Refused("the commit does not descend from the release currently applied: nothing is built")
    tar = rg.release_archive(cfg.clone, sha)          # P2-5: the shipped bytes are refused before a build
    digest = rg.inputs_digest(tar)
    path = _build_record_path(digest)
    attempt = 1
    if path.exists():
        previous, previous_problem = read_build_record(digest)
        if previous_problem or not isinstance(previous, dict):
            raise rg.Refused(f"BLOCKED: {previous_problem or 'the existing build record is unreadable'}")
        if not retry:
            raise rg.Refused(f"a build record already exists for these image inputs (attempt {previous['attempt']}); "
                             "the founder may run prepare --retry")
        attempt = previous["attempt"] + 1
        attempts = BUILDS / "attempts"
        attempts.mkdir(mode=0o750, parents=True, exist_ok=True)
        os.chmod(attempts, 0o750)
        # Kept for audit, never deleted: the attempt number is never reused and its anchors never collide.
        os.replace(path, attempts / f"{digest}.{previous['attempt']}.json")
    else:
        if retry:
            attempt = 1
    base = {"inputs_digest": digest, "built_from_sha": sha, "repository": cfg.repository,
            "recipe": "founder" if founder else "auto", "bootstrap": bool(bootstrap), "attempt": attempt,
            "built_at": now.isoformat(), "status": "building"}
    rg._write_atomic(path, base)                      # published BEFORE the first build command
    cfg.run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    d = cfg.run_root / _release_dir_name(sha)
    d.mkdir(mode=0o700)
    try:
        build_release(cfg, sha, d)
        code, output = run({"SHA": sha, "PREPARE": "1", "ATTEMPT": str(attempt), "AUTOMATIC": "false",
                            "ALLOW_MIGRATION": "", "ACK_OVERRIDES": "", "ACK_COMPOSE": "", "RELEASE_DIR": str(d)},
                           d / "company-os-deploy.sh")
    except BaseException:
        # No clean result: the record stays `building` (a crash may have left anchors this attempt alone may name).
        raise
    finally:
        if d.exists():
            shutil.rmtree(d)
    if code != 0:
        rg._write_atomic(path, {**base, "status": "failed"})
        raise rg.Refused(f"the pre-sign build failed (driver exited {code})")
    bounds = _bounds_line(output or "")
    if not bounds or bounds["inputs"] != digest or not rg.images_ok({name: bounds[name] for name in rg.BOUND}):
        rg._write_atomic(path, {**base, "status": "failed"})
        raise rg.Refused("the pre-sign build printed no usable image binding")
    images = {name: bounds[name] for name in rg.BOUND}
    if not images_present(images, docker):
        rg._write_atomic(path, {**base, "status": "failed"})
        raise rg.Refused("a built image is not present locally")
    ready = {**base, "status": "ready", "images": images}
    rg._write_atomic(path, ready)
    return {"outcome": "PREPARED", "inputs_digest": digest, "images": images, "attempt": attempt}


def _auto_prepare(cfg: Config, *, now: datetime, run, docker) -> None:
    """Best-effort, silent: build origin/main before it is signed, but only for an unchanged recipe and only while
    nothing is pending or queued. The timer never builds a recipe change and never rebuilds a recorded attempt."""
    try:
        state = rg.read_ledger(cfg.ledger)
        if not state.get("last_sha") or state.get("pending"):
            return
        _git(cfg, "fetch", "--no-tags", "--quiet", cfg.fetch_url,
             f"+refs/heads/{cfg.branch}:refs/remotes/origin/{cfg.branch}")
        sha = _git(cfg, "rev-parse", f"refs/remotes/origin/{cfg.branch}^{{commit}}").strip()
        if sha == rg.chain_parent(state) or _recipe_changed(cfg, state["last_sha"], sha):
            return
        _prepare_locked(cfg, sha, founder=False, retry=False, bootstrap=False, now=now, run=run, docker=docker)
    except (rg.Refused, RetryLater, OSError, subprocess.SubprocessError, ValueError):
        return


# --------------------------------------------------------------------------- retention

def _young_record_images(now: datetime) -> set[str]:
    keep = set()
    problem = untrusted_path(BUILDS)
    if problem:
        return keep
    for path in BUILDS.glob("*.json"):
        if not rg.DIGEST.fullmatch(path.stem):
            continue
        record, record_problem = read_build_record(path.stem)
        if record_problem or not isinstance(record, dict) or not rg.images_ok(record.get("images")):
            continue
        try:
            built = datetime.fromisoformat(record["built_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if built.tzinfo is None:
            built = built.replace(tzinfo=timezone.utc)
        if now - built <= BUILD_TTL:
            keep.update(record["images"].values())
    return keep


def gc_anchors(cfg: Config, ledger: dict, *, now: datetime, docker=subprocess.run) -> None:
    """Drop anchor containers whose image is in no kept set. Never removes an image (`docker rmi` is never called);
    Coolify's cleanup does that once nothing holds it. Kept: the four images running now, the ledger's bound set,
    pending_images, the last KEEP_APPLIED effective-applied bound sets, and every unexpired build record."""
    keep = set()
    try:
        running = observe_bound(docker)
    except RetryLater:
        return                                         # cannot tell what runs: touch nothing this step
    if running:
        keep.update(running.values())
    for key in ("bound_images", "pending_images"):
        value = ledger.get(key)
        if rg.images_ok(value):
            keep.update(value.values())
    try:
        applied = [e.get("bound_images") for e in rg.effective_applied(rg.read_history(cfg.ledger))]
    except rg.Refused:
        return
    for bound in [b for b in applied if rg.images_ok(b)][-KEEP_APPLIED:]:
        keep.update(bound.values())
    keep.update(_young_record_images(now))
    result = _bounded_call(docker, ["ps", "-a", "--no-trunc", "--filter", "name=company-os-bound-",
                                    "--format", "{{.Names}}\t{{.Image}}"])
    if result.returncode != 0:
        return
    for line in result.stdout.splitlines():
        name, sep, image = line.partition("\t")
        if not sep or not name.startswith("company-os-bound-"):
            continue
        if image and image not in keep:
            _bounded_call(docker, ["rm", "-f", name])


# --------------------------------------------------------------------------- the step

def step(cfg: Config, *, now: datetime | None = None, run=default_run, observe=observe_production,
         docker=subprocess.run, spawn=default_spawn) -> dict:
    """Apply at most one release. Returns {"outcome": ...}. A refused manifest is moved to `rejected` with its
    reason, so it is never retried; an apply failure leaves it in place and the ledger pending. When idle, prepare
    origin/main (an unchanged recipe only) and drop unkept anchors. Holds the install lock shared and the build lock
    exclusive for the whole step: prepare, GC and apply never overlap."""
    now = now or datetime.now(timezone.utc)
    try:
        with _lock(INSTALL_LOCK, shared=True):
            _ensure_builds(cfg)
            with _lock(BUILDS / ".lock"):
                return _step(cfg, now=now, run=run, observe=observe, docker=docker, spawn=spawn)
    except rg.LockBusy as error:
        return {"outcome": "RETRY_LATER", "reason": str(error)}
    except rg.Refused as error:
        return {"outcome": "BLOCKED", "reason": f"{error}"}


def _step(cfg: Config, *, now: datetime, run, observe, docker, spawn) -> dict:
    problem = bundle_problem()
    if problem:
        return {"outcome": "BLOCKED", "reason": problem}
    founder_key = cfg.founder_key.read_text().strip()
    problem = untrusted_layout(cfg)
    if problem:
        # Root would read, move or write through a link or a directory someone else controls: nothing is touched,
        # not even the identity report.
        return {"outcome": "BLOCKED", "reason": problem}
    report = write_identity_report(cfg, now, observe())
    if not report["ok"] and not report["pending"]:
        # Production is not exactly the recorded release (or there is no baseline): nothing is applied, moved or
        # discarded; the founder investigates (a legacy deploy, a manual change) and recovers or bootstraps.
        return {"outcome": "BLOCKED", "reason": report["problem"]}
    if rg.read_ledger(cfg.ledger).get("pending"):
        # A started release was never recorded: nothing else is applied, and nothing is moved or discarded.
        return {"outcome": "WAITING_FOR_FOUNDER", "reason": "a previous release is pending reconciliation"}
    queue = candidates(cfg)
    if not queue:
        _auto_prepare(cfg, now=now, run=spawn, docker=docker)
        gc_anchors(cfg, rg.read_ledger(cfg.ledger), now=now, docker=docker)
        return {"outcome": "IDLE"}
    _, path = queue[0]
    sig_path = path.with_suffix(".sig")
    raw = None
    try:
        try:
            raw = _read_inbox(path)
            signature = _read_inbox(sig_path).decode()
        except OSError as error:                          # missing or unreadable entry: the entry is invalid
            raise rg.Refused(f"manifest or signature unreadable: {type(error).__name__}") from None
        manifest = json.loads(raw)
        if not isinstance(manifest, dict):
            raise rg.Refused("manifest is not an object")
        ledger = rg.read_ledger(cfg.ledger)
        facts = host_facts(cfg, manifest, ledger, docker)
        ack_compose = deploy_acknowledgements(manifest, facts)
        try:
            policy_bytes, policy_sig = cfg.policy.read_bytes(), cfg.policy_sig.read_text()
        except OSError as error:
            raise RetryLater(f"release policy unreadable: {type(error).__name__}") from None

        # Named before the transaction marks it pending (pending_release_dir): the driver accepts only this directory.
        d = cfg.run_root / _release_dir_name(str(manifest.get("sha", "")))

        def apply(m: dict) -> None:
            if d.name.split(".")[0] != m["sha"]:                # verified manifest == the one the name was chosen for
                raise RuntimeError("staged directory was named for another release")
            cfg.run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            d.mkdir(mode=0o700)                                  # fresh, root-only; never an existing path
            try:
                build_release(cfg, m["sha"], d)
                code = run({"SHA": m["sha"], "APPLY": "--apply", "DETACH": "", "AUTOMATIC": "true",
                            "ALLOW_MIGRATION": m["migration_to"], "ACK_OVERRIDES": "", "ACK_COMPOSE": ack_compose,
                            "RELEASE_DIR": str(d)}, d / "company-os-deploy.sh")
            finally:
                if d.exists():                                   # the driver removes it itself when it ends
                    shutil.rmtree(d)
            if code != 0:
                raise RuntimeError(f"deploy driver exited {code}")

        m = rg.release_transaction(cfg.ledger, raw, signature, policy_bytes=policy_bytes, policy_sig=policy_sig,
                                   founder_key=founder_key, host_facts=facts, now=now, apply=apply, observe=observe,
                                   release_dir=str(d))
    except Exception as error:
        # F2: once `pending` names this manifest, whatever went wrong (driver, build, cleanup, the record itself)
        # is an apply failure. The manifest and its signature stay where they are as evidence; the founder reconciles.
        if raw is not None and _pending_is(cfg, raw):
            write_identity_report(cfg, now, observe())
            return {"outcome": "APPLY_FAILED", "manifest": path.name, "reason": f"{type(error).__name__}: {error}"[:500]}
        if isinstance(error, rg.LockBusy):
            return {"outcome": "RETRY_LATER", "manifest": path.name, "reason": str(error)}
        if isinstance(error, rg.Refused) and str(error).startswith(("BLOCKED", "host is not bootstrapped")):
            # Identity changed between the report and the lock: nothing applied, the manifest stays for later.
            write_identity_report(cfg, now, observe())
            return {"outcome": "BLOCKED", "reason": str(error)}
        if isinstance(error, (rg.Refused, ValueError)):
            return _reject(cfg, path, sig_path, error)           # the manifest itself is invalid: never retried
        # Infrastructure before pending (fetch, git, policy file, anything unexpected): retried, nothing moved.
        return {"outcome": "RETRY_LATER", "manifest": path.name, "reason": f"{type(error).__name__}: {error}"[:500]}
    os.replace(path, path.with_suffix(".applied"))
    gc_anchors(cfg, rg.read_ledger(cfg.ledger), now=now, docker=docker)
    write_identity_report(cfg, now, observe())
    return {"outcome": "APPLIED", "sha": m["sha"], "sequence": m["sequence"], "approver": m["approver"]}


def _pending_is(cfg: Config, raw: bytes) -> bool:
    try:
        return rg.read_ledger(cfg.ledger).get("pending") == hashlib.sha256(raw).hexdigest()
    except rg.Refused:
        return True                                      # unreadable ledger after a start: treat as pending (fail closed)


def _reject(cfg: Config, path: Path, sig_path: Path, error: Exception) -> dict:
    cfg.rejected.mkdir(parents=True, exist_ok=True)
    reason = f"{type(error).__name__}: {error}"[:500]
    os.replace(path, cfg.rejected / path.name)
    if sig_path.exists():
        os.replace(sig_path, cfg.rejected / sig_path.name)
    # Written to a fresh file and renamed into place: a planted symlink at the name is replaced, never followed.
    fd, tmp = tempfile.mkstemp(dir=cfg.rejected, prefix=f".{path.stem}.reason.")
    with os.fdopen(fd, "w") as f:
        f.write(reason + "\n")
    os.replace(tmp, cfg.rejected / f"{path.stem}.reason")
    return {"outcome": "REJECTED", "manifest": path.name, "reason": reason}


def _founder_common(cfg: Config) -> None:
    problem = bundle_problem()
    if problem:
        raise rg.Refused(f"BLOCKED: {problem}")
    problem = untrusted_layout(cfg)
    if problem:
        raise rg.Refused(problem)


def founder_statement(cfg: Config, kind: str, statement: Path, signature: Path, *, now=None,
                      observe=observe_production, machine_id=None) -> dict:
    """`bootstrap`, `recover` or `reconcile`, each with its own founder signature namespace; never run by a timer.
    After a reconcile, the pending manifest is renamed .applied or .failed (abandoned counts as failed)."""
    action = {"bootstrap": rg.bootstrap, "recover": rg.recover, "reconcile": rg.reconcile}[kind]
    now = now or datetime.now(timezone.utc)
    with _lock(INSTALL_LOCK, shared=True):
        _founder_common(cfg)
        pending = rg.read_ledger(cfg.ledger).get("pending") if kind == "reconcile" else None
        state = action(cfg.ledger, statement.read_bytes(), signature.read_text(),
                       founder_key=cfg.founder_key.read_text().strip(), policy_bytes=cfg.policy.read_bytes(),
                       policy_sig=cfg.policy_sig.read_text(), host_id=host_id() if machine_id is None else machine_id,
                       observe=observe, now=now)
    if pending:
        outcome = json.loads(statement.read_bytes())["outcome"]
        for box in cfg.inboxes:
            for path in (p for p in box.glob("*.json") if _regular(p)) if box.is_dir() else []:
                try:
                    match = hashlib.sha256(_read_inbox(path)).hexdigest() == pending
                except (OSError, rg.Refused):
                    match = False
                if match:
                    os.replace(path, path.with_suffix(".applied" if outcome == "applied" else ".failed"))
    write_identity_report(cfg, now, observe())
    return {"outcome": kind.upper() + "_DONE", "last_sha": state["last_sha"]}


def rebase_provenance(cfg: Config, old: Config):
    """The host's own git evidence for a rebase: the new clone (this config, freshly fetched) holds the base commit on
    its branch and the running commit's objects; the old clone (the previous config) recomputes the running payload."""
    def facts(base_sha: str, old_sha: str) -> dict:
        try:
            _git(cfg, "fetch", "--no-tags", "--quiet", cfg.fetch_url,
                 f"+refs/heads/{cfg.branch}:refs/remotes/origin/{cfg.branch}")
        except (OSError, subprocess.CalledProcessError) as error:
            raise RetryLater(f"fetch failed: {type(error).__name__}") from None

        # The running commit comes from the current chain's own (trusted) clone into the new one, also when going
        # back to a repository that never had it; it is then checked by object name and by recomputed payload.
        subprocess.run(["git", "-C", str(cfg.clone), "fetch", "--no-tags", "--quiet", str(old.clone),
                        f"+refs/remotes/origin/{old.branch}:refs/old/{old.branch}"], capture_output=True)
        on_branch = subprocess.run(["git", "-C", str(cfg.clone), "merge-base", "--is-ancestor", base_sha,
                                    f"refs/remotes/origin/{cfg.branch}"], capture_output=True).returncode == 0
        old_in_new = subprocess.run(["git", "-C", str(cfg.clone), "cat-file", "-e", f"{old_sha}^{{commit}}"],
                                    capture_output=True).returncode == 0
        try:
            old_payload = rg.payload_digest(rg.payload_listing(old.clone, old_sha))
        except subprocess.CalledProcessError:
            old_payload = None
        return {"old_repository": old.repository, "base_on_branch": on_branch, "old_in_new": old_in_new,
                "old_payload_digest": old_payload}
    return facts


def rebase_statement(cfg: Config, old: Config, statement: Path, signature: Path, *, now=None,
                     observe=observe_production, machine_id=None) -> dict:
    """`rebase STATEMENT SIG OLD_CONFIG`: run after this config (and its new policy) names the new repository; the
    old config still names the old clone. Never run by a timer. Nothing is deployed."""
    now = now or datetime.now(timezone.utc)
    with _lock(INSTALL_LOCK, shared=True):
        _founder_common(cfg)
        problem = untrusted_path(old.clone) or untrusted_path(cfg.clone)
        if problem:
            raise rg.Refused(problem)
        if old.ledger != cfg.ledger:
            raise rg.Refused("the old and new host configs must name the same ledger")
        state = rg.rebase(cfg.ledger, statement.read_bytes(), signature.read_text(),
                          founder_key=cfg.founder_key.read_text().strip(), policy_bytes=cfg.policy.read_bytes(),
                          policy_sig=cfg.policy_sig.read_text(), host_id=host_id() if machine_id is None else machine_id,
                          observe=observe, provenance=rebase_provenance(cfg, old), now=now)
    write_identity_report(cfg, now, observe())
    return {"outcome": "REBASE_DONE", "last_sha": state["last_sha"], "chain_base": state["chain_base"]}


def _prepare_args(argv: list[str]) -> tuple[str, bool, bool, bool]:
    flags, rest = {a for a in argv if a.startswith("--")}, [a for a in argv if not a.startswith("--")]
    unknown = flags - {"--founder", "--retry", "--bootstrap"}
    if unknown or len(rest) != 1:
        raise rg.Refused("usage: prepare [--founder] [--retry] [--bootstrap] <sha>")
    retry, bootstrap = "--retry" in flags, "--bootstrap" in flags
    founder = "--founder" in flags
    if (retry or bootstrap) and not founder:
        raise rg.Refused("--retry and --bootstrap are founder-only")
    return rest[0], founder, retry, bootstrap


def main() -> int:
    path = Path(os.environ.get("RELEASE_HOST_CONFIG", "/etc/release-host/config.json"))
    try:
        cfg = Config(json.loads(path.read_text()))
        if sys.argv[1:] == ["observe"]:                # read-only: what a founder statement must name
            with _lock(INSTALL_LOCK, shared=True):
                problem = bundle_problem()
                if problem:
                    raise rg.Refused(f"BLOCKED: {problem}")
                print(json.dumps(observe_production(), sort_keys=True))
            return 0
        if len(sys.argv) == 5 and sys.argv[1] == "rebase":
            old = Config(json.loads(Path(sys.argv[4]).read_text()))
            print(json.dumps(rebase_statement(cfg, old, Path(sys.argv[2]), Path(sys.argv[3])), sort_keys=True))
            return 0
        if len(sys.argv) == 4 and sys.argv[1] in ("bootstrap", "recover", "reconcile"):
            out = founder_statement(cfg, sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
            print(json.dumps(out, sort_keys=True))
            return 0
        if len(sys.argv) >= 2 and sys.argv[1] == "prepare":
            sha, founder, retry, bootstrap = _prepare_args(sys.argv[2:])
            out = prepare(cfg, sha, founder=founder, retry=retry, bootstrap=bootstrap)
            print(json.dumps(out, sort_keys=True))
            return 0
        out = step(cfg)
    except (rg.Refused, OSError, ValueError) as error:
        print(f"release-host: refused: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(json.dumps(out, sort_keys=True))
    return 0 if out["outcome"] in ("IDLE", "APPLIED") else 1


if __name__ == "__main__":
    sys.exit(main())
