#!/usr/bin/env bash
# Keep the host's `companyctl` the release's `companyctl`.
#
#   install-host-cli.sh install --backup DIR --expect-sha256 HASH [--bin PATH] [--target PATH]
#   install-host-cli.sh restore --backup DIR [--bin PATH] [--root DIR --sums FILE]
#   install-host-cli.sh status  [--bin PATH] [--target PATH]   (read-only; also reports target owner/group/mode)
#
# /usr/local/bin/companyctl used to be a separate copy that no deploy updated (2026-10-07: the host
# CLI did not know `stage-c status` while the deployed source did). It is now a symlink to the
# release's scripts/companyctl, because companyctl finds its sibling scripts (stage-c-host-facts.py)
# through `readlink -f "$0"`: a copied file would look for them in /usr/local/bin. A symlink also
# means the existing rollback, which restores /opt/company-os from files-before.tgz, restores the CLI
# version with it.
#
# install: the release checksum is REQUIRED (64 hex) and must match the target; the switch is one
#   rename(2) of a temp symlink made in the same directory, so the path is always the old CLI or the
#   new one. Before it, the previous state is written to a temp directory with a manifest (kind, link
#   or file checksum and mode, "complete=1" last) and published by renaming that directory, so an
#   interrupted backup can never look like a valid one.
# restore: refuses a missing, incomplete or corrupted recorded state, puts back exactly the recorded
#   file (bytes and mode verified) or symlink (target verified), and with --root/--sums also verifies
#   that the restored source files (companyctl and its siblings) match their pre-deploy checksums.
#   Any failure exits non-zero: the caller must not report the rollback as verified.
# Nothing here changes a mode or owner of a release file, or writes outside the BIN directory and DIR.
set -euo pipefail

mode=${1:?usage: install-host-cli.sh install|restore|status [options]}; shift
BIN=/usr/local/bin/companyctl TARGET=/opt/company-os/scripts/companyctl BACKUP="" EXPECT="" ROOT="" SUMS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --bin) BIN=${2:?}; shift 2 ;;
    --target) TARGET=${2:?}; shift 2 ;;
    --backup) BACKUP=${2:?}; shift 2 ;;
    --expect-sha256) EXPECT=${2:-}; shift 2 ;;
    --root) ROOT=${2:?}; shift 2 ;;
    --sums) SUMS=${2:?}; shift 2 ;;
    *) echo "install-host-cli: unknown option $1" >&2; exit 2 ;;
  esac
done

dir=$(dirname "$BIN"); name=$(basename "$BIN")
fail() { echo "install-host-cli: $*" >&2; exit 1; }
resolved() { readlink -f -- "$1" 2>/dev/null || true; }
sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -c1-64; }
fmode() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
# The target must exist as a regular file: GNU `readlink -f` still prints a path when only the last
# component is missing, so a dangling link would otherwise compare equal to the release path.
current() { [ -L "$BIN" ] && [ -f "$TARGET" ] && [ -n "$(resolved "$TARGET")" ] && [ "$(resolved "$BIN")" = "$(resolved "$TARGET")" ]; }
field() { sed -n "s/^$1=//p" "$2" | head -n 1; }
state="$BACKUP/host-cli.prev"
tmp="$dir/.$name.new.$$"     # same directory as BIN, so the final rename never crosses a filesystem

# A recorded state is trusted only if complete and self-consistent.
validate_state() {
  [ -d "$state" ] && [ -f "$state/manifest" ] || fail "recorded host CLI state is missing or has no manifest"
  [ "$(tail -n 1 "$state/manifest")" = "complete=1" ] || fail "recorded host CLI state is incomplete"
  case "$(field kind "$state/manifest")" in
    file) [ -f "$state/file" ] && [ "$(sha "$state/file")" = "$(field sha256 "$state/manifest")" ] \
            && [[ "$(field mode "$state/manifest")" =~ ^[0-7]{3,4}$ ]] || fail "recorded host CLI file is corrupted" ;;
    symlink) [ -n "$(field link "$state/manifest")" ] || fail "recorded host CLI symlink is corrupted" ;;
    absent) ;;
    *) fail "recorded host CLI state has an unknown kind" ;;
  esac
}

# Read-only: owner, group and octal mode of a path, and a warning when someone other than its owner
# can write it (a writable directory lets them replace the file by rename). Root-group membership is
# reported, never assumed safe: who is in that group is host configuration this script cannot see.
perms() { stat -c '%U %G %a' -- "$1" 2>/dev/null || stat -f '%Su %Sg %Lp' -- "$1"; }
describe() {
  local path=$1 label=$2 owner group m
  read -r owner group m <<< "$(perms "$path")"
  echo "$label: $path owner=$owner group=$group mode=$m"
  (( 8#$m & 8#020 )) && echo "WARNING: $label is group-writable (group $group): any member can change what companyctl runs"
  (( 8#$m & 8#002 )) && echo "WARNING: $label is world-writable: any local user can change what companyctl runs"
  return 0
}
report_target() {
  local t; t=$(resolved "$BIN")
  if [ -z "$t" ] || [ ! -e "$t" ]; then echo "target: missing (the link resolves to nothing)"; return 0; fi
  describe "$t" target
  describe "$(dirname "$t")" "target directory"
}

case "$mode" in
status)
  # Exit codes are unchanged (0 current, 1 stale or missing); permission findings only add lines.
  if current; then echo "current: $BIN -> $(resolved "$BIN")"; report_target
  elif [ -e "$BIN" ] || [ -L "$BIN" ]; then echo "stale: $BIN is not the release CLI ($(resolved "$BIN"))"; [ ! -L "$BIN" ] || report_target; exit 1
  else echo "missing: $BIN"; exit 1; fi ;;

install)
  [ -n "$BACKUP" ] || { echo "install-host-cli: --backup DIR is required" >&2; exit 2; }
  [[ "$EXPECT" =~ ^[0-9a-f]{64}$ ]] || fail "--expect-sha256 must be the release's 64-hex checksum of scripts/companyctl; refusing to install"
  [ -f "$TARGET" ] && [ ! -L "$TARGET" ] && [ -x "$TARGET" ] || fail "$TARGET is not an executable regular file"
  bash -n "$TARGET" || fail "$TARGET does not parse"
  [ "$(sha "$TARGET")" = "$EXPECT" ] || fail "$TARGET is not the release's companyctl (checksum differs)"
  [ -d "$dir" ] && [ ! -d "$BIN" ] || fail "cannot install at $BIN"
  rm -rf "$dir/.$name.new."* "$BACKUP"/.host-cli.prev.tmp.* 2>/dev/null || true   # leftovers of an interrupted run
  if [ -e "$state" ]; then
    validate_state                                   # keep the FIRST recorded state; never trust a damaged one
  else
    stage="$BACKUP/.host-cli.prev.tmp.$$"
    mkdir -p "$stage"
    if [ -L "$BIN" ]; then kind=symlink; readlink "$BIN" > "$stage/link"
    elif [ -f "$BIN" ]; then kind=file; cp -p "$BIN" "$stage/file"
    else kind=absent; fi
    {
      echo "kind=$kind"
      [ "$kind" != symlink ] || echo "link=$(cat "$stage/link")"
      [ "$kind" != file ] || { echo "sha256=$(sha "$stage/file")"; echo "mode=$(fmode "$stage/file")"; }
      echo "complete=1"                                # last line: written only after everything above
    } > "$stage/manifest"
    mv "$stage" "$state"                              # publish: one directory rename
    validate_state
  fi
  if current; then echo "host CLI already current: $BIN -> $(resolved "$BIN")"
  else
    ln -s "$TARGET" "$tmp"
    mv -f "$tmp" "$BIN"
    echo "host CLI installed: $BIN -> $(resolved "$BIN") (was: $(field kind "$state/manifest"))"
  fi
  current || fail "$BIN does not resolve to $TARGET after install" ;;

restore)
  [ -n "$BACKUP" ] || { echo "install-host-cli: --backup DIR is required" >&2; exit 2; }
  if [ ! -e "$state" ]; then echo "host CLI: nothing recorded, left as is"; exit 0; fi
  validate_state
  rm -f "$dir/.$name.new."* 2>/dev/null || true
  kind=$(field kind "$state/manifest")
  case "$kind" in
    file) cp -p "$state/file" "$tmp"; mv -f "$tmp" "$BIN" ;;
    symlink) ln -s "$(field link "$state/manifest")" "$tmp"; mv -f "$tmp" "$BIN" ;;
    absent) rm -f "$BIN" ;;
  esac
  # Verify what is on disk now, not what we meant to write.
  case "$kind" in
    file) [ -f "$BIN" ] && [ ! -L "$BIN" ] && [ "$(sha "$BIN")" = "$(field sha256 "$state/manifest")" ] \
            && [ "$(fmode "$BIN")" = "$(field mode "$state/manifest")" ] || fail "restored $BIN differs from the recorded file" ;;
    symlink) [ -L "$BIN" ] && [ "$(readlink "$BIN")" = "$(field link "$state/manifest")" ] \
               || fail "restored $BIN is not the recorded symlink" ;;
    absent) [ ! -e "$BIN" ] && [ ! -L "$BIN" ] || fail "$BIN should be absent" ;;
  esac
  if [ -n "$SUMS" ]; then                             # the restored SOURCE the symlink points into
    [ -n "$ROOT" ] && [ -f "$SUMS" ] || fail "--sums needs --root and an existing checksum file"
    while read -r want rel; do
      [[ "$want" =~ ^[0-9a-f]{64}$ ]] && [ -n "$rel" ] || fail "malformed pre-deploy checksum line"
      [ -f "$ROOT/$rel" ] && [ "$(sha "$ROOT/$rel")" = "$want" ] || fail "restored $rel differs from its pre-deploy version"
    done < "$SUMS"
  fi
  if [ "$kind" = symlink ] && [ -n "$ROOT" ]; then
    case "$(resolved "$BIN")" in
      "$(resolved "$ROOT")"/*) [ -x "$(resolved "$BIN")" ] && bash -n "$(resolved "$BIN")" \
                                 || fail "restored release CLI $(resolved "$BIN") is not runnable" ;;
    esac
  fi
  echo "host CLI restored and verified: previous $kind state" ;;

*) echo "usage: install-host-cli.sh install|restore|status" >&2; exit 2 ;;
esac
