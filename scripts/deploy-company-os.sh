#!/usr/bin/env bash
# Deploy Company OS to the production host: company-api + company-worker.
# CI passes COMPANY_OS_DEPLOY_SHA to deploy the exact tested main commit; founder runs default
# to the latest origin/main.
# (+ company-reasoner, which runs the worker image).
#
#   bash scripts/deploy-company-os.sh            read-only: prechecks + host drift report
#   bash scripts/deploy-company-os.sh --apply    backup DB + files, build, migrate FIRST, recreate
#   bash scripts/deploy-company-os.sh --follow   reattach to the running/last --apply log
#   ACK_OVERRIDES="<paths>" ... --apply           drop reviewed compose overrides (exact paths)
#   ACK_COMPOSE=<hash> ... --apply                replace a host docker-compose.yml that differs
#   COMPANY_OS_ALLOW_MIGRATION=<rev> ... --apply  authorize a schema change to exactly that alembic
#                                                 revision (12 hex); without it any migration aborts
#   COMPANY_OS_AUTOMATIC_DEPLOY=true|false        set by the founder-dispatched CI deploy only
#                                                 from main (hash printed by the read-only run)
#
# --apply runs detached on the host (setsid nohup, log under /var/log): a dropped SSH connection
# (2026-10-05: "client_loop: send disconnect") no longer kills a deploy halfway. If the
# connection drops, run --follow; the deploy keeps going and its exit code is in the log.
#
# Order matters: new API code reads columns added by migrations (incidents.open_scope,
# metric_observations), so `alembic upgrade head` runs with the new image before any
# container is switched. A failed migration leaves the running containers untouched.
# Images are also tagged with the git SHA and the SHA is passed as COMPANY_OS_GIT_SHA (§9).
# docker-compose.yml ships with the source, so the host runs main's compose, not a copy that
# drifted: a host file that differs is shown and replaced only on an exact acknowledgement.
set -euo pipefail
case "${1:-}" in ""|--apply|--follow) ;; *) echo "usage: $0 [--apply|--follow]"; exit 2;; esac
APPLY=${1:-}
HOST=${COMPANY_OS_DEPLOY_HOST:-company-os-host} LOGS=/var/log/company-os-deploy

# Stream the detached run's log until it ends; exit with the deploy's own exit code.
follow() {
  local pid state current_pid last_line deadline
  pid=$(ssh -o ConnectTimeout=10 "$HOST" "sudo cat $LOGS.pid")
  [[ "$pid" =~ ^[0-9]+$ ]] || { echo "invalid deploy pid" >&2; exit 3; }
  deadline=$((SECONDS + 1500))
  ssh -o ServerAliveInterval=30 "$HOST" "sudo tail -n +1 -f --pid=$pid $LOGS-latest.log" || true
  while :; do
    state=$(ssh -o ConnectTimeout=10 "$HOST" "sudo cat $LOGS.pid && sudo tail -n 1 $LOGS-latest.log" 2>/dev/null) || state=
    if [ -n "$state" ]; then
      current_pid=${state%%$'\n'*}
      [ "$current_pid" = "$pid" ] || { echo "deploy run changed while following" >&2; exit 3; }
      last_line=${state##*$'\n'}
      if [[ "$last_line" =~ ^EXIT\ ([0-9]{1,3})$ ]]; then exit "${BASH_REMATCH[1]}"; fi
    fi
    (( SECONDS >= deadline )) && break
    sleep 5
  done
  echo "deploy still running or log incomplete: re-run with --follow" >&2
  exit 3
}
[ "$APPLY" != "--follow" ] || follow
# The release mode travels explicitly to the host (ssh + sudo drop the caller's environment; that is how
# the 2026-10-08 automatic deploy migrated without its flag). Unknown or contradictory values stop here.
AUTOMATIC=${COMPANY_OS_AUTOMATIC_DEPLOY:-false}
ALLOW_MIGRATION=${COMPANY_OS_ALLOW_MIGRATION:-}
case "$AUTOMATIC" in true|false) ;; *) echo "ABORT: COMPANY_OS_AUTOMATIC_DEPLOY must be true or false"; exit 2;; esac
[[ -z "$ALLOW_MIGRATION" || "$ALLOW_MIGRATION" =~ ^[0-9a-f]{12}$ ]] || { echo "ABORT: COMPANY_OS_ALLOW_MIGRATION must be empty or one 12-hex alembic revision"; exit 2; }
if [ "$AUTOMATIC" = true ]; then
  [ -z "${ACK_OVERRIDES:-}" ] && [ -z "${ACK_COMPOSE:-}" ] || { echo "ABORT: contradictory: an automatic deploy cannot carry attended acknowledgements (ACK_*)"; exit 2; }
  [ -n "${COMPANY_OS_DEPLOY_SHA:-}" ] || { echo "ABORT: an automatic deploy must name the exact commit (COMPANY_OS_DEPLOY_SHA)"; exit 2; }
fi
cd "$(git rev-parse --show-toplevel)"
if [ -n "${COMPANY_OS_DEPLOY_SHA:-}" ]; then
  SHA=$(git rev-parse "${COMPANY_OS_DEPLOY_SHA}^{commit}")
else
  git fetch -q origin
  SHA=$(git rev-parse origin/main)
fi
PATHS="app migrations scripts agents skills tests alembic.ini requirements.txt Dockerfile docker-compose.yml docs/PERMANENT_FACTS.md"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
# tar.umask=0022: git's default (0002) packs files 0664/0775, and the host's root extraction keeps archive
# modes, so every release file was group-writable. Pin it here so a local git config cannot change it.
COPYFILE_DISABLE=1 git -c tar.umask=0022 archive --format=tar "$SHA" $PATHS | gzip > "$TMP/src.tgz"
git ls-tree -r "$SHA" --format='%(objectname) %(path)' -- $PATHS | while read -r obj p; do
  printf '%s  %s\n' "$(git cat-file -p "$obj" | shasum -a 256 | cut -c1-64)" "$p"; done > "$TMP/main.sha256"
cat > "$TMP/company-os-deploy.sh" <<'REMOTE'
set -euo pipefail
ANCHORED=   # set only by this run, just before it anchors; never taken from the environment
# The verifier and this driver are one installed bundle (IMAGE_BINDING_DESIGN.md §3): release_host.bundle_problem
# refuses when either marker is missing or the integers differ. Bound mode is never optional.
DRIVER_BOUND_IMAGES=1
DRIVER_BUNDLE_VERSION=1
# Recovery (RECOVER_DIR) restores the previous release and ships nothing, and `sudo RECOVER_DIR=... bash` drops the
# caller's environment, so it must not need a deploy mode. Only the recovery branch below ever sees this default
# (it always exits); a normal deploy without AUTOMATIC is still refused.
[ -z "${RECOVER_DIR:-}" ] || AUTOMATIC=${AUTOMATIC:-false}
# The release mode must arrive explicitly and make sense; unknown is refused, never defaulted.
case "${AUTOMATIC:-}" in
  true|false) ;;
  *) echo "ABORT: deploy mode unknown (AUTOMATIC='${AUTOMATIC:-}'); nothing was changed."; exit 1 ;;
esac
[[ -z "${ALLOW_MIGRATION:-}" || "${ALLOW_MIGRATION:-}" =~ ^[0-9a-f]{12}$ ]] || { echo "ABORT: malformed ALLOW_MIGRATION; nothing was changed."; exit 1; }
ALLOW_MIGRATION=${ALLOW_MIGRATION:-}
# One exception (B-7): the release host passes ACK_COMPOSE for a founder-signed release that changes compose, after
# checking the host file is the applied release's (app/release_host.py deploy_acknowledgements). It always uses a
# staged RELEASE_DIR; the staged bootstrap never forwards an ACK in automatic mode. The value is still only the
# host file's short hash, which the drift check below recomputes from the file itself.
if [ "$AUTOMATIC" = true ] && { [ -n "${ACK_OVERRIDES:-}" ] || { [ -n "${ACK_COMPOSE:-}" ] && { [ -z "${RELEASE_DIR:-}" ] || [[ ! "${ACK_COMPOSE}" =~ ^[0-9a-f]{12}$ ]]; }; }; }; then
  echo "ABORT: contradictory: an automatic deploy cannot carry attended acknowledgements; nothing was changed."; exit 1
fi
# --- end deploy mode

# The founder's Stage C pause fence (ops/company-coder-pause.compose.yml, docs/CODER_RUNTIME.md) is the one compose
# override a deploy keeps: recreating company-api from the base file alone used to drop it on every deploy. It is
# re-applied only when it is byte-identical to the reviewed file (sha pinned here, checked against ops/ by a test)
# and root-owned, not group/world-writable, in a root-owned directory. Anything else falls back to the ACK path.
FENCE_OVERLAY=/opt/company-os/ops/company-coder-pause.compose.yml
FENCE_SHA256=e5fa3c821ecd7f4da58b24275fb65592cd2d62072b679cfdddd6504bdd88e3fa
fence_verified() {   # [$1 owner uid] [$2 group gid], root for both by default ($1/$2 exist for tests)
  local uid=${1:-0} gid=${2:-0} f=$FENCE_OVERLAY p meta
  [ -f "$f" ] && [ ! -L "$f" ] && [ ! -L "$(dirname "$f")" ] || return 1
  for p in "$(dirname "$f")" "$f"; do
    meta=$(stat -c '%u:%g:%a' -- "$p" 2>/dev/null || stat -f '%u:%g:%Lp' -- "$p") || return 1
    [ "${meta%:*}" = "$uid:$gid" ] || return 1
    if (( 8#${meta##*:} & 8#022 )); then return 1; fi
  done
  [ "$({ sha256sum "$f" 2>/dev/null || shasum -a 256 "$f"; } | cut -c1-64)" = "$FENCE_SHA256" ]
}
# --- compose plumbing (IMAGE_BINDING_DESIGN.md §3–§5) -----------------------------------------------------------
# Every Compose call gets the four image references explicitly on its own command line. The driver sources .env
# under `set -a`, so .env must not be able to inject or override them; COMPANY_OS_IMAGE_TAG is dropped for the four
# bound services (the unbound review signer and coder proxy still read it).
compose_env() {   # $1..$4 = api worker reasoner egress references; rest = compose arguments
  local api=$1 worker=$2 reasoner=$3 egress=$4
  shift 4
  (
    unset COMPANY_OS_IMAGE_TAG
    export COMPANY_OS_IMAGE_API="$api" COMPANY_OS_IMAGE_WORKER="$worker"
    export COMPANY_OS_IMAGE_REASONER="$reasoner" COMPANY_OS_IMAGE_EGRESS="$egress"
    docker compose "$@"
  )
}
compose_check() {   # $1..$4 references, then the call's -f files: its resolved config must name exactly these four
  local api=$1 worker=$2 reasoner=$3 egress=$4
  shift 4
  compose_env "$api" "$worker" "$reasoner" "$egress" "$@" config --format json | python3 -c '
import json, sys
want = {"company-api": sys.argv[1], "company-worker": sys.argv[2],
        "company-reasoner": sys.argv[3], "company-reasoner-egress": sys.argv[4]}
services = (json.load(sys.stdin) or {}).get("services") or {}
for name, image in want.items():
    got = (services.get(name) or {}).get("image")
    if got != image:
        print(f"resolved {name} image is {got!r}, expected {image!r}", file=sys.stderr)
        sys.exit(3)
' "$api" "$worker" "$reasoner" "$egress"
}
compose_bound() {   # $1..$4 references; optional "--"; then the compose arguments
  local api=$1 worker=$2 reasoner=$3 egress=$4 arg
  local files=()
  shift 4
  [ "${1:-}" != -- ] || shift
  # Check exactly the files the call uses; a late file option must not bypass the check.
  while [ "${1:-}" = -f ] && [ $# -ge 2 ]; do files+=(-f "$2"); shift 2; done
  for arg in "$@"; do
    case "$arg" in -f|-f?*|--file|--file=*)
      echo "compose: compose files must lead the call, where the image check sees them" >&2; return 1 ;;
    esac
  done
  compose_check "$api" "$worker" "$reasoner" "$egress" ${files[@]+"${files[@]}"} || return 1
  compose_env "$api" "$worker" "$reasoner" "$egress" ${files[@]+"${files[@]}"} "$@"
}
run_compose() {   # signed releases use verified IDs; only the pre-ledger legacy transport reaches plain Compose
  if [ "${BOUND:-no}" = yes ] || [ "${BOOTSTRAP:-no}" = yes ]; then
    compose_bound "$BOUND_API" "$BOUND_WORKER" "$BOUND_REASONER" "$BOUND_EGRESS" -- "$@"
  else
    ( export COMPANY_OS_IMAGE_TAG="${COMPANY_OS_IMAGE_TAG:-latest}"; docker compose "$@" )
  fi
}
compose_config_json() {   # the resolved config, under the same environment as the release it describes
  run_compose config --format json
}
service_of() {   # component (api|worker|reasoner|egress) -> the compose service that runs it
  case "$1" in
    api) echo company-api ;; worker) echo company-worker ;;
    reasoner) echo company-reasoner ;; egress) echo company-reasoner-egress ;;
    *) return 1 ;;
  esac
}
# The release's image inputs (release_gateway.inputs_digest, embedded verbatim): the same function the verifier and
# prepare use, computed from the tar the old verifier already built root-side (§7 step 5). Never from main.sha256,
# which carries no modes.
inputs_digest() {   # $1 = release tar; prints the 64-hex digest
  python3 -I - "$1" <<'PY'
import hashlib, io, json, sys, tarfile


class Refused(ValueError):
    pass


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


BUILD_TARGETS = {"api": "api", "worker": "worker", "reasoner": "reasoner", "egress": "egress"}


def inputs_digest(tar: bytes) -> str:
    """What an image build consumes: every non-directory release-archive member (except docker-compose.yml, which is
    removed from the build context) with its mode and type, plus the fixed component -> Dockerfile target map. JSON,
    so the encoding is unambiguous. `rg.inputs_digest` is embedded verbatim in the driver's PREPARE/bootstrap snippet;
    a test asserts both copies agree on fixtures with an executable bit, a symlink and a non-ASCII path."""
    files = []
    with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
        for member in archive.getmembers():
            if member.isdir() or member.name == "docker-compose.yml":
                continue
            if member.isfile():
                data, kind = archive.extractfile(member).read(), "file"
            elif member.issym():
                data, kind = member.linkname.encode("utf-8", "surrogateescape"), "symlink"
            else:
                raise Refused(f"the release archive holds an unexpected entry: {member.name}")
            files.append([member.name, member.mode & 0o777, kind, hashlib.sha256(data).hexdigest()])
    return hashlib.sha256(canonical({"build": BUILD_TARGETS, "files": sorted(files)})).hexdigest()


with open(sys.argv[1], "rb") as archive:
    print(inputs_digest(archive.read()))
PY
}
# The pre-sign build (IMAGE_BINDING_DESIGN.md §3–§4.2). release_host.prepare runs the INSTALLED bundle driver with the
# candidate's staged payload and PREPARE=1: a clean context, four untagged builds, one never-started anchor per image,
# and one result line. No tag is written, no container is started, no migration runs.
prepare_build() {
  local ctx idfile c target id inputs attempt=${ATTEMPT:-}
  local dummy=sha256:0000000000000000000000000000000000000000000000000000000000000000 ids=""
  [[ "${SHA:-}" =~ ^[0-9a-f]{40}$ ]] || { echo "ABORT: prepare needs a 40-hex commit; nothing was built."; exit 1; }
  [[ "$attempt" =~ ^[1-9][0-9]*$ ]] || { echo "ABORT: prepare needs a positive ATTEMPT; nothing was built."; exit 1; }
  [[ "${RELEASE_DIR:-}" =~ ^/run/company-os-deploy/[0-9a-f]{40}\.[A-Za-z0-9]{8}$ ]] || { echo "ABORT: prepare needs a staged release directory; nothing was built."; exit 1; }
  [ -f "$RELEASE_DIR/src.tgz" ] && [ ! -L "$RELEASE_DIR/src.tgz" ] || { echo "ABORT: prepare has no staged release archive; nothing was built."; exit 1; }
  inputs=$(inputs_digest "$RELEASE_DIR/src.tgz") || { echo "ABORT: prepare could not read the release inputs."; exit 1; }
  [[ "$inputs" =~ ^[0-9a-f]{64}$ ]] || { echo "ABORT: prepare computed no usable inputs digest."; exit 1; }
  ctx=$(mktemp -d) || exit 1
  trap 'rm -rf -- "${ctx:-}"' EXIT
  tar --no-same-owner -xzf "$RELEASE_DIR/src.tgz" -C "$ctx" || { echo "ABORT: prepare could not extract a clean build context."; exit 1; }
  # Compose may not re-map which target a service builds: the component -> target map is a constant (§3). The build
  # sections are read under placeholder IDs because the binding does not exist yet; only those sections are used.
  for c in api worker reasoner egress; do
    svc=$(service_of "$c") || { echo "ABORT: $c is not a component."; exit 1; }
    target=$(cd "$ctx" && POSTGRES_PASSWORD=prepare-config-only CLOUDFLARE_TUNNEL_TOKEN=prepare-config-only \
      compose_env "$dummy" "$dummy" "$dummy" "$dummy" -f docker-compose.yml config --format json \
      | python3 -I -c '
import json, sys
build = json.load(sys.stdin)["services"][sys.argv[1]].get("build") or {}
if set(build) - {"context", "target", "dockerfile"} or build.get("dockerfile", "Dockerfile") != "Dockerfile":
    sys.exit(3)
print(build.get("target") or "")' "$svc") \
      || { echo "ABORT: prepare could not read the $c build section; nothing was built."; exit 1; }
    [ "$target" = "$c" ] || { echo "ABORT: compose maps $c to build target '$target', expected '$c'; nothing was built."; exit 1; }
  done
  rm -f -- "$ctx/docker-compose.yml"   # never part of the build context: a Dockerfile copying it cannot consume it
  for c in api worker reasoner egress; do
    idfile=$ctx/.iid-$c
    docker build -q --target "$c" --iidfile "$idfile" "$ctx" >/dev/null || { echo "ABORT: docker build of $c failed."; exit 1; }
    id=$(cat "$idfile" 2>/dev/null) || exit 1
    [[ "$id" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "ABORT: docker build of $c produced no image ID."; exit 1; }
    docker image inspect --format '{{.Id}}' "$id" >/dev/null 2>&1 || { echo "ABORT: the built $c image is not present."; exit 1; }
    # A never-started container keeps the image from Coolify's cleanup; the attempt is in the name, so a retry can
    # never collide with (or silently reuse) an earlier attempt's anchors.
    docker create --name "company-os-bound-${inputs:0:12}-a$attempt-$c" --label coolify.managed=false \
      --network none --pull=never "$id" true >/dev/null 2>&1 \
      || { echo "ABORT: could not anchor the built $c image (company-os-bound-${inputs:0:12}-a$attempt-$c)."; exit 1; }
    ids="$ids $c=$id"
  done
  echo "BOUND inputs=$inputs$ids"
}
# Restoring the previous release starts the images it ran, never the candidate's: by content-addressed ID when they
# are known, otherwise through the rollback tag (and the tag -> ID mapping is re-inspected before each `up`). The
# restored compose may be tag-shaped (a release from before binding), so both shapes are accepted under one rule.
restore_env() {   # $1..$4 references; $5 rollback tag; rest = compose arguments
  local api=$1 worker=$2 reasoner=$3 egress=$4 tag=$5
  shift 5
  (
    export COMPANY_OS_IMAGE_TAG="$tag"
    export COMPANY_OS_IMAGE_API="$api" COMPANY_OS_IMAGE_WORKER="$worker"
    export COMPANY_OS_IMAGE_REASONER="$reasoner" COMPANY_OS_IMAGE_EGRESS="$egress"
    docker compose "$@"
  )
}
restore_resolve() {   # $1..$4 = previous image IDs (may be empty); $5 rollback tag. Sets RESTORE_ID_*, verifies them.
  local api=$1 worker=$2 reasoner=$3 egress=$4 tag=$5 out kind name value id var
  RESTORE_ID_API= RESTORE_ID_WORKER= RESTORE_ID_REASONER= RESTORE_ID_EGRESS=
  RESTORE_EXPECT_API=$api RESTORE_EXPECT_WORKER=$worker RESTORE_EXPECT_REASONER=$reasoner RESTORE_EXPECT_EGRESS=$egress
  out=$(restore_env "$api" "$worker" "$reasoner" "$egress" "$tag" config --format json) || return 1
  while read -r kind name value; do
    case "$kind" in
      bound) id=$value ;;
      tag) id=$(docker image inspect --format '{{.Id}}' "$value" 2>/dev/null) || return 1
           [[ "$id" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1 ;;
      *) return 1 ;;
    esac
    case "$name" in
      company-api) var=RESTORE_ID_API ;;
      company-worker) var=RESTORE_ID_WORKER ;;
      company-reasoner) var=RESTORE_ID_REASONER ;;
      company-reasoner-egress) var=RESTORE_ID_EGRESS ;;
      *) return 1 ;;
    esac
    printf -v "$var" '%s' "$id"
  done < <(printf '%s\n' "$out" | python3 -c '
import json, sys
api, worker, reasoner, egress, tag = sys.argv[1:6]
want = [("company-api", api, "company-os-api"), ("company-worker", worker, "company-os-worker"),
        ("company-reasoner", reasoner, "company-os-reasoner"),
        ("company-reasoner-egress", egress, "company-os-egress")]
services = (json.load(sys.stdin) or {}).get("services") or {}
for name, ref, repo in want:
    got = (services.get(name) or {}).get("image")
    if ref and got == ref:
        print("bound", name, got)
    elif got == repo + ":" + tag:
        print("tag", name, got)
    else:
        print(f"{name} resolves to {got!r}, expected {ref!r} or {repo}:{tag}", file=sys.stderr)
        sys.exit(3)
' "$api" "$worker" "$reasoner" "$egress" "$tag") || return 1
  [ -z "${RESTORE_EXPECT_API:-}" ] || [ "$RESTORE_ID_API" = "$RESTORE_EXPECT_API" ] || return 1
  [ -z "${RESTORE_EXPECT_WORKER:-}" ] || [ "$RESTORE_ID_WORKER" = "$RESTORE_EXPECT_WORKER" ] || return 1
  [ -z "${RESTORE_EXPECT_REASONER:-}" ] || [ "$RESTORE_ID_REASONER" = "$RESTORE_EXPECT_REASONER" ] || return 1
  [ -z "${RESTORE_EXPECT_EGRESS:-}" ] || [ "$RESTORE_ID_EGRESS" = "$RESTORE_EXPECT_EGRESS" ] || return 1
}
compose_restore() {   # the ONLY way a rollback starts containers; $1..$4 references, $5 rollback tag
  local api=$1 worker=$2 reasoner=$3 egress=$4 tag=$5 got
  restore_resolve "$api" "$worker" "$reasoner" "$egress" "$tag" || return 1
  if [ "${FENCE_KEEP:-no}" = yes ]; then
    restore_env "$api" "$worker" "$reasoner" "$egress" "$tag" -f docker-compose.yml -f "$FENCE_OVERLAY" \
      up -d --no-deps --no-build --pull never company-api || return 1
    restore_resolve "$api" "$worker" "$reasoner" "$egress" "$tag" || return 1
    restore_env "$api" "$worker" "$reasoner" "$egress" "$tag" up -d --no-deps --no-build --pull never \
      company-worker company-reasoner-egress company-reasoner || return 1
  else
    restore_resolve "$api" "$worker" "$reasoner" "$egress" "$tag" || return 1
    restore_env "$api" "$worker" "$reasoner" "$egress" "$tag" up -d --no-deps --no-build --pull never \
      company-api company-worker company-reasoner-egress company-reasoner || return 1
  fi
  # The containers must run exactly the previous IDs: a tag moved in the window is caught here, not claimed verified.
  got=$(docker inspect company-api --format '{{.Image}}') && [ "$got" = "$RESTORE_ID_API" ] || return 1
  got=$(docker inspect company-worker --format '{{.Image}}') && [ "$got" = "$RESTORE_ID_WORKER" ] || return 1
  got=$(docker inspect company-reasoner --format '{{.Image}}') && [ "$got" = "$RESTORE_ID_REASONER" ] || return 1
  got=$(docker inspect company-reasoner-egress --format '{{.Image}}') && [ "$got" = "$RESTORE_ID_EGRESS" ] || return 1
}
# Create the release containers from the images chosen before the switch. The caller sets COMPANY_OS_GIT_SHA and
# the four BOUND_* references; with FENCE_KEEP=yes the verified pause fence goes back onto company-api (only there:
# the other services keep the base file alone).
up_services() {   # $1 = bound (forward) | restore (rollback)
  case "${1:-bound}" in
    bound)
      # A pinless legacy apply still has a ledger-less host: run_compose resolves by tag there, as it always did.
      if [ "${FENCE_KEEP:-no}" = yes ]; then
        fence_verified || { echo "pause fence overlay is no longer the verified file; refusing to apply it" >&2; return 1; }
        run_compose -f docker-compose.yml -f "$FENCE_OVERLAY" up -d --no-deps --no-build --pull never company-api || return 1
        run_compose up -d --no-deps --no-build --pull never company-worker company-reasoner-egress company-reasoner
      else
        run_compose up -d --no-deps --no-build --pull never company-api company-worker company-reasoner-egress company-reasoner
      fi ;;
    restore) compose_restore "${PREV_IMAGE_API:-}" "${PREV_IMAGE_WORKER:-}" "${PREV_IMAGE_REASONER:-}" \
               "${PREV_IMAGE_EGRESS:-}" "${PREV_COMPOSE_TAG:-rollback-$S}" ;;
    *) echo "unknown up_services mode: ${1:-}" >&2; return 1 ;;
  esac
}
fence_live() {   # with FENCE_KEEP=yes, the running company-api must actually carry the fence
  [ "${FENCE_KEEP:-no}" != yes ] || [ "$(docker exec company-api printenv CODER_PAUSE_FENCE_ENABLED)" = true ]
}
# Coolify's nightly cleanup `docker rmi`s every tagged image no container uses (its coolify.managed check never
# works), so the rollback-<S> images vanished overnight. A never-started container per component keeps a rollback
# set. An attempt anchors its own set under pending names; only a VERIFIED release promotes them over the stable
# anchors, so a failed attempt never unprotects the last good release's rollback images. Anchors are containers:
# replacing one never removes an image.
ANCHOR=company-os-rollback-anchor
anchor_rollback() {   # $1 = component, $2 = image
  docker rm -f "$ANCHOR-pending-$1" >/dev/null 2>&1 || true
  docker create --name "$ANCHOR-pending-$1" --label coolify.managed=false --network none --pull=never "$2" true >/dev/null
}
promote_rollback_anchors() {   # after VERIFIED only; a component is never left with neither anchor
  local c
  for c in api worker reasoner egress; do
    docker inspect "$ANCHOR-pending-$c" >/dev/null 2>&1 || continue
    # An earlier promotion interrupted after stable -> old: put that anchor back before discarding anything.
    docker inspect "$ANCHOR-$c" >/dev/null 2>&1 || docker rename "$ANCHOR-old-$c" "$ANCHOR-$c" >/dev/null 2>&1 || true
    docker rm -f "$ANCHOR-old-$c" >/dev/null 2>&1 || true
    if docker inspect "$ANCHOR-$c" >/dev/null 2>&1; then docker rename "$ANCHOR-$c" "$ANCHOR-old-$c" || return 1; fi
    if ! docker rename "$ANCHOR-pending-$c" "$ANCHOR-$c"; then
      docker rename "$ANCHOR-old-$c" "$ANCHOR-$c" >/dev/null 2>&1 || true
      return 1
    fi
    docker rm -f "$ANCHOR-old-$c" >/dev/null 2>&1 || true
  done
}
drop_pending_anchors() {   # any exit that is not a promoted VERIFIED release; stable anchors stay
  local c
  for c in api worker reasoner egress; do docker rm -f "$ANCHOR-pending-$c" >/dev/null 2>&1 || true; done
}

# A schema change needs its own authorization naming the exact target revision, for every deploy,
# attended or automatic. Unknown current or target revision is refused.
migration_authorized() {
  local rev0=$1 target=$2 allow=$3
  [ -n "$rev0" ] && [ -n "$target" ] || return 1
  [ "$target" = "$rev0" ] && return 0
  [ "$allow" = "$target" ]
}

single_alembic_head() {
  local output=$1
  [[ "$output" =~ ^[0-9a-f]{12}$ ]] || return 1
  printf '%s' "$output"
}

# Where this run's release files are. Legacy transport: fixed /tmp names. Staged transport: the private
# root-owned copy the bootstrap verified (deploy-stage-bootstrap.sh), which this run removes when it ends.
REL=/tmp
if [ -n "${RELEASE_DIR:-}" ]; then
  if [[ ! "$RELEASE_DIR" =~ ^/run/company-os-deploy/[0-9a-f]{40}\.[A-Za-z0-9]{8}$ ]] || [ -L "$RELEASE_DIR" ] || [ ! -d "$RELEASE_DIR" ]; then
    echo "ABORT: RELEASE_DIR is not a staged release directory"; exit 1
  fi
  REL=$RELEASE_DIR
fi
# The pre-sign build runs the installed bundle driver against the candidate's payload and changes nothing on
# production. It is the only build reachable from an apply path with a ledger, and it exits before the ledger,
# the backup, the migration and every container.
[ "${PREPARE:-}" != 1 ] || { prepare_build; exit 0; }
# verify-release-manifest (B6 step 8): once release_host has bootstrapped this host (its ledger exists), an --apply
# is accepted only from release_host's own transaction: the ledger is pending (a manifest digest) for exactly this SHA,
# written after it verified the signed manifest, and RELEASE_DIR is exactly the staged directory release_host named in
# that same write (pending_release_dir). Another upload cannot claim the pending SHA. Checked before anything on the host
# changes; a refusal touches nothing, not even the named directory (it may be a live transaction's). An unreadable,
# malformed or incomplete ledger refuses. No ledger (not bootstrapped), a read-only run and RECOVER_DIR are unchanged.
# The path is fixed and release_host refuses any other (release_host.LEDGER); tests rewrite the script text, as for
# /opt/company-os; there is deliberately no override.
RELEASE_LEDGER=/var/lib/release-host/ledger/ledger.json
BUILDS=/var/lib/release-host/builds   # release_host.BUILDS: root-written build records and the bootstrap-consumed marker
if [ "${APPLY:-}" = --apply ] && [ -z "${RECOVER_DIR:-}" ] && { [ -e "$RELEASE_LEDGER" ] || [ -L "$RELEASE_LEDGER" ]; }; then
  PENDING=$(python3 -I -c '
import json, re, sys
s = json.load(open(sys.argv[1]))
if not isinstance(s, dict):
    sys.exit(1)
IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
def ids(v):
    if not isinstance(v, dict) or set(v) != {"api", "worker", "reasoner", "egress"}:
        return None
    out = [v.get(c) for c in ("api", "worker", "reasoner", "egress")]
    if not all(isinstance(x, str) and IMAGE.fullmatch(x) for x in out):
        return None
    return out
d, sha, rel = s.get("pending"), s.get("pending_sha"), s.get("pending_release_dir")
pi, bi = s.get("pending_images"), s.get("bound_images")
pids = ids(pi) if pi is not None else None
bids = ids(bi) if bi is not None else None
if (pi is not None and pids is None) or (bi is not None and bids is None):
    sys.exit(1)                                         # named but malformed: never authority, never "absent"
if d is None and sha is None and rel is None:
    print("none none none", *(["-"] * 8)); sys.exit(0)  # bootstrapped, nothing pending
ok = (isinstance(d, str) and re.fullmatch(r"[0-9a-f]{64}", d) and isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha)
      and isinstance(rel, str) and re.fullmatch(r"/run/company-os-deploy/[0-9a-f]{40}\.[A-Za-z0-9]{8}", rel)
      and rel.rsplit("/", 1)[-1].startswith(sha + "."))            # named for this release, as release_host names it
if not ok:
    sys.exit(1)                                         # an incomplete pending transaction is never authority
# Who signed it, then what the release runs: the pending binding (v2 verifier) and the running one (for the
# rollback/PREV comparison). Anything malformed is refused above; "-" only ever means absent.
print(sha, rel, "founder" if s.get("pending_approver") == "founder" else "gateway",
      *(pids or ["-"] * 4), *(bids or ["-"] * 4))' "$RELEASE_LEDGER" 2>/dev/null) \
    || PENDING="<unreadable-or-incomplete-ledger> - - - - - - - - - -"
  read -r PENDING_SHA PENDING_DIR PENDING_APPROVER P_API P_WORKER P_REASONER P_EGRESS B_API B_WORKER B_REASONER B_EGRESS <<< "$PENDING"
  if [[ ! "${SHA:-}" =~ ^[0-9a-f]{40}$ ]] || [ "$PENDING_SHA" != "$SHA" ] || [ "${RELEASE_DIR:-}" != "$PENDING_DIR" ]; then
    echo "ABORT: verify-release-manifest: this host is bootstrapped; it applies only the staged release the host verifier marked pending (pending_sha=${PENDING_SHA:-none}). Release by signing a manifest into the founder inbox; break-glass is a founder ledger recovery."
    exit 1
  fi
fi
# What this apply starts. A ledger with pending_images is a bound release (start by ID); a ledger whose pending
# release has no images is the one bootstrap release, which starts the IDs pinned in its own compose (§7). No
# ledger at all is the pre-binding transport (only an old verifier reaches it): it still builds, and its images are
# named by tag. The pins path is positively the bootstrap release: it also requires that the ledger has no running
# binding yet, so a later candidate can never take it.
BOUND=no
BOOTSTRAP=no
BOUND_API= BOUND_WORKER= BOUND_REASONER= BOUND_EGRESS=
BUILD_MARKER=
if [ "${APPLY:-}" = --apply ] && [ -z "${RECOVER_DIR:-}" ] && { [ -e "$RELEASE_LEDGER" ] || [ -L "$RELEASE_LEDGER" ]; }; then
  if [[ "${P_API:-}" =~ ^sha256:[0-9a-f]{64}$ ]] && [[ "${P_WORKER:-}" =~ ^sha256:[0-9a-f]{64}$ ]] \
     && [[ "${P_REASONER:-}" =~ ^sha256:[0-9a-f]{64}$ ]] && [[ "${P_EGRESS:-}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    BOUND=yes
    BOUND_API=$P_API BOUND_WORKER=$P_WORKER BOUND_REASONER=$P_REASONER BOUND_EGRESS=$P_EGRESS
  elif [[ "${B_API}${B_WORKER}${B_REASONER}${B_EGRESS}" == *sha256:* ]]; then
    echo "ABORT: the ledger already binds running images but this pending release names none; it is not the bootstrap release and cannot start by tag."
    exit 1
  else
    BOOTSTRAP=yes
  fi
fi
# Images are content-addressed; a tag may only live on a fixed name (company-os-<c>:<sha|rollback-…|latest>). A
# repository literally named `sha256` would make a later tag/restore ambiguous, so it is refused before anything moves.
# Capture first: Docker failure must not be interpreted as the absence of a forbidden repository.
if [ "$BOUND" = yes ]; then
  repositories=$(docker images --format '{{.Repository}}') || {
    echo "ABORT: could not list local image repositories. Nothing was changed."
    exit 1
  }
  if grep -x sha256 <<< "$repositories" >/dev/null; then
    echo "ABORT: a local image tag is named like an image ID (repository 'sha256'); tags only live on fixed names. Nothing was changed."
    exit 1
  fi
fi
# Nothing sourced later (./.env runs as root under set -a) may change what the gate decided on.
readonly APPLY SHA RELEASE_DIR REL RECOVER_DIR
readonly RELEASE_LEDGER BUILDS
declare -ar RELEASE_IMAGE_AUTHORITY=("$BOUND" "$BOOTSTRAP" "$BOUND_API" "$BOUND_WORKER" "$BOUND_REASONER" "$BOUND_EGRESS")
readonly P_API P_WORKER P_REASONER P_EGRESS B_API B_WORKER B_REASONER B_EGRESS
# The schedule is part of the release but the last thing it changes: a rejected, failed or interrupted deploy
# must leave /etc/cron.d/company-os exactly as it was. It is installed only after the new release passed its
# health gate, atomically (a dotted temp name, which cron ignores, then rename), verified byte-for-byte, and
# a copy of the previous file (or the fact that there was none) is kept so rollback can restore it verified.
CRON_FILE=${CRON_FILE:-/etc/cron.d/company-os}
cron_meta() { stat -c '%u:%g:%a' "$1" 2>/dev/null || stat -f '%u:%g:%Lp' "$1"; }   # owner:group:mode (GNU, then BSD)
snapshot_cron() {   # $1 = backup dir
  if [ -L "$CRON_FILE" ] || { [ -e "$CRON_FILE" ] && [ ! -f "$CRON_FILE" ]; }; then
    echo "ABORT: $CRON_FILE is not a regular file; refusing to manage it"; return 1
  fi
  if [ -f "$CRON_FILE" ]; then
    cp -p "$CRON_FILE" "$1/cron-before" && cmp -s "$CRON_FILE" "$1/cron-before" && cron_meta "$CRON_FILE" > "$1/cron-meta"
  else : > "$1/cron-absent"; fi
}
place_cron() {      # $1 = source file or "absent"; $2 = "keep" to preserve the source's owner and mode; atomic, verified
  local tmp
  tmp="$(dirname "$CRON_FILE")/.$(basename "$CRON_FILE").new.$$"
  if [ "$1" = absent ]; then rm -f "$CRON_FILE" && [ ! -e "$CRON_FILE" ]; return; fi
  if [ "${2:-}" = keep ]; then cp -p "$1" "$tmp"; else install -m 0644 "$1" "$tmp"; fi \
    && mv -f "$tmp" "$CRON_FILE" && cmp -s "$1" "$CRON_FILE" || { rm -f "$tmp"; return 1; }
}
install_cron() {    # $1 = the payload manifest: the schedule is what it says, present or deliberately absent
  [ -r "$1" ] || return 1
  if grep -qE '^[0-9a-f]{64}  scripts/company-os\.cron$' "$1"; then place_cron scripts/company-os.cron
  else place_cron absent; fi   # a release without a schedule must not inherit the previous release's file
}
restore_cron() {    # $1 = backup dir; nonzero if the previous schedule could not be put back and verified
  local want
  if [ -f "$1/cron-before" ]; then
    want=$(cat "$1/cron-meta" 2>/dev/null) && [ -n "$want" ] || return 1
    # Untouched since the snapshot (bytes, owner, group, mode): leave it alone, never rewrite what did not change.
    if [ -f "$CRON_FILE" ] && cmp -s "$1/cron-before" "$CRON_FILE" && [ "$(cron_meta "$CRON_FILE")" = "$want" ]; then return 0; fi
    place_cron "$1/cron-before" keep && [ "$(cron_meta "$CRON_FILE")" = "$want" ]   # metadata is verified, not just bytes
  elif [ -f "$1/cron-absent" ]; then place_cron absent
  else return 1; fi
}
# --- source restore, deploy journal and recovery -------------------------------------------------------------
# Restoring the previous release means the previous BYTES back and the files this release newly created removed
# again; extracting files-before.tgz alone leaves a file the new release added (a mixed tree).
restore_source() {   # $1 = backup dir
  local p
  tar -xzf "$1/files-before.tgz" -C /opt/company-os || return 1
  [ -f "$1/files-new.list" ] || return 0
  while IFS= read -r p; do
    case "$p" in ""|/*|..|../*|*/..|*/../*) continue ;; esac   # a manifest path is relative and never climbs
    rm -f -- "/opt/company-os/$p" || return 1
  done < "$1/files-new.list"
}
# The default (:latest) image tags are what a later `docker compose up` would start. A build or a refused/failed
# migration leaves them pointing at the NEW images while the containers still run the old ones; every abort puts
# all four back on the images that were running (tagged rollback-$S before anything changed). Bound mode never
# retags a value taken from a compose `image:` field: a bound reference is an ID, and an ID is not a tag name.
restore_tags() {
  [ -n "${API_IMG:-}" ] || return 0   # images not resolved yet: nothing was retagged
  case "$API_IMG" in sha256:*) return 0 ;; esac
  docker tag "company-os-api:rollback-$S" "$API_IMG" || return 1
  docker tag "company-os-worker:rollback-$S" "$WORKER_IMG" || return 1
  [ -z "$R_OLD" ] || docker tag "company-os-reasoner:rollback-$S" "$REASONER_IMG" || return 1
  [ -z "$E_OLD" ] || docker tag "company-os-egress:rollback-$S" "$EGRESS_IMG" || return 1
}
# One line per phase, replaced atomically. A phase other than the terminal ones below means the deploy did not
# finish (killed, power loss, lost host): the next --apply refuses until recovery has put the previous release back.
phase() { printf '%s\n' "$1" > "$B/phase.new" && mv -f "$B/phase.new" "$B/phase"; }
# Progress for the operator tailing the log: whole lines (no \r), an honest share of the deploy's steps, not a
# measure of time. ponytail: fixed step weights; a docker build's own progress is not knowable from -q.
progress() { printf '[%3d%%] %s\n' "$1" "$2"; }
# A pre-migration refusal is clean only when both the source tree and every changed image tag are restored
# and the terminal phase is durably recorded. A failed restore blocks the next deployment for human recovery.
abort_before_migration() {
  local reason="$1" prerequisite_ok="${2:-yes}" source_ok=yes tags_ok=yes
  restore_source "$B" || source_ok=no
  restore_tags || tags_ok=no
  if [ "$prerequisite_ok" = yes ] && [ "$source_ok" = yes ] && [ "$tags_ok" = yes ]; then
    if phase ABORTED_RESTORED; then
      echo "ABORT: $reason; previous files and image tags restored, nothing migrated."
    else
      phase ROLLBACK_FAILED || true
      echo "ROLLBACK FAILED: $reason; files and image tags were restored, but the recovery journal could not be verified; manual recovery required." >&2
    fi
  else
    phase ROLLBACK_FAILED || true
    echo "ROLLBACK FAILED: $reason; source, image-tag, or cleanup recovery failed; manual recovery required." >&2
  fi
  exit 1
}
# After Alembic succeeds, restoring source and tags alone is not a full rollback. If the schema revision
# changed, preserve a blocking failure state and require human recovery rather than restoring the DB automatically.
abort_host_cli_install() {
  local reason="$1" source_ok=yes cli_ok=yes tags_ok=yes
  restore_source "$B" || source_ok=no
  "$HCLI" restore --backup "$B" --root /opt/company-os --sums "$B/cli-files-before.sha256" || cli_ok=no
  restore_tags || tags_ok=no
  if [ "$REV0" != "$TARGET_REV" ]; then
    phase ROLLBACK_FAILED || true
    echo "ROLLBACK FAILED: $reason; database migration $REV0 -> $TARGET_REV already succeeded; no automatic database rollback was attempted; manual recovery required." >&2
  elif [ "$source_ok" = yes ] && [ "$cli_ok" = yes ] && [ "$tags_ok" = yes ]; then
    if phase ABORTED_RESTORED; then
      echo "ABORT: $reason; previous source, host CLI, and image tags restored; no schema migration was applied."
    else
      phase ROLLBACK_FAILED || true
      echo "ROLLBACK FAILED: $reason; source, host CLI, and image tags were restored, but the recovery journal could not be verified; manual recovery required." >&2
    fi
  else
    phase ROLLBACK_FAILED || true
    echo "ROLLBACK FAILED: $reason; source, host CLI, or image-tag restoration failed; manual recovery required." >&2
  fi
  exit 1
}
# A failed migration command can leave schema effects that are not represented by the Alembic revision.
# Restore the non-database release artifacts, but keep the journal blocking until a human verifies the DB.
abort_migration_failure() {
  local reason="$1" source_ok=yes tags_ok=yes journal_ok=yes
  restore_source "$B" || source_ok=no
  restore_tags || tags_ok=no
  phase ROLLBACK_FAILED || journal_ok=no
  if [ "$source_ok" = yes ] && [ "$tags_ok" = yes ] && [ "$journal_ok" = yes ]; then
    echo "MIGRATION FAILED: $reason; source and image tags restored, but database state requires verification; no automatic database rollback was attempted." >&2
  else
    echo "ROLLBACK FAILED: $reason; source, image-tag, or journal recovery failed; database state requires verification and manual recovery." >&2
  fi
  exit 1
}
is_terminal_phase() { case "$1" in VERIFIED|ROLLED_BACK_VERIFIED|ABORTED_RESTORED|ROLLED_BACK_PARTIAL|ROLLBACK_FAILED) return 0 ;; *) return 1 ;; esac; }
# Terminal does not always mean healthy: ROLLED_BACK_PARTIAL and ROLLBACK_FAILED still demand a human, so they
# block the next --apply too (only VERIFIED, ROLLED_BACK_VERIFIED and ABORTED_RESTORED are clean).
phase_is_clean() { case "$1" in VERIFIED|ROLLED_BACK_VERIFIED|ABORTED_RESTORED) return 0 ;; *) return 1 ;; esac; }
incomplete_deploys() {   # $1 = backups dir; prints "<dir> <phase>" for every deploy that is not in a clean terminal phase
  local d ph
  for d in "$1"/deploy-*; do
    [ -d "$d" ] && [ ! -L "$d" ] && [ -f "$d/phase" ] || continue   # older backups without a journal are not judged
    ph=$(head -n 1 "$d/phase")
    phase_is_clean "$ph" || printf '%s %s\n' "$d" "$ph"
  done
}
# deploy.env: the facts recovery needs, written as NAME=value and read back through an allow-list (never sourced).
write_env() { printf '%s=%s\n' "$1" "$2" >> "$B/deploy.env"; }
read_env() {   # $1 = backup dir; sets the allow-listed variables, ignores everything else
  local k v
  while IFS== read -r k v; do
    case "$k" in
      S|NEW_SHA|OLD_SHA|API_IMG|WORKER_IMG|REASONER_IMG|EGRESS_IMG|R_OLD|E_OLD|FENCE_KEEP|FENCE_BEFORE|BOUND|PREV_IMAGE_API|PREV_IMAGE_WORKER|PREV_IMAGE_REASONER|PREV_IMAGE_EGRESS|PREV_COMPOSE_TAG|REV0|TARGET_REV)
        [[ "$v" =~ ^[A-Za-z0-9_.:/@-]*$ ]] || { echo "recovery: refusing a malformed $k in deploy.env" >&2; return 1; }
        printf -v "$k" '%s' "$v" ;;
    esac
  done < "$1/deploy.env"
}
if [ "${DETACH:-}" = 1 ]; then
  # One deploy at a time. 2026-10-05: two overlapping runs; the second read the old alembic
  # revision, then failed with DuplicateColumn, rebuilt :latest and overwrote the pid file.
  # The launcher takes the lock and the detached run inherits fd 9, holding it until it ends.
  exec 9>/var/lock/company-os-deploy.lock
  flock -n 9 || { echo "ABORT: another deploy is running; attach with --follow"; exit 1; }
  # Re-launch this script in its own session so SIGHUP from a dropped SSH session cannot reach it.
  LOG=/var/log/company-os-deploy-$(date -u +%Y%m%dT%H%M%SZ).log
  ln -sfn "$LOG" /var/log/company-os-deploy-latest.log
  DETACH= setsid nohup bash -c 'bash "$1"; echo "EXIT $?"' _ "$0" > "$LOG" 2>&1 < /dev/null &
  echo $! > /var/log/company-os-deploy.pid
  echo "detached: pid $! · log $LOG"
  exit 0
fi
cd /opt/company-os
set -a; . ./.env; set +a
BOUND=${RELEASE_IMAGE_AUTHORITY[0]} BOOTSTRAP=${RELEASE_IMAGE_AUTHORITY[1]}
BOUND_API=${RELEASE_IMAGE_AUTHORITY[2]} BOUND_WORKER=${RELEASE_IMAGE_AUTHORITY[3]}
BOUND_REASONER=${RELEASE_IMAGE_AUTHORITY[4]} BOUND_EGRESS=${RELEASE_IMAGE_AUTHORITY[5]}
PGU=${POSTGRES_USER:-company}; PGD=${POSTGRES_DB:-company}
q() { docker exec company-postgres psql -U "$PGU" -d "$PGD" -Atc "$1"; }
OPEN="('READY','QUEUED','IN_PROGRESS','WAITING_EXTERNAL','WAITING_APPROVAL','BLOCKED','REVIEW')"
cleanup() {
  if [ "$REL" = /tmp ]; then rm -f /tmp/src.tgz /tmp/main.sha256; else rm -rf -- "$REL"; fi
  # The pending anchors hold the pre-deploy release's rollback-$S images. Drop them only when THIS run created them
  # (ANCHORED) and nothing was switched yet (no journal) or the attempt ended clean. Before anchoring, pending
  # anchors belong to an earlier deploy (an unfinished one whose RECOVER_DIR recovery needs them, or a VERIFIED one
  # whose promotion failed). After VERIFIED a successful promotion already renamed them; if promotion failed they
  # are the only anchors of the release before this one, so they stay too.
  if [ "${APPLY:-}" = --apply ] && [ -n "${ANCHORED:-}" ] && [ -n "${B:-}" ]; then
    local p=""
    [ ! -f "$B/phase" ] || p=$(head -n 1 "$B/phase")
    if [ -z "$p" ] || { [ "$p" != VERIFIED ] && phase_is_clean "$p"; }; then drop_pending_anchors; fi
  fi
}
trap cleanup EXIT

# Root executes helpers from the deploy backup directory, so every directory from / down to it must be
# a real directory (no symlink), owned by root (or $2, for tests) and writable by nobody else: then only
# root can create, rename or replace anything in it. Never repaired here; an untrusted path stops the deploy.
trusted_dir() {
  local path=$1 uid=${2:-0} rest p=/ part owner m
  case "$path" in /*) ;; *) echo "untrusted path: $path is not absolute" >&2; return 1 ;; esac
  rest=${path#/}
  while :; do
    if [ -L "$p" ] || [ ! -d "$p" ]; then echo "untrusted path: $p is not a real directory" >&2; return 1; fi
    read -r owner m <<< "$(stat -c '%u %a' -- "$p" 2>/dev/null || stat -f '%u %Lp' -- "$p")"
    if [ "$owner" != 0 ] && [ "$owner" != "$uid" ]; then echo "untrusted path: $p is owned by uid $owner" >&2; return 1; fi
    if (( 8#$m & 8#022 )); then echo "untrusted path: $p is group- or world-writable (mode $m)" >&2; return 1; fi
    [ -n "$rest" ] || return 0
    part=${rest%%/*}; rest=${rest#"$part"}; rest=${rest#/}
    case "$part" in ""|.|..) echo "untrusted path: $path has an empty, . or .. component" >&2; return 1 ;; esac
    p=${p%/}/$part
  done
}

# Trusted owners of the deploy path: root, and the account that invoked sudo for this deploy. On the host
# /opt/company-os is owned by the deploy user, who already holds sudo, so trusting it adds nothing that
# account could not do anyway. Anything other than a plain number means root only.
TRUSTED_UID=0
case "${SUDO_UID:-}" in
  ''|*[!0-9]*) ;;
  *) TRUSTED_UID=$SUDO_UID ;;
esac

# Three distinct outcomes, never conflated: OK (verified), FAIL (unsafe mode, wrong type, missing path) and
# UNVERIFIED (the check itself could not run). Report-only until the founder approves enforcement; then
# flip this constant in a reviewed change and a FAIL or UNVERIFIED aborts before any container switch.
ENFORCE_RELEASE_PERMISSIONS=false
check_release_permissions() {
  # The verifier's own last line decides, together with its exit code: an exit status alone is not
  # trusted (bash 3.2 under `set -e` reports a missing interpreter as 1, i.e. as "findings").
  local out rc errexit=0
  case $- in *e*) errexit=1; set +e ;; esac
  out=$(python3 scripts/verify-release-permissions.py --root /opt/company-os --archive "$REL/src.tgz" 2>&1)
  rc=$?
  [ "$errexit" = 0 ] || set -e
  [ -z "$out" ] || printf '%s\n' "$out"
  case "$rc:$(printf '%s\n' "$out" | tail -n 1)" in
    "0:RELEASE PERMISSIONS: OK"*) return 0 ;;
    "1:RELEASE PERMISSIONS: FAIL"*) echo "RELEASE PERMISSIONS: FAIL: extracted files differ from the archive or are group/world-writable (findings above)" ;;
    *) echo "RELEASE PERMISSIONS: UNVERIFIED: the check could not complete (exit $rc); nothing was proven" ;;
  esac
  if [ "$ENFORCE_RELEASE_PERMISSIONS" != true ]; then
    echo "WARN: release permissions are report-only; the release continues" >&2
    return 0
  fi
  return 1
}

# A fresh backups/deploy-<S> under a trusted ROOT, created by this run with mode 0700 (mkdir without -p
# fails on anything already there, including a symlink). Prints the path relative to ROOT.
new_backup_dir() {
  local root=$1 stamp=$2 uid=${3:-0} dir
  trusted_dir "$root" "$uid" || return 1
  if [ ! -e "$root/backups" ] && [ ! -L "$root/backups" ]; then mkdir -m 0755 -- "$root/backups" || return 1; fi
  trusted_dir "$root/backups" "$uid" || return 1
  dir="backups/deploy-$stamp"
  # Fresh, created by root with exactly 0700 inside a trusted parent: nobody else can replace it.
  mkdir -m 0700 -- "$root/$dir" || { echo "untrusted path: $root/$dir already exists" >&2; return 1; }
  echo "$dir"
}

# Put the previous release back: source (bytes + removal of files the release added), host CLI, cron schedule,
# image tags and containers, then verify with the same gate that verifies a release. Shared by the failure path of
# a running deploy and by recovery after an interruption. Returns 0 only when the containers are verified AND the
# host CLI, source, schedule and image tags are verified restored; sets ROLLBACK_RESULT to VERIFIED, PARTIAL or FAILED.
rollback_core() {
  local cli_restored=yes id
  ROLLBACK_RESULT=PARTIAL
  if [ -e "$B/migration-attempted" ] || [ -L "$B/migration-attempted" ]; then
    echo "database migration was attempted; refusing to start previous code against an unverified schema; manual recovery required" >&2
    return 1
  fi
  # The previous release goes back with the fence it had (journals older than FENCE_BEFORE: the forward decision).
  local fence_before=${FENCE_BEFORE:-${FENCE_KEEP:-no}}
  local FENCE_KEEP=no   # set below from fence_before, once the old source is back
  ROLLBACK_RESULT=FAILED
  restore_source "$B" || cli_restored=no
  if [ -x "$B/host-cli.sh" ] && [ -f "$B/cli-files-before.sha256" ]; then
    "$B/host-cli.sh" restore --backup "$B" --root /opt/company-os --sums "$B/cli-files-before.sha256" || cli_restored=no
  fi
  restore_cron "$B" || cli_restored=no   # the schedule is restored and verified like the CLI; unverified is reported as partial
  # Tags only on fixed names: a bound release's image references are IDs, and none of them was retagged.
  case "${API_IMG:-}" in
    sha256:*) ;;
    "") ;;
    *) docker tag "company-os-api:rollback-$S" "$API_IMG" 2>/dev/null || cli_restored=no
       docker tag "company-os-worker:rollback-$S" "${WORKER_IMG:-company-os-worker:latest}" 2>/dev/null || cli_restored=no
       [ -z "${R_OLD:-}" ] || [ -z "${REASONER_IMG:-}" ] || docker tag "company-os-reasoner:rollback-$S" "$REASONER_IMG" || cli_restored=no
       [ -z "${E_OLD:-}" ] || [ -z "${EGRESS_IMG:-}" ] || docker tag "company-os-egress:rollback-$S" "$EGRESS_IMG" || cli_restored=no ;;
  esac
  if [ -n "${PREV_COMPOSE_TAG:-}" ]; then
    [[ "$PREV_COMPOSE_TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || return 1
    docker tag "$PREV_IMAGE_API" "company-os-api:$PREV_COMPOSE_TAG" || cli_restored=no
    docker tag "$PREV_IMAGE_WORKER" "company-os-worker:$PREV_COMPOSE_TAG" || cli_restored=no
    docker tag "$PREV_IMAGE_REASONER" "company-os-reasoner:$PREV_COMPOSE_TAG" || cli_restored=no
    docker tag "$PREV_IMAGE_EGRESS" "company-os-egress:$PREV_COMPOSE_TAG" || cli_restored=no
  fi
  # A bound release starts the previous images by ID only: a journal without them cannot be rolled back by tag.
  if [ "${BOUND:-no}" = yes ]; then
    for id in "${PREV_IMAGE_API:-}" "${PREV_IMAGE_WORKER:-}" "${PREV_IMAGE_REASONER:-}" "${PREV_IMAGE_EGRESS:-}"; do
      [[ "$id" =~ ^sha256:[0-9a-f]{64}$ ]] || {
        echo "ROLLBACK FAILED: this bound release's journal records no previous image ID for every component; tags are not a fallback" >&2
        return 1
      }
    done
  fi
  # A fence that is not the verified file is never applied, but the old release is still recreated (from the base
  # file) so the candidate is never left running; not getting the old configuration back is PARTIAL.
  case $fence_before in
    yes) if fence_verified; then FENCE_KEEP=yes
         else cli_restored=no; echo "pause fence overlay is not the verified file; previous release restored without it" >&2; fi ;;
    unverified) cli_restored=no; echo "previous release ran an unreviewed fence overlay; restored without it" >&2 ;;
  esac
  COMPANY_OS_GIT_SHA=$OLD_SHA COMPANY_OS_IMAGE_TAG=rollback-$S up_services restore \
    || { cli_restored=no; echo "the previous release could not be started from its recorded image IDs" >&2; }
  for _ in $(seq 1 30); do
    docker inspect company-api --format '{{.State.Health.Status}}' | grep -q healthy && break
    sleep 3
  done
  fence_live || { cli_restored=no; echo "pause fence is not live on the restored company-api" >&2; }
  local gate=$B/release-health-gate.sh
  [ -x "$gate" ] || gate=scripts/deploy-health-gate.sh
  if [ ! -f "$B/recovery-identity.py" ] || ! python3 -I "$B/recovery-identity.py" verify "$B" "$OLD_SHA" "$PGU" "$PGD"; then
    cli_restored=no
    echo "database or four-container recovery identity is unverified; manual recovery evidence required" >&2
  fi
  if "$gate" "$OLD_SHA"; then
    if [ "$cli_restored" = yes ]; then ROLLBACK_RESULT=VERIFIED; else ROLLBACK_RESULT=PARTIAL; fi
  fi
  [ "$ROLLBACK_RESULT" = VERIFIED ]
}
# Recovery after an interrupted deploy: sudo RECOVER_DIR=/opt/company-os/backups/deploy-<stamp> bash <that dir>/deploy-driver.sh
if [ -n "${RECOVER_DIR:-}" ]; then
  [[ "$RECOVER_DIR" =~ ^/opt/company-os/backups/deploy-[0-9]{8}T[0-9]{6}Z$ ]] || { echo "ABORT: RECOVER_DIR is not a deploy backup directory"; exit 1; }
  trusted_dir "$RECOVER_DIR" "$TRUSTED_UID" || { echo "ABORT: untrusted recovery directory"; exit 1; }
  [ -f "$RECOVER_DIR/phase" ] && [ -f "$RECOVER_DIR/deploy.env" ] || { echo "ABORT: $RECOVER_DIR has no deploy journal; nothing to recover automatically"; exit 1; }
  B=${RECOVER_DIR#/opt/company-os/}
  P=$(head -n 1 "$B/phase")
  if phase_is_clean "$P"; then echo "NOTHING TO RECOVER: $B is $P"; exit 0; fi
  read_env "$B" || exit 1
  [[ "${OLD_SHA:-}" =~ ^[0-9a-f]{40}$ && "${S:-}" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || { echo "ABORT: deploy.env lacks a valid S/OLD_SHA"; exit 1; }
  echo "== recovering interrupted deploy $B (phase $P) -> $OLD_SHA"
  if rollback_core; then phase ROLLED_BACK_VERIFIED; echo "RECOVERY VERIFIED: $OLD_SHA"; exit 0; fi
  if [ "$ROLLBACK_RESULT" = PARTIAL ]; then phase ROLLED_BACK_PARTIAL; echo "RECOVERY PARTIAL: restoration of $OLD_SHA is unverified; database/container identity or host artifacts require manual recovery; check $B" >&2; exit 1; fi
  phase ROLLBACK_FAILED; echo "RECOVERY FAILED: manual recovery required; backup is $B" >&2; exit 1
fi

# --- bound releases (IMAGE_BINDING_DESIGN.md §3, §4.1, §7) ------------------------------------------------------
# Every bound ID must already exist locally and be held by a never-started anchor: an apply with a ledger never
# builds, and an unanchored ID could be removed by Coolify's cleanup before a rollback needs it.
prebind_images() {
  local c id
  for c in api worker reasoner egress; do
    case "$c" in
      api) id=$BOUND_API ;; worker) id=$BOUND_WORKER ;; reasoner) id=$BOUND_REASONER ;; egress) id=$BOUND_EGRESS ;;
    esac
    [[ "$id" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "ABORT: no valid $c image ID is bound; nothing was changed."; return 1; }
    [ "$(docker image inspect --format '{{.Id}}' "$id" 2>/dev/null)" = "$id" ] \
      || { echo "ABORT: the bound $c image $id is not present on this host; a bound apply never builds."; return 1; }
    # --no-trunc: without it `docker ps` prints a 12-character ID that never equals a full sha256 ID.
    docker ps -a --no-trunc --filter name=company-os-bound- --format '{{.Image}}' | grep -x -- "$id" >/dev/null \
      || { echo "ABORT: the bound $c image $id is not anchored; nothing may start unanchored."; return 1; }
  done
}
# Compose may not re-map which Dockerfile target a service builds: `prepare` enforced this before it built these
# images, and the apply enforces it again on the config it is about to start.
enforce_targets() {
  local json c target svc
  json=$(compose_config_json) || return 1
  for c in api worker reasoner egress; do
    svc=$(service_of "$c") || return 1
    target=$(printf '%s\n' "$json" | python3 -I -c '
import json, sys
build = json.load(sys.stdin)["services"][sys.argv[1]].get("build") or {}
if set(build) - {"context", "target", "dockerfile"} or build.get("dockerfile", "Dockerfile") != "Dockerfile":
    sys.exit(3)
print(build.get("target") or "")' "$svc") \
      || { echo "the compose build section for $c cannot be read" >&2; return 1; }
    [ "$target" = "$c" ] || { echo "compose maps $c to build target '$target', expected '$c'" >&2; return 1; }
  done
}
# The one bootstrap release (§7): no pending_images and no running binding, so the expected IDs are the pins in the
# release's own signed compose. They count only if they equal a root-written `bootstrap: true` ready record for
# exactly this release's image inputs, and the pins are consumed once (the marker is created just before the switch).
bootstrap_pins() {
  local pins pins_inputs attempt
  [ "$BOOTSTRAP" = yes ] || return 0
  [ -f docker-compose.yml ] && [ ! -L docker-compose.yml ] \
    || { echo "ABORT: the bootstrap release has no regular docker-compose.yml; nothing was changed."; return 1; }
  pins=$(python3 -I - docker-compose.yml <<'PY'
import re, sys

text = open(sys.argv[1], encoding="utf-8", errors="surrogateescape").read()
lines = text.splitlines()
start = None
for i, line in enumerate(lines):
    if re.match(r"^x-bound-images\s*:", line):
        if start is not None:
            sys.exit(3)                       # two pin blocks are never authority
        start = i
if start is None:
    sys.exit(3)
body, j = lines[start].split(":", 1)[1], start + 1
while "}" not in body:
    if j >= len(lines):
        sys.exit(3)
    body, j = body + " " + lines[j], j + 1
body = body.strip()
if body.count("{") != 1 or body.count("}") != 1 or not body.startswith("{") or not body.endswith("}"):
    sys.exit(3)                               # a flow mapping only: no anchors, no block style, no comments inside
pairs = {}
for part in body[1:-1].split(","):
    if not part.strip():
        continue
    key, sep, value = part.partition(":")
    key, value = key.strip(), value.strip().strip("'\"")
    if not sep or not re.fullmatch(r"[a-z0-9_]+", key) or key in pairs:
        sys.exit(3)                           # a duplicate key silently keeps the last one: refused, not parsed
    pairs[key] = value
if set(pairs) != {"inputs_sha256", "api", "worker", "reasoner", "egress"}:
    sys.exit(3)
if not re.fullmatch(r"[0-9a-f]{64}", pairs["inputs_sha256"]):
    sys.exit(3)
for c in ("api", "worker", "reasoner", "egress"):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", pairs[c]):
        sys.exit(3)
print(pairs["inputs_sha256"], pairs["api"], pairs["worker"], pairs["reasoner"], pairs["egress"])
PY
  ) || { echo "ABORT: the bootstrap release's x-bound-images pins are absent or malformed; nothing was changed."; return 1; }
  read -r pins_inputs BOUND_API BOUND_WORKER BOUND_REASONER BOUND_EGRESS <<< "$pins"
  inputs=$(inputs_digest "$REL/src.tgz") || { echo "ABORT: the release inputs cannot be read; nothing was changed."; return 1; }
  [ "$inputs" = "$pins_inputs" ] \
    || { echo "ABORT: the pins are for image inputs $pins_inputs but this release ships $inputs; nothing was changed."; return 1; }
  attempt=$(python3 -I - "$pins_inputs" "$BOUND_API" "$BOUND_WORKER" "$BOUND_REASONER" "$BOUND_EGRESS" "$BUILDS" <<'PY'
import json, os, re, stat, sys

digest, builds = sys.argv[1], sys.argv[6]
pins = dict(zip(("api", "worker", "reasoner", "egress"), sys.argv[2:6]))
try:
    directory = os.stat(builds)
except OSError:
    sys.exit(3)
if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != 0 or directory.st_mode & 0o022:
    sys.exit(3)                               # root-only, not group/world-writable: only root writes a record
try:
    fd = os.open(os.path.join(builds, digest + ".json"), os.O_RDONLY | os.O_NOFOLLOW)
except OSError:
    sys.exit(3)
try:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0 or info.st_mode & 0o022
            or info.st_size > 1 << 20):
        sys.exit(3)
    raw = os.read(fd, info.st_size + 1)
finally:
    os.close(fd)
try:
    record = json.loads(raw)
except ValueError:
    sys.exit(3)
if not isinstance(record, dict) or record.get("inputs_digest") != digest:
    sys.exit(3)
if record.get("status") != "ready" or record.get("bootstrap") is not True or record.get("images") != pins:
    sys.exit(3)
attempt = record.get("attempt")
if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
    sys.exit(3)
print(attempt)
PY
  ) || { echo "ABORT: the pins are not bound to a root-written ready bootstrap build record for these inputs; nothing was changed."; return 1; }
  BUILD_MARKER=$BUILDS/$pins_inputs.$attempt.bootstrap-consumed
  [ ! -e "$BUILD_MARKER" ] && [ ! -L "$BUILD_MARKER" ] \
    || { echo "ABORT: these pins were already consumed once; a retry needs a new founder prepare --retry --bootstrap and a new pins release."; return 1; }
}
# The staged release's compose must be the one this run validated (source install just laid it down).
bootstrap_compose_matches() {
  [ "$BOOTSTRAP" = yes ] || return 0
  local have want
  have=$(sha256sum docker-compose.yml | cut -c1-64)
  want=$(tar -xzf "$REL/src.tgz" -O docker-compose.yml | sha256sum | cut -c1-64)
  [ "$have" = "$want" ] || { echo "ABORT: the installed docker-compose.yml is not the release's; nothing was changed."; return 1; }
}

echo "== prechecks"
echo "alembic revision : $(q 'select version_num from alembic_version')"
DUP=$(q "select dedupe_key||': '||string_agg(key,', ') from tasks where dedupe_key is not null and status in $OPEN group by dedupe_key having count(*)>1")
echo "duplicate open   : ${DUP:-none}"
RUN=$(q "select string_agg(key,', ') from tasks where status='IN_PROGRESS'")
STAGE_C_RUN=$(q "select string_agg(id::text,', ' order by id) from agent_runs where status = 'STARTED' and tool_calls->>'stage_c' = 'true'")
echo "in-progress tasks: ${RUN:-none}"
echo "started Stage C runs: ${STAGE_C_RUN:-none}"
echo "external release : ENABLE_AUTOMATIC_EXTERNAL_RELEASE=${ENABLE_AUTOMATIC_EXTERNAL_RELEASE:-<unset: false>}"
echo "running images   : api $(docker inspect company-api --format '{{.Image}}' | cut -c8-19) · worker $(docker inspect company-worker --format '{{.Image}}' | cut -c8-19)"
# A container started with an extra compose file (e.g. a release override) would lose those
# settings when recreated from the base docker-compose.yml. Read Docker's own compose label.
OVERRIDES=""
for c in company-api company-worker; do
  files=$(docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')
  for f in ${files//,/ }; do
    [ "$(basename "$f")" = docker-compose.yml ] || OVERRIDES="$OVERRIDES $c:$f"
  done
done
# Whether company-api carried the pause fence before this deploy: a rollback restores THAT state, whatever the
# forward decision below (an ACK authorizes dropping the fence for the new release, not for the old one).
# yes only for the verified file; an override at that path that is not the reviewed file is recorded as unverified.
FENCE_BEFORE=no
case " $OVERRIDES " in *" company-api:$FENCE_OVERLAY "*)
  if fence_verified; then FENCE_BEFORE=yes; else FENCE_BEFORE=unverified; fi ;;
esac
# The verified pause fence on company-api is kept, not dropped, unless the founder names it in ACK_OVERRIDES.
FENCE_KEEP=no
case " $OVERRIDES " in *" company-api:$FENCE_OVERLAY "*)
  case " ${ACK_OVERRIDES:-} " in
    *" $FENCE_OVERLAY "*) echo "pause fence      : named in ACK_OVERRIDES, will be dropped" ;;
    *) if fence_verified; then
         FENCE_KEEP=yes
         kept=""; for o in $OVERRIDES; do [ "$o" = "company-api:$FENCE_OVERLAY" ] || kept="$kept $o"; done; OVERRIDES=$kept
         echo "pause fence      : kept on company-api (verified $FENCE_OVERLAY)"
       else
         echo "pause fence      : $FENCE_OVERLAY is NOT the reviewed root-owned file; treated as an ordinary override"
       fi ;;
  esac ;;
esac
echo "compose overrides: ${OVERRIDES:-none}"

# Founder steps that follow a deploy. Read-only, values never printed beyond mode/presence.
echo "== readiness (informational; nothing here blocks the deploy)"
DENY=$(docker logs --since 72h company-api 2>&1 | grep -c WOULD_DENY || true)
echo "api auth         : API_AUTH_MODE=${API_AUTH_MODE:-<unset: enforce>} · WOULD_DENY in 72h: $DENY"
if [ -z "${API_AUTH_MODE:-}" ] && [ "$DENY" != 0 ]; then
  echo "  WARN: unset means enforce; callers above would be refused. Run api-auth-setup.sh --apply first."
fi
echo "backup encryption: BACKUP_AGE_RECIPIENT $([ -n "${BACKUP_AGE_RECIPIENT:-}" ] && echo set || echo 'unset: coolify-db/config backups FAIL in production until set')"
echo "reasoner         : $(docker inspect company-reasoner --format '{{.State.Status}}' 2>/dev/null || echo 'not created yet (--apply creates it)')"
# Running digests, to pin docker-compose.yml to exactly what runs (no surprise recreate).
for c in company-postgres company-cloudflared; do
  echo "pin $c: $(docker inspect "$(docker inspect "$c" --format '{{.Image}}')" --format '{{index .RepoDigests 0}}' 2>/dev/null || echo unknown)"
done

echo "== host files that will change"
CHANGED=0
while read -r h p; do
  if [ ! -f "$p" ]; then echo "  new      $p"; CHANGED=$((CHANGED+1))
  elif [ "$(sha256sum "$p" | cut -c1-64)" != "$h" ]; then echo "  differs  $p"; CHANGED=$((CHANGED+1)); fi
done < "$REL/main.sha256"
find app migrations scripts -type f -not -path '*/__pycache__/*' | sort | while read -r p; do
  grep -q "  $p\$" "$REL/main.sha256" || echo "  HOST-ONLY (kept on the host; never reaches an image: it is built from a clean copy of the release) $p"
done
echo "  $CHANGED file(s) will be written"
echo "== host CLI"
CLI_SUM=$(sed -n 's|  scripts/companyctl$||p' "$REL/main.sha256")
[[ "$CLI_SUM" =~ ^[0-9a-f]{64}$ ]] || echo "  WARNING: main's checksum list has no valid scripts/companyctl entry; --apply will ABORT before installing the host CLI"
if [ -L /usr/local/bin/companyctl ] && [ "$(readlink -f /usr/local/bin/companyctl)" = /opt/company-os/scripts/companyctl ]; then
  echo "  /usr/local/bin/companyctl -> the release CLI (current)"
else
  echo "  /usr/local/bin/companyctl is NOT the release CLI (it may predate commands such as 'stage-c status'); --apply will point it at /opt/company-os/scripts/companyctl and keep the previous one in the backup"
fi
# The host compose may hold hand edits main does not have: show them before they are replaced.
# Compose references secrets as ${VAR} from .env; the diff is printed only in read-only runs,
# never into the --apply log.
COMPOSE_WANT=$(sed -n 's|  docker-compose.yml$||p' "$REL/main.sha256")
COMPOSE_HAVE=$(sha256sum docker-compose.yml 2>/dev/null | cut -c1-64 || true)
COMPOSE_DRIFT=""   # empty: host matches main; else the host's short hash, or "missing"
if [ "$COMPOSE_HAVE" != "$COMPOSE_WANT" ]; then COMPOSE_DRIFT=${COMPOSE_HAVE:0:12}; COMPOSE_DRIFT=${COMPOSE_DRIFT:-missing}; fi
if [ -n "$COMPOSE_DRIFT" ] && [ "$APPLY" != "--apply" ]; then
  echo "== docker-compose.yml: host ($COMPOSE_DRIFT) -> main"
  tar -xzf "$REL/src.tgz" -O docker-compose.yml | diff -u docker-compose.yml - 2>/dev/null || true
fi

echo "== unfinished deploys"
UNFINISHED_RO=$(incomplete_deploys /opt/company-os/backups)
if [ -n "$UNFINISHED_RO" ]; then
  printf '%s\n' "$UNFINISHED_RO" | sed 's/^/  UNFINISHED /'
  echo "  --apply will ABORT until each is recovered: sudo RECOVER_DIR=<directory> bash <directory>/deploy-driver.sh"
else echo "  none"; fi

echo "== deploy path trust"
if trusted_dir /opt/company-os "$TRUSTED_UID" && { [ ! -e /opt/company-os/backups ] && [ ! -L /opt/company-os/backups ] || trusted_dir /opt/company-os/backups "$TRUSTED_UID"; }; then
  echo "  /opt/company-os and backups/: real directories owned by root or uid $TRUSTED_UID (the sudo invoker), not group/world-writable"
else
  echo "  UNTRUSTED (see above): --apply will ABORT before changing anything"
fi
if [ "$APPLY" != "--apply" ]; then echo "read-only run; nothing changed. Re-run with --apply."; exit 0; fi
[ -z "$DUP" ] || { echo "ABORT: resolve duplicate open tasks first (the migration would refuse too)"; exit 1; }
[ -z "$RUN" ] || { echo "ABORT: a task is IN_PROGRESS; recreating the worker would cut it. Retry when idle."; exit 1; }
[ -z "$STAGE_C_RUN" ] || { echo "ABORT: Stage C AgentRun(s) are STARTED: $STAGE_C_RUN; recreating the worker would cut a live pilot. Existing release was left untouched. Inspect: sudo companyctl stage-c status"; exit 1; }
# Reviewed overrides may be dropped only when the founder names exactly the paths detected.
if [ -n "$OVERRIDES" ]; then
  if [ "$AUTOMATIC" = true ]; then
    echo "ABORT: automatic deployment refuses compose overrides:$OVERRIDES. Existing release was left untouched. ACK_OVERRIDES is attended-only; inspect: sudo companyctl stage-c status"
    exit 1
  fi
  found=$(for o in $OVERRIDES; do echo "${o#*:}"; done | sort -u | tr '\n' ' ')
  acked=$(for o in ${ACK_OVERRIDES:-}; do echo "$o"; done | sort -u | tr '\n' ' ')
  [ "$found" = "$acked" ] || { echo "ABORT: running containers use compose overrides:$OVERRIDES."
    echo "  Review what each sets, fold anything needed into docker-compose.yml, then re-run with"
    echo "  ACK_OVERRIDES=\"${found% }\" to recreate from the base file."; exit 1; }
  echo "dropping reviewed overrides: $found"
fi
if [ -n "$COMPOSE_DRIFT" ] && [ "${ACK_COMPOSE:-}" != "$COMPOSE_DRIFT" ]; then
  echo "ABORT: host docker-compose.yml ($COMPOSE_DRIFT) differs from main. Review the diff in the"
  echo "  read-only run, fold any host-only setting into main, then re-run with ACK_COMPOSE=$COMPOSE_DRIFT."
  exit 1
fi

UNFINISHED=$(incomplete_deploys /opt/company-os/backups)
if [ -n "$UNFINISHED" ]; then
  echo "ABORT: a previous deploy did not finish cleanly (directory phase):"
  printf '%s\n' "$UNFINISHED" | sed 's/^/  /'
  echo "  Recover it first: sudo RECOVER_DIR=<directory> bash <directory>/deploy-driver.sh ; nothing was changed."
  exit 1
fi

S=$(date -u +%Y%m%dT%H%M%SZ)
B=$(new_backup_dir /opt/company-os "$S" "$TRUSTED_UID") || { echo "ABORT: refusing an untrusted deploy backup path (see above); nothing was changed."; exit 1; }
echo "== backup -> $B"
progress 5 "backing up the database"
docker exec company-postgres pg_dump -U "$PGU" -d "$PGD" -Fc > "$B/db.dump" \
  || { echo "ABORT: the pre-deploy database dump failed; nothing was changed."; exit 1; }
# Read the whole dump back, not only its table of contents (--list): a dump cut off past the table of contents
# lists fine, then restores with tables silently empty. This dump is the restore point for this deploy.
docker exec -i company-postgres pg_restore --file=/dev/null < "$B/db.dump" \
  || { echo "ABORT: the pre-deploy database dump does not read back completely; nothing was changed."; exit 1; }
echo "  db dump table-data entries: $(docker exec -i company-postgres pg_restore --list < "$B/db.dump" | grep -c 'TABLE DATA')"
tar -czf "$B/files-before.tgz" $(cut -c67- "$REL/main.sha256" | while read -r p; do [ -f "$p" ] && echo "$p"; done)
cut -c67- "$REL/main.sha256" | while read -r p; do [ -e "$p" ] || [ -L "$p" ] || echo "$p"; done > "$B/files-new.list"   # restoring removes these again
snapshot_cron "$B" || { echo "ABORT: could not snapshot the current cron schedule; nothing was changed."; exit 1; }
# What the host's CLI files are NOW, before the new source overwrites them: a rollback must put these
# exact bytes back and prove it (the files on disk after extraction are already the new release).
for f in scripts/companyctl scripts/stage-c-host-facts.py; do
  [ ! -f "$f" ] || printf '%s  %s\n' "$(sha256sum "$f" | cut -c1-64)" "$f"
done > "$B/cli-files-before.sha256"
# Tag what is actually running, by image ID: release images need not be named company-os-*:latest.
OLD_SHA=$(docker exec company-api printenv COMPANY_OS_GIT_SHA)
[[ "$OLD_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "ABORT: running API has no full source SHA; cannot verify an automatic rollback."; exit 1;
}
# The rollback target, read before anything changes (IMAGE_BINDING_DESIGN.md §4.1). A reasoner or egress container
# may not exist yet on a pre-binding host; a bound release always runs all four, so there an absent one is refused.
PREV_IMAGE_API=$(docker inspect company-api --format '{{.Image}}')
PREV_IMAGE_WORKER=$(docker inspect company-worker --format '{{.Image}}')
PREV_IMAGE_REASONER=$(docker inspect company-reasoner --format '{{.Image}}' 2>/dev/null || true)
PREV_IMAGE_EGRESS=$(docker inspect company-reasoner-egress --format '{{.Image}}' 2>/dev/null || true)
for id in "$PREV_IMAGE_API" "$PREV_IMAGE_WORKER"; do
  [[ "$id" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "ABORT: a running container has no image ID; cannot verify a rollback."; exit 1; }
done
if [ "$BOUND" = yes ]; then
  for id in "$PREV_IMAGE_REASONER" "$PREV_IMAGE_EGRESS"; do
    [[ "$id" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "ABORT: a bound host must run all four services; a previous image ID is missing."; exit 1; }
  done
  # The ledger's running binding and production must agree before this run changes anything (the verifier's pre-step
  # BLOCK covers the timer path; a direct apply is refused here instead of discovering it in the rollback).
  if [[ "$B_API" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    { [ "$PREV_IMAGE_API" = "$B_API" ] && [ "$PREV_IMAGE_WORKER" = "$B_WORKER" ] \
      && [ "$PREV_IMAGE_REASONER" = "$B_REASONER" ] && [ "$PREV_IMAGE_EGRESS" = "$B_EGRESS" ]; } \
      || { echo "ABORT: the running images are not the ledger's bound images; production and the ledger disagree. Nothing was changed."; exit 1; }
  fi
fi
R_OLD=$PREV_IMAGE_REASONER
E_OLD=$PREV_IMAGE_EGRESS
docker tag "$PREV_IMAGE_API" "company-os-api:rollback-$S"
docker tag "$PREV_IMAGE_WORKER" "company-os-worker:rollback-$S"
[ -z "$R_OLD" ] || docker tag "$R_OLD" "company-os-reasoner:rollback-$S"
[ -z "$E_OLD" ] || docker tag "$E_OLD" "company-os-egress:rollback-$S"
# From here the pending anchors are this run's own (ANCHORED): only then may cleanup drop them. A partial
# failure drops every pending anchor, so a later VERIFIED run can never promote a stale component's image.
ANCHORED=1
if ! { anchor_rollback api "company-os-api:rollback-$S" && anchor_rollback worker "company-os-worker:rollback-$S" \
       && { [ -z "$R_OLD" ] || anchor_rollback reasoner "company-os-reasoner:rollback-$S"; } \
       && { [ -z "$E_OLD" ] || anchor_rollback egress "company-os-egress:rollback-$S"; }; }; then
  drop_pending_anchors
  # A bound release never switches without the previous release's images anchored (§4.3); the legacy transport keeps
  # the old warning because its rollback tags are only a convenience.
  if [ "$BOUND" = yes ] || [ "$BOOTSTRAP" = yes ]; then
    abort_before_migration "could not anchor the rollback images; a bound release must keep the previous images anchored"
  fi
  echo "WARN: could not anchor the rollback images; Coolify's nightly cleanup may delete them" >&2
fi
W0=$(docker inspect company-worker --format '{{.State.StartedAt}}')

# Everything a recovery needs is on disk before the first mutation; from here until a terminal phase is written,
# an interruption is visible (the next --apply refuses) and recoverable (RECOVER_DIR, see above).
cp -p -- "$0" "$B/deploy-driver.sh" && chmod 0700 "$B/deploy-driver.sh"
: > "$B/deploy.env"
write_env S "$S"; write_env NEW_SHA "$SHA"; write_env OLD_SHA "$OLD_SHA"
write_env R_OLD "$R_OLD"; write_env E_OLD "$E_OLD"; write_env FENCE_KEEP "$FENCE_KEEP"; write_env FENCE_BEFORE "$FENCE_BEFORE"
write_env BOUND "$BOUND"
write_env PREV_IMAGE_API "$PREV_IMAGE_API"; write_env PREV_IMAGE_WORKER "$PREV_IMAGE_WORKER"
write_env PREV_IMAGE_REASONER "$PREV_IMAGE_REASONER"; write_env PREV_IMAGE_EGRESS "$PREV_IMAGE_EGRESS"
# The signed archive supplies the standard-library checker before source replacement; the backup copy survives rollback.
tar -xzf "$REL/src.tgz" -O app/release_identity.py > "$B/recovery-identity.py" \
  || { echo "ABORT: the release has no recovery identity checker; nothing changed."; exit 1; }
python3 -I "$B/recovery-identity.py" snapshot "$B" "$OLD_SHA" "$PGU" "$PGD" \
  || { echo "ABORT: previous container/database identity is unavailable; nothing changed."; exit 1; }
PREV_COMPOSE_TAG=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("compose_tag") or "")' "$B/recovery-identity.json")
write_env PREV_COMPOSE_TAG "$PREV_COMPOSE_TAG"
phase SOURCE_INSTALLING
echo "== install source $SHA"
progress 15 "installing source $SHA"
tar --no-same-owner -xzf "$REL/src.tgz" -C /opt/company-os
chown -R root:root app migrations scripts agents skills tests alembic.ini requirements.txt Dockerfile docker-compose.yml docs/PERMANENT_FACTS.md
# Only the release's own scripts get the executable bit: a host-only script is not ours to change, and a rollback
# (which restores only what the release overwrote) could not put its mode back.
{ cut -c67- "$REL/main.sha256" | grep -E '^scripts/[^/]+\.sh$' || true; } | while IFS= read -r p; do chmod +x "$p"; done
# This copy survives restoring the old source during rollback, so rollback verification uses
# exactly the same deterministic gate as release verification. It lives in the root-created backup
# directory, never at a fixed /tmp path: root executes it, and a /tmp file someone else pre-created
# stays theirs after `cp` writes into it.
GATE="$B/release-health-gate.sh"
cp scripts/deploy-health-gate.sh "$GATE"
chmod +x "$GATE"
# Do the extracted files carry the archive's modes, none group/world-writable? Report-only today.
if ! check_release_permissions; then
  abort_before_migration "release file permissions are not verified"
fi
# The bootstrap release's expected IDs are the pins in its own compose (§7). They are read before any Compose call:
# the four `:?` image fields make Compose itself fail without them.
if [ "$BOOTSTRAP" = yes ]; then
  bootstrap_compose_matches || abort_before_migration "the installed docker-compose.yml is not the release's"
  bootstrap_pins || abort_before_migration "the bootstrap image pins are not usable"
fi
readonly BOUND BOOTSTRAP BOUND_API BOUND_WORKER BOUND_REASONER BOUND_EGRESS BUILD_MARKER
# A compose main cannot parse must not reach `build`/`up`: put the old files back and stop.
if ! run_compose config -q; then
  abort_before_migration "main's docker-compose.yml does not validate on this host"
fi
if [ "$BOUND" = yes ] || [ "$BOOTSTRAP" = yes ]; then
  # No build is reachable here: the four IDs are already built, pinned and anchored, and every Compose call below
  # resolves the config to exactly them before it runs.
  phase BUILDING
  progress 20 "checking the bound images"
  enforce_targets || abort_before_migration "the compose build target map is not the reviewed one"
  prebind_images || abort_before_migration "the bound images are not present and anchored"
  API_IMG=$BOUND_API WORKER_IMG=$BOUND_WORKER REASONER_IMG=$BOUND_REASONER EGRESS_IMG=$BOUND_EGRESS
  write_env API_IMG "$API_IMG"; write_env WORKER_IMG "$WORKER_IMG"
  write_env REASONER_IMG "$REASONER_IMG"; write_env EGRESS_IMG "$EGRESS_IMG"
  progress 80 "bound images verified"
else
  # Legacy transport (an old verifier on a host with no ledger): build from a CLEAN copy of the release, never from
  # the host tree. The host overlays the archive onto /opt/company-os and keeps files a release no longer ships (and
  # host-only data), and the Dockerfile copies whole app/ and migrations/: building from the host tree would put a
  # deleted module or a removed Alembic revision into the new image. The context below is exactly the payload
  # manifest, byte-verified, and is thrown away after the build.
  # Images come from main's compose, not the host's old one: a service main adds (the reasoner
  # egress, 2026-10-06) is missing there, and the old reasoner image was the worker's.
  # `config --images SERVICE` also lists dependencies (postgres sorted first on 2026-10-05), so
  # read each service's own image field from the resolved config.
  img() { compose_config_json | python3 -c "import json,sys; print(json.load(sys.stdin)['services']['$1']['image'])"; }
  API_IMG=$(img company-api); WORKER_IMG=$(img company-worker); REASONER_IMG=$(img company-reasoner); EGRESS_IMG=$(img company-reasoner-egress)
  case "$API_IMG $WORKER_IMG $REASONER_IMG $EGRESS_IMG" in
    *company-os-api*company-os-worker*company-os-reasoner*company-os-egress*) ;;
    *) abort_before_migration "unexpected compose images: $API_IMG / $WORKER_IMG / $REASONER_IMG / $EGRESS_IMG" ;;
  esac
  write_env API_IMG "$API_IMG"; write_env WORKER_IMG "$WORKER_IMG"; write_env REASONER_IMG "$REASONER_IMG"; write_env EGRESS_IMG "$EGRESS_IMG"
  CTX="$B/build-context"
  phase BUILDING
  build_failed() {
    local cleanup_ok=yes
    rm -rf -- "$CTX" || cleanup_ok=no
    abort_before_migration "build failed: $1" "$cleanup_ok"
  }
  # docker build -q prints nothing for minutes; a line every 15 s shows the deploy is alive. Returns the build's status.
  build_alive() {
    local svc=$1 pct=$2 pid t=0
    docker build -q --target "$TARGET" -t "$IMG" "$CTX" >/dev/null &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
      sleep 1; t=$((t + 1))
      [ $((t % 15)) -ne 0 ] || progress "$pct" "still building $svc · $((t / 60))m$((t % 60))s"
    done
    wait "$pid"
  }
  mkdir -m 0700 -- "$CTX" || build_failed "could not create the clean build context"
  tar --no-same-owner -xzf "$REL/src.tgz" -C "$CTX" || build_failed "could not extract the release into a clean build context"
  (cd "$CTX" && sha256sum -c --quiet "$REL/main.sha256") >/dev/null 2>&1 || build_failed "the clean build context does not match the release manifest"
  [ "$(cd "$CTX" && find . \( -type f -o -type l \) | sed 's|^\./||' | LC_ALL=C sort | tr '\n' ' ')" = "$(cut -c67- "$REL/main.sha256" | LC_ALL=C sort | tr '\n' ' ')" ] \
    || build_failed "the clean build context holds files the release manifest does not list (or lacks some)"
  SVCS=(company-api company-worker company-reasoner company-reasoner-egress)
  n=0
  for svc in "${SVCS[@]}"; do
    # Only a plain {context: ".", target: T} build is reproduced here; any other build option would be silently
    # dropped from a hand-run `docker build`, so it stops the deploy instead. `docker compose config` normalizes a
    # build without a dockerfile to dockerfile: Dockerfile (compose-go loader/normalize.go), which is exactly what
    # `docker build` reads by default, so that one value is the plain build; any other dockerfile is not.
    TARGET=$(compose_config_json | python3 -c "
import json, os, sys
b = json.load(sys.stdin)['services']['$svc'].get('build') or {}
ctx = os.path.realpath(b.get('context', ''))
if b.get('dockerfile', 'Dockerfile') == 'Dockerfile':
    b.pop('dockerfile', None)
if set(b) - {'context', 'target'} or ctx != os.path.realpath('.') or not b.get('target'):
    sys.exit(3)
print(b['target'])") || build_failed "$svc has a build configuration the clean-context build does not reproduce"
    IMG=$(img "$svc")
    pct=$((20 + 60 * n / ${#SVCS[@]})); n=$((n + 1))
    progress "$pct" "building image $n/${#SVCS[@]}: $svc"
    build_alive "$svc" "$pct" || build_failed "docker build of $svc failed"
  done
  progress 80 "images built"
  rm -rf -- "$CTX"
fi
phase BUILT
# The SHA tags are for humans (and older tooling) only; a bound release starts the IDs, never these tags.
docker tag "$API_IMG" "company-os-api:$SHA"
docker tag "$WORKER_IMG" "company-os-worker:$SHA"
docker tag "$REASONER_IMG" "company-os-reasoner:$SHA"
docker tag "$EGRESS_IMG" "company-os-egress:$SHA"

REV0=$(q 'select version_num from alembic_version')
HEADS=$(run_compose run --rm --no-deps company-api alembic heads | awk 'NF {print $1}') || {
  abort_before_migration "could not read Alembic heads"
}
TARGET_REV=$(single_alembic_head "$HEADS") || {
  abort_before_migration "expected exactly one valid Alembic head"
}
if ! migration_authorized "$REV0" "$TARGET_REV" "$ALLOW_MIGRATION"; then
  abort_before_migration "schema change '$REV0' -> '$TARGET_REV' is not authorized; required authorization is COMPANY_OS_ALLOW_MIGRATION=$TARGET_REV (or the allow_migration_to dispatch input)"
fi
echo "== migrate (new image, before any container switch) from $REV0"
progress 82 "migrating the database"
phase MIGRATING
write_env REV0 "$REV0"; write_env TARGET_REV "$TARGET_REV"
if [ "$REV0" != "$TARGET_REV" ]; then
  python3 -I "$B/recovery-identity.py" migration "$B" "$REV0" "$TARGET_REV"
  if ! run_compose run --rm --no-deps company-api alembic upgrade head; then
  # A migration can use operations whose partial schema effects are not reflected in alembic_version. Preserve a
  # blocking journal state until a human verifies the DB; do not claim a clean rollback based only on old code.
    abort_migration_failure "Alembic upgrade failed from revision $REV0 toward $TARGET_REV"
  fi
fi
phase MIGRATED
echo "alembic now: $(q 'select version_num from alembic_version')"

# The host CLI must be the release CLI before containers switch (a CLI that cannot read the new state
# is how a deploy is mis-operated). A failure here leaves everything as it was, like a failed migration.
phase CLI_INSTALLING
echo "== host CLI"
progress 88 "installing the host CLI"
HCLI="$B/host-cli.sh"   # survives restoring the old source on rollback; root-owned, not a fixed /tmp path
cp scripts/install-host-cli.sh "$HCLI"
chmod +x "$HCLI"
CLI_SUM=$(sed -n 's|  scripts/companyctl$||p' "$REL/main.sha256")
if [[ "$CLI_SUM" =~ ^[0-9a-f]{64}$ ]] && "$HCLI" install --backup "$B" --expect-sha256 "$CLI_SUM"; then
  :
else
  abort_host_cli_install "could not install the release companyctl on the host (missing or mismatched release checksum, or install failure)"
fi

rollback_release() {
  status=$?
  trap - ERR
  set +e
  echo "== release failed; restoring $OLD_SHA"
  rollback_core
  case "$ROLLBACK_RESULT" in
    VERIFIED) phase ROLLED_BACK_VERIFIED; echo "ROLLBACK VERIFIED: $OLD_SHA" ;;
    PARTIAL) phase ROLLED_BACK_PARTIAL
      echo "PARTIALLY ROLLED BACK: restoration of $OLD_SHA is unverified; database/container identity, image tags, host CLI/source files or the cron schedule could NOT be verified; manual recovery required; check $B" >&2 ;;
    *) phase ROLLBACK_FAILED; echo "ROLLBACK FAILED: manual recovery required; backup is $B" >&2 ;;
  esac
  exit "$status"
}
# The one bootstrap release is consumed exactly once, immediately before the switch (§7): these pins can never be
# used again, even after a failed switch. A retry needs a new founder `prepare --retry --bootstrap` and new pins.
if [ "$BOOTSTRAP" = yes ]; then
  ( set -C; : > "$BUILD_MARKER" ) 2>/dev/null \
    || abort_host_cli_install "these bootstrap pins were already consumed; a retry needs a new prepare --retry --bootstrap and a new pins release"
fi

echo "== switch containers"
progress 90 "switching containers and verifying health"
# The SHA tag (set above) is what a legacy container runs; a bound release starts the pinned IDs.
phase SWITCHING
trap rollback_release ERR
# Nested functions do not inherit ERR traps: propagate their failure to rollback explicitly.
COMPANY_OS_GIT_SHA=$SHA COMPANY_OS_IMAGE_TAG=$SHA up_services bound || rollback_release
# The containers must run exactly the bound IDs: a tag that moved, a fallback to `latest`, or an older image is a
# failed switch, and the ERR trap rolls the previous release back.
if [ "$BOUND" = yes ] || [ "$BOOTSTRAP" = yes ]; then
  for pair in "company-api:$BOUND_API" "company-worker:$BOUND_WORKER" \
              "company-reasoner:$BOUND_REASONER" "company-reasoner-egress:$BOUND_EGRESS"; do
    got=$(docker inspect "${pair%%:*}" --format '{{.Image}}')
    [ "$got" = "${pair#*:}" ] || { echo "${pair%%:*} runs ${got:-nothing}, not the bound ${pair#*:}" >&2; false; }
  done
fi
for _ in $(seq 1 30); do
  docker inspect company-api --format '{{.State.Health.Status}}' | grep -q healthy && break
  sleep 3
done
fence_live || { echo "pause fence is not live on the new company-api (CODER_PAUSE_FENCE_ENABLED != true)" >&2; false; }
"$GATE" "$SHA"
install_cron "$REL/main.sha256" || rollback_release   # last mutation, only for a verified release
trap - ERR
phase VERIFIED
echo "RELEASE VERIFIED: $SHA (worker restarted at $(docker inspect company-worker --format '{{.State.StartedAt}}'), was $W0)"
progress 100 "release verified"
promote_rollback_anchors || echo "WARN: could not promote every rollback anchor of this release; a component that was not promoted keeps its previous anchor" >&2
if ! scripts/companyctl snapshot; then
  echo "WARN: release is healthy but generated snapshot refresh failed; daily cron will retry." >&2
fi
REMOTE
if [ "${COMPANY_OS_DEPLOY_TRANSPORT:-legacy}" = staged ]; then
  # Private upload directory + root-owned, hash-checked copy (scripts/deploy-stage-bootstrap.sh, sent on
  # stdin so it is never a file on the host). Opt-in until the founder approves it for CI.
  sha256() { shasum -a 256 "$1" | cut -c1-64; }
  H_SCRIPT=$(sha256 "$TMP/company-os-deploy.sh"); H_SRC=$(sha256 "$TMP/src.tgz"); H_SUMS=$(sha256 "$TMP/main.sha256")
  STAGE=$(ssh "$HOST" 'umask 077 && mktemp -d /tmp/company-os-deploy.XXXXXXXXXX')
  [[ "$STAGE" =~ ^/tmp/company-os-deploy\.[A-Za-z0-9]{10}$ ]] || { echo "ABORT: unexpected staging directory: $STAGE"; exit 1; }
  trap 'rm -rf "$TMP"; ssh "$HOST" "rm -rf -- $STAGE" || true' EXIT
  scp -q "$TMP/src.tgz" "$TMP/main.sha256" "$TMP/company-os-deploy.sh" "$HOST:$STAGE/"
  boot() {
    ssh "$HOST" "sudo AUTOMATIC=$AUTOMATIC ALLOW_MIGRATION=$ALLOW_MIGRATION ACK_OVERRIDES='${ACK_OVERRIDES:-}' ACK_COMPOSE='${ACK_COMPOSE:-}' bash -s -- $STAGE $SHA $H_SCRIPT $H_SRC $H_SUMS $1" \
      < scripts/deploy-stage-bootstrap.sh
  }
  echo "deploying origin/main $SHA (staged transport)"
  if [ "$APPLY" = "--apply" ]; then boot apply; follow; fi
  boot readonly
  exit 0
fi
scp -q "$TMP/src.tgz" "$TMP/main.sha256" "$TMP/company-os-deploy.sh" "$HOST":/tmp/
echo "deploying origin/main $SHA"
if [ "$APPLY" = "--apply" ]; then
  ssh "$HOST" "sudo SHA=$SHA APPLY=--apply DETACH=1 AUTOMATIC=$AUTOMATIC ALLOW_MIGRATION=$ALLOW_MIGRATION ACK_OVERRIDES='${ACK_OVERRIDES:-}' ACK_COMPOSE='${ACK_COMPOSE:-}' bash /tmp/company-os-deploy.sh"
  follow
fi
ssh "$HOST" "sudo SHA=$SHA APPLY= AUTOMATIC=$AUTOMATIC ALLOW_MIGRATION=$ALLOW_MIGRATION bash /tmp/company-os-deploy.sh"
