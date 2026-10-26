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
# Recreate the release containers; the caller sets COMPANY_OS_GIT_SHA/COMPANY_OS_IMAGE_TAG. With FENCE_KEEP=yes the
# verified pause fence goes back onto company-api (only there: the other services keep the base file alone).
up_services() {
  if [ "${FENCE_KEEP:-no}" = yes ]; then
    fence_verified || { echo "pause fence overlay is no longer the verified file; refusing to apply it" >&2; return 1; }
    docker compose -f docker-compose.yml -f "$FENCE_OVERLAY" up -d --no-deps --no-build company-api || return 1
    docker compose up -d --no-deps --no-build company-worker company-reasoner-egress company-reasoner
  else
    docker compose up -d --no-deps --no-build company-api company-worker company-reasoner-egress company-reasoner
  fi
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
# verify-release-manifest (B6 step 8): once release_host has bootstrapped this host (its ledger exists), an --apply
# is accepted only from release_host's own transaction: the ledger is pending (a manifest digest) for exactly this SHA,
# written after it verified the signed manifest, and RELEASE_DIR is exactly the staged directory release_host named in
# that same write (pending_release_dir). Another upload cannot claim the pending SHA. Checked before anything on the host
# changes; a refusal touches nothing, not even the named directory (it may be a live transaction's). An unreadable,
# malformed or incomplete ledger refuses. No ledger (not bootstrapped), a read-only run and RECOVER_DIR are unchanged.
# The path is fixed and release_host refuses any other (release_host.LEDGER); tests rewrite the script text, as for
# /opt/company-os; there is deliberately no override.
RELEASE_LEDGER=/var/lib/release-host/ledger/ledger.json
if [ "${APPLY:-}" = --apply ] && [ -z "${RECOVER_DIR:-}" ] && { [ -e "$RELEASE_LEDGER" ] || [ -L "$RELEASE_LEDGER" ]; }; then
  PENDING=$(python3 -I -c '
import json, re, sys
s = json.load(open(sys.argv[1]))
if not isinstance(s, dict):
    sys.exit(1)
d, sha, rel = s.get("pending"), s.get("pending_sha"), s.get("pending_release_dir")
if d is None and sha is None and rel is None:
    print("none none none"); sys.exit(0)                # bootstrapped, nothing pending
ok = (isinstance(d, str) and re.fullmatch(r"[0-9a-f]{64}", d) and isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha)
      and isinstance(rel, str) and re.fullmatch(r"/run/company-os-deploy/[0-9a-f]{40}\.[A-Za-z0-9]{8}", rel)
      and rel.rsplit("/", 1)[-1].startswith(sha + "."))            # named for this release, as release_host names it
if not ok:
    sys.exit(1)                                         # an incomplete pending transaction is never authority
# Who signed it. Anything but "founder" (gateway, absent from an older verifier, malformed) is held to the running images.
print(sha, rel, "founder" if s.get("pending_approver") == "founder" else "gateway")' "$RELEASE_LEDGER" 2>/dev/null) \
    || PENDING="<unreadable-or-incomplete-ledger> - -"
  read -r PENDING_SHA PENDING_DIR PENDING_APPROVER <<< "$PENDING"
  if [[ ! "${SHA:-}" =~ ^[0-9a-f]{40}$ ]] || [ "$PENDING_SHA" != "$SHA" ] || [ "${RELEASE_DIR:-}" != "$PENDING_DIR" ]; then
    echo "ABORT: verify-release-manifest: this host is bootstrapped; it applies only the staged release the host verifier marked pending (pending_sha=${PENDING_SHA:-none}). Release by signing a manifest into the founder inbox; break-glass is a founder ledger recovery."
    exit 1
  fi
fi
# Only a founder-signed release may change what the images contain; unset when the host is not bootstrapped.
HOLD_IMAGES=no
[ "${PENDING_APPROVER:-founder}" = founder ] || HOLD_IMAGES=yes
# Nothing sourced later (./.env runs as root under set -a) may change what the gate decided on.
readonly APPLY SHA RELEASE_DIR REL RECOVER_DIR HOLD_IMAGES
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
# all four back on the images that were running (tagged rollback-$S before anything changed).
restore_tags() {
  [ -n "${API_IMG:-}" ] || return 0   # images not resolved yet: nothing was retagged
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
      S|NEW_SHA|OLD_SHA|API_IMG|WORKER_IMG|REASONER_IMG|EGRESS_IMG|R_OLD|E_OLD|FENCE_KEEP|FENCE_BEFORE)
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
  local cli_restored=yes
  # The previous release goes back with the fence it had (journals older than FENCE_BEFORE: the forward decision).
  local fence_before=${FENCE_BEFORE:-${FENCE_KEEP:-no}}
  local FENCE_KEEP=no   # set below from fence_before, once the old source is back
  ROLLBACK_RESULT=FAILED
  restore_source "$B" || cli_restored=no
  if [ -x "$B/host-cli.sh" ] && [ -f "$B/cli-files-before.sha256" ]; then
    "$B/host-cli.sh" restore --backup "$B" --root /opt/company-os --sums "$B/cli-files-before.sha256" || cli_restored=no
  fi
  restore_cron "$B" || cli_restored=no   # the schedule is restored and verified like the CLI; unverified is reported as partial
  docker tag "company-os-api:rollback-$S" "${API_IMG:-company-os-api:latest}" 2>/dev/null || cli_restored=no
  docker tag "company-os-worker:rollback-$S" "${WORKER_IMG:-company-os-worker:latest}" 2>/dev/null || cli_restored=no
  [ -z "${R_OLD:-}" ] || [ -z "${REASONER_IMG:-}" ] || docker tag "company-os-reasoner:rollback-$S" "$REASONER_IMG" || cli_restored=no
  [ -z "${E_OLD:-}" ] || [ -z "${EGRESS_IMG:-}" ] || docker tag "company-os-egress:rollback-$S" "$EGRESS_IMG" || cli_restored=no
  # A fence that is not the verified file is never applied, but the old release is still recreated (from the base
  # file) so the candidate is never left running; not getting the old configuration back is PARTIAL.
  case $fence_before in
    yes) if fence_verified; then FENCE_KEEP=yes
         else cli_restored=no; echo "pause fence overlay is not the verified file; previous release restored without it" >&2; fi ;;
    unverified) cli_restored=no; echo "previous release ran an unreviewed fence overlay; restored without it" >&2 ;;
  esac
  COMPANY_OS_GIT_SHA=$OLD_SHA COMPANY_OS_IMAGE_TAG=rollback-$S up_services || true
  for _ in $(seq 1 30); do
    docker inspect company-api --format '{{.State.Health.Status}}' | grep -q healthy && break
    sleep 3
  done
  fence_live || { cli_restored=no; echo "pause fence is not live on the restored company-api" >&2; }
  local gate=$B/release-health-gate.sh
  [ -x "$gate" ] || gate=scripts/deploy-health-gate.sh
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
  if [ "$ROLLBACK_RESULT" = PARTIAL ]; then phase ROLLED_BACK_PARTIAL; echo "RECOVERY PARTIAL: containers verified at $OLD_SHA but host CLI/source/schedule/image tags unverified; check against $B" >&2; exit 1; fi
  phase ROLLBACK_FAILED; echo "RECOVERY FAILED: manual recovery required; backup is $B" >&2; exit 1
fi

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
docker tag "$(docker inspect company-api --format '{{.Image}}')" "company-os-api:rollback-$S"
docker tag "$(docker inspect company-worker --format '{{.Image}}')" "company-os-worker:rollback-$S"
# The reasoner may not exist yet (first deploy that creates it): nothing to roll back to then.
R_OLD=$(docker inspect company-reasoner --format '{{.Image}}' 2>/dev/null || true)
[ -z "$R_OLD" ] || docker tag "$R_OLD" "company-os-reasoner:rollback-$S"
# The network boundary rolls back with the app: keep the running egress proxy's image too.
E_OLD=$(docker inspect company-reasoner-egress --format '{{.Image}}' 2>/dev/null || true)
[ -z "$E_OLD" ] || docker tag "$E_OLD" "company-os-egress:rollback-$S"
# From here the pending anchors are this run's own (ANCHORED): only then may cleanup drop them. A partial
# failure drops every pending anchor, so a later VERIFIED run can never promote a stale component's image.
ANCHORED=1
{ anchor_rollback api "company-os-api:rollback-$S" && anchor_rollback worker "company-os-worker:rollback-$S" \
  && { [ -z "$R_OLD" ] || anchor_rollback reasoner "company-os-reasoner:rollback-$S"; } \
  && { [ -z "$E_OLD" ] || anchor_rollback egress "company-os-egress:rollback-$S"; }; } \
  || { drop_pending_anchors; echo "WARN: could not anchor the rollback images; Coolify's nightly cleanup may delete them" >&2; }
W0=$(docker inspect company-worker --format '{{.State.StartedAt}}')

# Everything a recovery needs is on disk before the first mutation; from here until a terminal phase is written,
# an interruption is visible (the next --apply refuses) and recoverable (RECOVER_DIR, see above).
cp -p -- "$0" "$B/deploy-driver.sh" && chmod 0700 "$B/deploy-driver.sh"
: > "$B/deploy.env"
write_env S "$S"; write_env NEW_SHA "$SHA"; write_env OLD_SHA "$OLD_SHA"
write_env R_OLD "$R_OLD"; write_env E_OLD "$E_OLD"; write_env FENCE_KEEP "$FENCE_KEEP"; write_env FENCE_BEFORE "$FENCE_BEFORE"
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
# A compose main cannot parse must not reach `build`/`up`: put the old files back and stop.
if ! docker compose config -q; then
  abort_before_migration "main's docker-compose.yml does not validate on this host"
fi
# Images come from main's compose, not the host's old one: a service main adds (the reasoner
# egress, 2026-10-06) is missing there, and the old reasoner image was the worker's.
# `config --images SERVICE` also lists dependencies (postgres sorted first on 2026-10-05), so
# read each service's own image field from the resolved config.
img() { docker compose config --format json | python3 -c "import json,sys; print(json.load(sys.stdin)['services']['$1']['image'])"; }
API_IMG=$(img company-api); WORKER_IMG=$(img company-worker); REASONER_IMG=$(img company-reasoner); EGRESS_IMG=$(img company-reasoner-egress)
case "$API_IMG $WORKER_IMG $REASONER_IMG $EGRESS_IMG" in
  *company-os-api*company-os-worker*company-os-reasoner*company-os-egress*) ;;
  *) abort_before_migration "unexpected compose images: $API_IMG / $WORKER_IMG / $REASONER_IMG / $EGRESS_IMG" ;;
esac
write_env API_IMG "$API_IMG"; write_env WORKER_IMG "$WORKER_IMG"; write_env REASONER_IMG "$REASONER_IMG"; write_env EGRESS_IMG "$EGRESS_IMG"
# The images are built from a CLEAN copy of the release, never from the host tree. The host overlays the archive
# onto /opt/company-os and keeps files a release no longer ships (and host-only data), and the Dockerfile copies
# whole app/ and migrations/: building from the host tree would put a deleted module or a removed Alembic
# revision into the new image. The context below is exactly the payload manifest, byte-verified, and is thrown
# away after the build. Host-only files stay on the host and never reach an image.
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
# A held release compares against what runs now, so every one of the four must be running before anything builds:
# a missing container would leave nothing to compare (and no previous tag to restore).
HELD_IMAGE=()                                     # by SVCS index (no associative arrays: tests run bash 3.2)
if [ "$HOLD_IMAGES" = yes ]; then
  for i in "${!SVCS[@]}"; do
    HELD_IMAGE[i]=$(docker inspect "${SVCS[i]}" --format '{{.Image}}' 2>/dev/null) || HELD_IMAGE[i]=""
    # A stopped container still has an image; only a running one is what production runs now.
    [ "$(docker inspect "${SVCS[i]}" --format '{{.State.Running}}' 2>/dev/null)" = true ] || HELD_IMAGE[i]=""
    [[ "${HELD_IMAGE[i]}" =~ ^sha256:[0-9a-f]{64}$ ]] \
      || build_failed "a gateway-approved release needs ${SVCS[i]} running to compare its image; a founder-signed release is required"
  done
fi
n=0
for svc in "${SVCS[@]}"; do
  # Only a plain {context: ".", target: T} build is reproduced here; any other build option would be silently
  # dropped from a hand-run `docker build`, so it stops the deploy instead. `docker compose config` normalizes a
  # build without a dockerfile to dockerfile: Dockerfile (compose-go loader/normalize.go), which is exactly what
  # `docker build` reads by default, so that one value is the plain build; any other dockerfile is not.
  TARGET=$(docker compose config --format json | python3 -c "
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
# #192 F4: a gateway (LOW) release was reviewed as a diff that never enters an image, but a cold build cache re-runs
# apt/pip/npm and can fetch different bytes. Such a release must build exactly the images production runs now;
# anything else waits for a founder-signed release. Checked before any tag, migration or container switch.
if [ "$HOLD_IMAGES" = yes ]; then
  for i in "${!SVCS[@]}"; do
    built=$(docker image inspect --format '{{.Id}}' "$(img "${SVCS[i]}")" 2>/dev/null) || built=""
    if [ -z "$built" ] || [ "$built" != "${HELD_IMAGE[i]}" ]; then
      # build_failed: guarded context cleanup, then the same restore of source and tags as any failed build.
      build_failed "a gateway-approved release would change the ${SVCS[i]} image (built ${built:-none}, running ${HELD_IMAGE[i]}); a founder-signed release is required"
    fi
  done
fi
rm -rf -- "$CTX"
phase BUILT
docker tag "$API_IMG" "company-os-api:$SHA"
docker tag "$WORKER_IMG" "company-os-worker:$SHA"
docker tag "$REASONER_IMG" "company-os-reasoner:$SHA"
docker tag "$EGRESS_IMG" "company-os-egress:$SHA"

REV0=$(q 'select version_num from alembic_version')
HEADS=$(docker compose run --rm --no-deps company-api alembic heads | awk 'NF {print $1}') || {
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
if ! docker compose run --rm --no-deps company-api alembic upgrade head; then
  # A migration can use operations whose partial schema effects are not reflected in alembic_version. Preserve a
  # blocking journal state until a human verifies the DB; do not claim a clean rollback based only on old code.
  abort_migration_failure "Alembic upgrade failed from revision $REV0 toward $TARGET_REV"
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
      echo "PARTIALLY ROLLED BACK: containers verified at $OLD_SHA, but the image tags, host CLI/source files or the cron schedule could NOT be verified as restored; check image tags, /usr/local/bin/companyctl, scripts/ and $CRON_FILE against $B" >&2 ;;
    *) phase ROLLBACK_FAILED; echo "ROLLBACK FAILED: manual recovery required; backup is $B" >&2 ;;
  esac
  exit "$status"
}

echo "== switch containers"
progress 90 "switching containers and verifying health"
# The SHA tag (set above) is what the containers run, not the mutable :latest.
phase SWITCHING
trap rollback_release ERR
COMPANY_OS_GIT_SHA=$SHA COMPANY_OS_IMAGE_TAG=$SHA up_services
for _ in $(seq 1 30); do
  docker inspect company-api --format '{{.State.Health.Status}}' | grep -q healthy && break
  sleep 3
done
fence_live || { echo "pause fence is not live on the new company-api (CODER_PAUSE_FENCE_ENABLED != true)" >&2; false; }
"$GATE" "$SHA"
install_cron "$REL/main.sha256"   # last mutation, only for a verified release; a failure here rolls the release back (ERR trap)
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
