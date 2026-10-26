#!/usr/bin/env bash
# Root-side bootstrap of the staged deploy transport. Never stored on the host: the deploy sends it on
# the SSH connection's stdin (`ssh HOST 'sudo bash -s -- ARGS' < deploy-stage-bootstrap.sh`).
#
#   bash -s -- STAGE SHA SCRIPT_SHA256 SRC_SHA256 SUMS_SHA256 apply|readonly [--run-root DIR] [--stage-parent DIR]
#
# STAGE is the deploy user's private upload directory (`mktemp -d /tmp/company-os-deploy.XXXXXXXXXX`).
# Nothing from it is executed or read as root until:
#   1. STAGE is a real directory (no symlink) owned by the sudo invoker (SUDO_UID), mode 0700, directly in
#      a root-owned sticky parent, and named exactly company-os-deploy.<10 alphanumerics>;
#   2. each of company-os-deploy.sh, src.tgz and main.sha256 is a regular file (no symlink), owned by the
#      invoker, with a single hard link;
#   3. the three files are copied into a fresh directory created by root (mktemp -d, 0700) under RUN_ROOT,
#      itself a real directory owned by root with mode 0700;
#   4. the SHA-256 of each COPY equals the hash the deploying side computed before upload.
# Any failure stops before anything executes and removes the root copy. Then the root-owned copy of
# company-os-deploy.sh is exec'ed with RELEASE_DIR pointing at the copies; it removes them when it ends.
set -euo pipefail
die() { echo "ABORT (staged transport): $*" >&2; exit 1; }
[ $# -ge 6 ] || die "usage: STAGE SHA SCRIPT_SHA256 SRC_SHA256 SUMS_SHA256 apply|readonly [--run-root DIR] [--stage-parent DIR]"
STAGE=$1 SHA=$2 H_SCRIPT=$3 H_SRC=$4 H_SUMS=$5 MODE=$6
shift 6
RUN_ROOT=/run/company-os-deploy STAGE_PARENT=/tmp
while [ $# -gt 0 ]; do
  case "$1" in
    --run-root) RUN_ROOT=${2:?}; shift 2 ;;
    --stage-parent) STAGE_PARENT=${2:?}; shift 2 ;;
    *) die "unknown option $1" ;;
  esac
done

hex() { [[ "$1" =~ ^[0-9a-f]{$2}$ ]]; }
hex "$SHA" 40 || die "SHA must be 40 hex characters"
for h in "$H_SCRIPT" "$H_SRC" "$H_SUMS"; do hex "$h" 64 || die "every file hash must be 64 hex characters"; done
case "$MODE" in apply|readonly) ;; *) die "mode must be apply or readonly" ;; esac
# The release mode (deploy-company-os.sh) travels in the sudo environment; re-checked here and again by
# the root copy. Unknown is refused, never defaulted.
case "${AUTOMATIC:-}" in true|false) ;; *) die "deploy mode unknown (AUTOMATIC must be true or false)" ;; esac
# Only the release host may combine automatic mode with ACK_COMPOSE (it calls the driver directly, never this script).
[ "$AUTOMATIC" = false ] || { [ -z "${ACK_OVERRIDES:-}" ] && [ -z "${ACK_COMPOSE:-}" ]; } \
  || die "contradictory: an automatic deploy cannot carry attended acknowledgements"
[[ -z "${ALLOW_MIGRATION:-}" || "${ALLOW_MIGRATION:-}" =~ ^[0-9a-f]{12}$ ]] || die "malformed ALLOW_MIGRATION"
[[ "${SUDO_UID:-}" =~ ^[0-9]+$ ]] || die "must run under sudo (SUDO_UID missing)"
INVOKER=$SUDO_UID ME=$(id -u)

meta() { stat -c '%u %a %h' -- "$1" 2>/dev/null || stat -f '%u %Lp %l' -- "$1"; }
sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum -- "$1"; else shasum -a 256 -- "$1"; fi | cut -c1-64; }

# 1. the staging directory
case "$STAGE" in "$STAGE_PARENT"/company-os-deploy.*) ;; *) die "staging directory is not in $STAGE_PARENT" ;; esac
[[ "${STAGE#"$STAGE_PARENT"/company-os-deploy.}" =~ ^[A-Za-z0-9]{10}$ ]] || die "staging directory name is not company-os-deploy.<10 alphanumerics>"
if [ -L "$STAGE_PARENT" ] || [ ! -d "$STAGE_PARENT" ]; then die "$STAGE_PARENT is not a real directory"; fi
read -r owner mode _ <<< "$(meta "$STAGE_PARENT")"
[ "$owner" = 0 ] || [ "$owner" = "$ME" ] || die "$STAGE_PARENT is not owned by root"
if (( 8#$mode & 8#022 )) && [ ! -k "$STAGE_PARENT" ]; then die "$STAGE_PARENT is writable by others without the sticky bit"; fi
if [ -L "$STAGE" ] || [ ! -d "$STAGE" ]; then die "$STAGE is not a real directory"; fi
read -r owner mode _ <<< "$(meta "$STAGE")"
[ "$owner" = "$INVOKER" ] || die "$STAGE is owned by uid $owner, not the deploying user $INVOKER"
[ "$mode" = 700 ] || die "$STAGE has mode $mode, not 700"

# 2. the uploaded files
FILES="company-os-deploy.sh src.tgz main.sha256"
for f in $FILES; do
  p="$STAGE/$f"
  if [ -L "$p" ] || [ ! -f "$p" ]; then die "$f is missing or not a regular file"; fi
  read -r owner mode links <<< "$(meta "$p")"
  [ "$owner" = "$INVOKER" ] || die "$f is owned by uid $owner, not the deploying user $INVOKER"
  [ "$links" = 1 ] || die "$f has $links hard links"
done

# 3. a fresh root-owned private copy
# A concurrent deploy may create it first; whoever did, the checks below decide whether it is trusted.
if [ ! -e "$RUN_ROOT" ] && [ ! -L "$RUN_ROOT" ]; then mkdir -m 0700 -- "$RUN_ROOT" 2>/dev/null || true; fi
if [ -L "$RUN_ROOT" ] || [ ! -d "$RUN_ROOT" ]; then die "$RUN_ROOT is not a real directory"; fi
read -r owner mode _ <<< "$(meta "$RUN_ROOT")"
[ "$owner" = "$ME" ] && [ "$mode" = 700 ] || die "$RUN_ROOT must be owned by uid $ME with mode 700 (is uid $owner, mode $mode)"
D=$(mktemp -d "$RUN_ROOT/$SHA.XXXXXXXX")
trap 'rm -rf -- "$D"' EXIT
for f in $FILES; do
  cat -- "$STAGE/$f" > "$D/$f"
  chmod 0600 "$D/$f"
done

# 4. integrity of what root will actually read
[ "$(sha "$D/company-os-deploy.sh")" = "$H_SCRIPT" ] || die "company-os-deploy.sh does not match its hash"
[ "$(sha "$D/src.tgz")" = "$H_SRC" ] || die "src.tgz does not match its hash"
[ "$(sha "$D/main.sha256")" = "$H_SUMS" ] || die "main.sha256 does not match its hash"

trap - EXIT
echo "staged transport: verified $D"
if [ "$MODE" = apply ]; then APPLY=--apply DETACH=1; else APPLY= DETACH=; fi
exec env SHA="$SHA" APPLY="$APPLY" DETACH="$DETACH" AUTOMATIC="$AUTOMATIC" ALLOW_MIGRATION="${ALLOW_MIGRATION:-}" \
  ACK_OVERRIDES="${ACK_OVERRIDES:-}" ACK_COMPOSE="${ACK_COMPOSE:-}" \
  RELEASE_DIR="$D" bash "$D/company-os-deploy.sh"
