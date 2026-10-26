"""A disposable stand-in for the production host, to run the REAL root-side deploy script end to end.

Nothing here touches the machine it runs on: the script text is rewritten to point /opt/company-os, /var/lock and
/var/log into a private directory, and `docker` is a small stateful fake (images, containers, alembic revision,
failure and crash switches). The deploy logic under test is the script's own text, not a copy.
"""
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "scripts" / "deploy-company-os.sh").read_text()
REMOTE = SOURCE[SOURCE.index("<<'REMOTE'\n") + len("<<'REMOTE'\n"):SOURCE.index("\nREMOTE\n")]
INSTALL_CLI = (ROOT / "scripts" / "install-host-cli.sh").read_text()
VERIFY_PERMS = (ROOT / "scripts" / "verify-release-permissions.py").read_text()
ME = os.getuid()

FAKE_DOCKER = r'''#!%(python)s
import hashlib, json, os, re, signal, sys, time
STATE = os.environ["FAKE_STATE"]
LOG = os.environ["FAKE_LOG"]
args = sys.argv[1:]
with open(LOG, "a") as f:
    f.write(" ".join(args) + "\n")
def load():
    with open(STATE) as f: return json.load(f)
def save(s):
    with open(STATE, "w") as f: json.dump(s, f)
def fmt():
    return args[args.index("--format") + 1] if "--format" in args else ""
def rid(tag, s):
    if tag in s["tags"]: return s["tags"][tag]
    if tag in s["images"]: return tag
    sys.stderr.write("fake docker: no such image %%s\n" %% tag); sys.exit(1)
s = load()
cmd = args[0] if args else ""
knobs = s["knobs"]
def maybe_crash(point):
    if knobs.get("crash") == point:
        save(s)
        os.killpg(os.getpgrp(), signal.SIGKILL)
KIND = {"company-api": "api", "company-worker": "worker", "company-reasoner": "reasoner", "company-reasoner-egress": "egress"}

if cmd == "tag":
    s["tags"][args[2]] = rid(args[1], s); save(s); sys.exit(0)

def build_image(ctx, target, tag):
    maybe_crash("build")
    if knobs.get("block_build"):
        open(knobs["block_build"] + ".started", "w").close()
        while os.path.exists(knobs["block_build"]): time.sleep(0.05)
    if knobs.get("fail_build"):
        sys.stderr.write("fake docker: build failed\n"); sys.exit(1)
    files = {}
    # As the Dockerfile copies: these trees and one docs file; other docs never enter an image.
    for sub in ("app", "migrations", "scripts", "agents", "skills", "tests", "docs/PERMANENT_FACTS.md"):
        base = os.path.join(ctx, sub)
        if os.path.isfile(base):
            files[sub] = hashlib.sha256(open(base, "rb").read()).hexdigest()
        for d, _, names in os.walk(base):
            for n in names:
                p = os.path.join(d, n)
                files[os.path.relpath(p, ctx)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    for top in ("alembic.ini", "requirements.txt"):
        if os.path.exists(os.path.join(ctx, top)):
            files[top] = hashlib.sha256(open(os.path.join(ctx, top), "rb").read()).hexdigest()
    revs, downs = set(), set()
    vdir = os.path.join(ctx, "migrations", "versions")
    for n in sorted(os.listdir(vdir)) if os.path.isdir(vdir) else []:
        text = open(os.path.join(vdir, n)).read()
        m = re.search(r'^revision: str = "([0-9a-f]{12})"', text, re.M); d = re.search(r'^down_revision: str \| None = "([0-9a-f]{12})"', text, re.M)
        if m: revs.add(m.group(1))
        if d: downs.add(d.group(1))
    heads = sorted(revs - downs)
    # `drift` models a cold cache: apt/pip/npm fetch different bytes for the same inputs.
    img = "sha256:" + hashlib.sha256((target + json.dumps(files, sort_keys=True) + knobs.get("drift", "")).encode()).hexdigest()
    s["images"][img] = {"files": files, "target": target, "heads": heads}
    s["tags"][tag] = img
    return img

if cmd == "build":
    ctx = args[-1]; target = args[args.index("--target") + 1]; tag = args[args.index("-t") + 1]
    img = build_image(ctx, target, tag)
    save(s); print(img); sys.exit(0)

if args[:2] == ["image", "inspect"]:
    if fmt() == "{{.Id}}":
        print(rid(args[-1], s)); sys.exit(0)

if cmd == "inspect":
    name = args[1]; f = fmt()
    if name in s["containers"]:
        c = s["containers"][name]
        out = {"{{.Image}}": c["image"], "{{.State.Health.Status}}": c["health"], "{{.State.StartedAt}}": c["started"],
               "{{.State.Status}}": c["status"], "{{.State.Running}}": "true" if c["status"] == "running" else "false",
               '{{index .Config.Labels "com.docker.compose.project.config_files"}}': os.environ["FAKE_OPT"] + "/docker-compose.yml"}
        print(out[f]); sys.exit(0)
    if name.startswith("sha256:") and f == "{{index .RepoDigests 0}}":
        print("registry/img@" + name); sys.exit(0)
    sys.stderr.write("Error: No such object: %%s\n" %% name); sys.exit(1)

if cmd == "exec":
    rest = [a for a in args[1:] if a != "-i"]
    name = rest[0]
    if name == "company-postgres":
        if rest[1] == "psql":
            sql = rest[-1]
            if "alembic_version" in sql: print(s["rev"])
            sys.exit(0)
        if rest[1] == "pg_dump":
            sys.stdout.write("DUMP-OF-" + s["rev"]); sys.exit(0)
        if rest[1] == "pg_restore":
            if "--file=/dev/null" in rest:   # the full read-back of the pre-deploy dump
                sys.exit(1 if knobs.get("bad_dump") else 0)
            print("; TABLE DATA public tasks\n; TABLE DATA public audit_log"); sys.exit(0)
    if rest[1:] == ["printenv", "COMPANY_OS_GIT_SHA"]:
        print(s["containers"][name]["sha"]); sys.exit(0)
    sys.exit(0)

if cmd == "logs":
    sys.exit(0)

if cmd == "compose":
    sub = args[1]
    tag = os.environ.get("COMPANY_OS_IMAGE_TAG", "latest")
    if sub == "config":
        if "-q" in args:
            sys.exit(1 if knobs.get("bad_compose") else 0)
        cwd = os.path.realpath(".")
        # As real compose prints it: a build without a dockerfile is normalized to dockerfile: Dockerfile
        # (compose-go loader/normalize.go). `compose_build` replaces the api build to model other configurations.
        svc = {n: {"image": "company-os-%%s:%%s" %% (k, tag), "build": {"context": cwd, "dockerfile": "Dockerfile", "target": k}}
               for n, k in KIND.items()}
        if knobs.get("compose_build"):
            svc["company-api"]["build"] = {"context": cwd, **knobs["compose_build"]}
        print(json.dumps({"services": svc})); sys.exit(0)
    if sub == "build":      # the pre-clean-context flow: builds from the host tree (kept so that flow can be compared)
        tagv = os.environ.get("COMPANY_OS_IMAGE_TAG", "latest")
        for name in [a for a in args[2:] if a in KIND]:
            build_image(os.getcwd(), KIND[name], "company-os-%%s:%%s" %% (KIND[name], tagv))
        save(s); sys.exit(0)
    if sub == "run":
        img = rid("company-os-api:latest", s)
        if args[-2:] == ["alembic", "heads"]:
            for h in s["images"][img]["heads"]: print(h + " (head)")
            sys.exit(0)
        if args[-3:] == ["alembic", "upgrade", "head"]:
            maybe_crash("migrate")
            if knobs.get("fail_migrate"):
                sys.stderr.write("fake alembic: migration failed\n"); sys.exit(1)
            s["rev"] = s["images"][img]["heads"][0]; save(s); sys.exit(0)
    if sub == "up":
        sha = os.environ.get("COMPANY_OS_GIT_SHA", "")
        svcs = [a for a in args[2:] if a in KIND]
        for i, name in enumerate(svcs):
            image = rid("company-os-%%s:%%s" %% (KIND[name], tag), s)
            s["clock"] += 1
            s["containers"][name] = {"image": image, "sha": sha, "status": "running", "started": "t%%04d" %% s["clock"],
                                     "health": "unhealthy" if sha in knobs.get("unhealthy_for", []) else "healthy"}
            if i == 0 and knobs.get("crash") == "switch:" + sha:
                save(s); os.killpg(os.getpgrp(), signal.SIGKILL)
        save(s); sys.exit(0)
sys.stderr.write("fake docker: unsupported call: %%s\n" %% " ".join(args)); sys.exit(9)
'''

GATE = '''#!/usr/bin/env bash
# stand-in for scripts/deploy-health-gate.sh: healthy API + worker running exactly the expected SHA
set -euo pipefail
SHA=${1:?}
[ "$(docker inspect company-api --format '{{.State.Health.Status}}')" = healthy ] || { echo "RELEASE HEALTH FAILED: api" >&2; exit 1; }
[ "$(docker exec company-api printenv COMPANY_OS_GIT_SHA)" = "$SHA" ] || { echo "RELEASE HEALTH FAILED: api sha" >&2; exit 1; }
[ "$(docker exec company-worker printenv COMPANY_OS_GIT_SHA)" = "$SHA" ] || { echo "RELEASE HEALTH FAILED: worker sha" >&2; exit 1; }
echo "RELEASE HEALTH PASSED: $SHA"
'''

SHA256SUM = '''#!%(python)s
import hashlib, sys
args = [a for a in sys.argv[1:] if a not in ("--quiet", "-q")]
def digest(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()
if args and args[0] == "-c":
    bad = 0
    for line in open(args[1]):
        if not line.strip(): continue
        want, path = line.rstrip("\\n").split("  ", 1)
        try: ok = digest(path) == want
        except OSError: ok = False
        if not ok: print(path + ": FAILED"); bad += 1
    sys.exit(1 if bad else 0)
if not args:
    print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest() + "  -")
for p in args: print(digest(p) + "  " + p)
'''
FLOCK = '''#!%(python)s
import fcntl, sys
try: fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError: sys.exit(1)
'''
SETSID = '''#!%(python)s
import os, sys
try: os.setsid()
except OSError: pass
os.execvp(sys.argv[1], sys.argv[1:])
'''
CHOWN = "#!/bin/sh\nexit 0\n"      # the sandbox is not root; ownership is not what these tests prove


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def trusted_sandbox_parent():
    """A directory whose whole ancestor chain only root and this user can write (what trusted_dir demands)."""
    probe = REMOTE[REMOTE.index("trusted_dir() {"):REMOTE.index("\n}\n", REMOTE.index("trusted_dir() {")) + 3]
    for candidate in (Path.home(), ROOT, Path(tempfile.gettempdir())):
        try:
            work = Path(tempfile.mkdtemp(prefix=".release-sim-", dir=candidate)).resolve()
        except OSError:
            continue
        ok = subprocess.run(["bash", "-c", "set -euo pipefail\n" + probe + '\ntrusted_dir "$1" "$2"', "_", str(work), str(ME)],
                            capture_output=True).returncode == 0
        if ok:
            return work
        shutil.rmtree(work, ignore_errors=True)
    return None


class Release:
    """A payload exactly as the driver ships it: src.tgz + main.sha256 (sha256sum format, as `git ls-tree` builds it)."""
    def __init__(self, sha_: str, files: dict[str, tuple[bytes | str, int]]):
        self.sha, self.files = sha_, {p: ((c.encode() if isinstance(c, str) else c), m) for p, (c, m) in files.items()}

    def write(self, directory: Path):
        directory.mkdir(parents=True)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path, (content, mode) in sorted(self.files.items()):
                info = tarfile.TarInfo(path)
                info.size, info.mode, info.mtime = len(content), mode, 0
                tar.addfile(info, io.BytesIO(content))
        (directory / "src.tgz").write_bytes(buf.getvalue())
        (directory / "main.sha256").write_text("".join(f"{sha(c)}  {p}\n" for p, (c, _) in sorted(self.files.items())))
        return directory


def migration(rev: str, down: str | None) -> str:
    return f'revision: str = "{rev}"\ndown_revision: str | None = {json.dumps(down) if down else "None"}\n'


def base_files(**over):
    """A minimal but complete release tree: everything the Dockerfile copies, the CLI, the gate, the schedule."""
    cli = "#!/usr/bin/env bash\nexit 0\n"
    files = {
        "app/__init__.py": ("", 0o644), "app/main.py": ("print('v1')\n", 0o644),
        "migrations/versions/aaaaaaaaaaaa_init.py": (migration("aaaaaaaaaaaa", None), 0o644),
        "scripts/companyctl": (cli, 0o755), "scripts/stage-c-host-facts.py": ("", 0o644),
        "scripts/deploy-health-gate.sh": (GATE, 0o755),
        "scripts/install-host-cli.sh": (INSTALL_CLI, 0o755),
        "scripts/verify-release-permissions.py": (VERIFY_PERMS, 0o755),
        "scripts/company-os.cron": ("# schedule v1\n30 3 * * *   root  /opt/company-os/scripts/backup.sh\n", 0o644),
        "agents/registry.yaml": ("agents: []\n", 0o644), "skills/s/SKILL.md": ("s\n", 0o644),
        "tests/test_x.py": ("", 0o644), "alembic.ini": ("[alembic]\n", 0o644), "requirements.txt": ("pytest\n", 0o644),
        "Dockerfile": ("FROM scratch\n", 0o644), "docker-compose.yml": ("services: {}\n", 0o644),
        "docs/PERMANENT_FACTS.md": ("facts\n", 0o644),
    }
    files.update(over)
    return files


class Host:
    def __init__(self, parent: Path):
        self.parent = parent
        self.opt = parent / "opt" / "company-os"
        self.bin = parent / "fakebin"
        self.shims = parent / "shims"
        self.var = parent / "var"
        self.cron = parent / "etc-cron.d-company-os"
        self.cli_link = parent / "usr-local-bin" / "companyctl"
        self.ledger = parent / "var" / "lib" / "release-host" / "ledger" / "ledger.json"   # absent: not bootstrapped
        self.run_root = parent / "run" / "company-os-deploy"                                # release_host's staged copies
        self.state = parent / "docker-state.json"
        self.log = parent / "docker.log"
        for d in (self.opt.parent, self.bin, self.shims, self.var / "lock", self.var / "log", self.cli_link.parent):
            d.mkdir(parents=True, exist_ok=True)
        self.opt.parent.chmod(0o755)
        self.opt.mkdir(exist_ok=True)
        self.opt.chmod(0o755)
        for name, body in (("docker", FAKE_DOCKER), ("chown", CHOWN)):
            self._exe(self.bin / name, body % {"python": sys.executable} if "%(python)s" in body else body)
        for name, body in (("sha256sum", SHA256SUM), ("flock", FLOCK), ("setsid", SETSID)):
            self._exe(self.shims / name, body % {"python": sys.executable})
        (self.opt / ".env").write_text("POSTGRES_USER=company\nPOSTGRES_DB=company\n")
        self.releases = 0
        self.script = self._script_text()
        self.script_path = parent / "deploy.sh"
        self.script_path.write_text(self.script)

    @staticmethod
    def _exe(path: Path, body: str):
        path.write_text(body)
        path.chmod(0o755)

    def _script_text(self) -> str:
        text = REMOTE
        for old, new in (("/opt/company-os", str(self.opt)), ("/var/lock/company-os-deploy.lock", str(self.var / "lock" / "deploy.lock")),
                         ("/var/log/company-os-deploy", str(self.var / "log" / "company-os-deploy")),
                         ("/var/lib/release-host/ledger/ledger.json", str(self.ledger)), ("/run/company-os-deploy", str(self.run_root))):
            text = text.replace(old, new)
        assert "REL=/tmp\n" in text
        return text

    # -- state ---------------------------------------------------------------------------------------------
    def init_docker(self, current: Release, **knobs):
        """Containers running `current`, as a previous successful deploy would have left them."""
        images = {}
        tags = {}
        containers = {}
        for name, kind in (("company-api", "api"), ("company-worker", "worker"), ("company-reasoner", "reasoner"),
                           ("company-reasoner-egress", "egress")):
            img = "sha256:" + sha(f"{kind}-{current.sha}".encode())
            images[img] = {"files": {}, "target": kind, "heads": ["aaaaaaaaaaaa"]}
            tags[f"company-os-{kind}:latest"] = img
            tags[f"company-os-{kind}:{current.sha}"] = img
            containers[name] = {"image": img, "sha": current.sha, "status": "running", "started": "t0000", "health": "healthy"}
        for name in ("company-postgres", "company-cloudflared"):
            containers[name] = {"image": "sha256:" + sha(name.encode()), "sha": "", "status": "running", "started": "t0000", "health": "healthy"}
        self.state.write_text(json.dumps({"rev": "aaaaaaaaaaaa", "images": images, "tags": tags, "containers": containers,
                                          "clock": 0, "knobs": knobs}))
        self.log.write_text("")

    def docker_state(self):
        return json.loads(self.state.read_text())

    def set_knobs(self, **knobs):
        s = self.docker_state()
        s["knobs"] = knobs
        self.state.write_text(json.dumps(s))

    def sandboxed(self, release: Release) -> Release:
        """The payload as this sandbox needs it: the host-CLI installer's built-in paths point into the sandbox."""
        files = dict(release.files)
        if "scripts/install-host-cli.sh" in files:
            body, mode = files["scripts/install-host-cli.sh"]
            files["scripts/install-host-cli.sh"] = (body.decode().replace("/usr/local/bin/companyctl", str(self.cli_link))
                                                    .replace("/opt/company-os", str(self.opt)), mode)
        return Release(release.sha, files)

    def install_files(self, release: Release):
        """What a previous deploy left on the host (including root-extracted modes)."""
        release = self.sandboxed(release)
        for path, (content, mode) in release.files.items():
            p = self.opt / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
            p.chmod(mode)
        self.cli_link.unlink(missing_ok=True)
        self.cli_link.symlink_to(self.opt / "scripts" / "companyctl")
        self.cron.write_text(release.files["scripts/company-os.cron"][0].decode()) if "scripts/company-os.cron" in release.files else None

    def tree(self):
        """Every regular file under the deploy root (excluding backups/) with content hash and mode."""
        out = {}
        for p in sorted(self.opt.rglob("*")):
            rel = p.relative_to(self.opt).as_posix()
            if rel.startswith("backups") or p.is_dir():
                continue
            out[rel] = (sha(p.read_bytes()), stat.S_IMODE(p.stat().st_mode))
        return out

    # -- running -------------------------------------------------------------------------------------------
    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ("RELEASE_DIR", "DETACH", "RECOVER_DIR")}
        env.update(PATH=f"{self.bin}:{os.environ['PATH']}:{self.shims}", FAKE_STATE=str(self.state), FAKE_LOG=str(self.log),
                   FAKE_OPT=str(self.opt), CRON_FILE=str(self.cron), SUDO_UID=str(ME), AUTOMATIC="false", ALLOW_MIGRATION="",
                   TZ="UTC")
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}   # extra=None removes a variable (e.g. sudo drops AUTOMATIC)

    def deploy(self, release: Release, *, apply=True, allow_migration="", automatic="false", detach=False, wait=True, tamper=None,
               staged=False, **extra):
        """staged=True: as release_host runs it, from its private RELEASE_DIR (<run_root>/<sha>.<8 alnum>)."""
        self.releases += 1
        self._next_second()
        where = self.run_root / f"{release.sha}.sim{self.releases:05d}" if staged else self.parent / f"rel-{self.releases}"
        rel = self.sandboxed(release).write(where)
        if staged:
            extra.setdefault("RELEASE_DIR", str(rel))
        if tamper:
            tamper(rel)
        script = self.script.replace("REL=/tmp\n", f"REL={rel}\n", 1)
        path = self.parent / f"deploy-{self.releases}.sh"
        path.write_text(script)
        env = self.env(SHA=release.sha, APPLY="--apply" if apply else "", ALLOW_MIGRATION=allow_migration, AUTOMATIC=automatic, **extra)
        if detach:
            env["DETACH"] = "1"
        proc = subprocess.Popen(["bash", str(path)], cwd=str(self.parent), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
        if not wait:
            return proc
        out, _ = proc.communicate(timeout=120)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, "")

    def _next_second(self):
        """The backup directory is named by the UTC second: two deploys must not start in the same one."""
        import time
        now = int(time.time())
        if getattr(self, "_last_second", None) == now:
            time.sleep(1.05 - (time.time() % 1))
        self._last_second = int(time.time())

    def recover(self, backup: Path | str, **extra):
        driver = Path(backup) / "deploy-driver.sh"
        env = self.env(RECOVER_DIR=str(backup), **extra)
        proc = subprocess.run(["bash", str(driver)], cwd=str(self.parent), env=env, capture_output=True, text=True, start_new_session=True,
                              timeout=120)
        return subprocess.CompletedProcess(proc.args, proc.returncode, proc.stdout + proc.stderr, "")

    def backups(self):
        root = self.opt / "backups"
        return sorted(root.glob("deploy-*")) if root.exists() else []

    def phase(self, backup: Path | None = None):
        backup = backup or self.backups()[-1]
        return (backup / "phase").read_text().strip()

    def containers(self):
        return {n: (c["sha"], c["image"], c["started"]) for n, c in self.docker_state()["containers"].items()
                if n.startswith("company-") and n not in ("company-postgres", "company-cloudflared")}

    def cleanup(self):
        shutil.rmtree(self.parent, ignore_errors=True)
