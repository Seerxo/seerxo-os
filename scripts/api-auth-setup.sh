#!/usr/bin/env bash
# Founder-run, on <production-host>: put the API caller tokens in place (app/api_auth.py, step 2).
#
#   ssh <production-host> 'sudo bash -s'           < scripts/api-auth-setup.sh   # read-only report
#   ssh <production-host> 'sudo bash -s -- --apply' < scripts/api-auth-setup.sh  # write
#
# --apply: backs up both .env files, adds any missing COMPANY_API_TOKEN_* (openssl rand -hex 32)
# and API_HOST_IPS (the company-os network gateway) to /opt/company-os/.env, and copies the
# youtube token into /opt/youtube-auto/.env as COMPANY_OS_TOKEN. Existing values are never
# replaced. Token values are never printed; only set/unset and length. Nothing is restarted.
# An unset API_AUTH_MODE means enforce, so --apply pins API_AUTH_MODE=log when it is missing:
# the rollout stays in log mode until the founder flips it. An existing value is kept.
set -euo pipefail
APPLY=${1:-}
[ -z "$APPLY" ] || [ "$APPLY" = --apply ] || { echo "usage: bash -s [-- --apply]"; exit 2; }
CO=${CO:-/opt/company-os/.env} YT=${YT:-/opt/youtube-auto/.env} BK=${BK:-/opt/company-os/backups}
for f in "$CO" "$YT"; do [ -f "$f" ] || { echo "ABORT: $f not found"; exit 1; }; done

val() { sed -n "s/^$2=//p" "$1" | tail -n 1; }
add() {  # add <file> <line>: never glued onto a last line that lacks its newline
  [ -z "$(tail -c 1 "$1")" ] || printf '\n' >> "$1"
  printf '%s\n' "$2" >> "$1"
}
show() {  # show <file> <name>: presence and length only
  local v; v=$(val "$1" "$2")
  if [ -n "$v" ]; then printf '  %-28s set (%s chars)\n' "$2" "${#v}"; else printf '  %-28s unset\n' "$2"; fi
}
GATEWAY=$(docker network inspect company-os --format '{{range .IPAM.Config}}{{.Gateway}} {{end}}' | awk '{print $1}')

echo "== now"
for n in COMPANY_API_TOKEN_ADMIN COMPANY_API_TOKEN_DESK COMPANY_API_TOKEN_YOUTUBE; do show "$CO" "$n"; done
echo "  API_HOST_IPS                 $(val "$CO" API_HOST_IPS) (company-os gateway: ${GATEWAY:-unknown})"
echo "  API_AUTH_MODE                $(val "$CO" API_AUTH_MODE) (empty = enforce)"
show "$YT" COMPANY_OS_TOKEN
if [ "$APPLY" != --apply ]; then echo "read-only; re-run with -- --apply"; exit 0; fi

[ -n "$GATEWAY" ] || { echo "ABORT: company-os network gateway not found"; exit 1; }
S=$(date -u +%Y%m%dT%H%M%SZ)
install -m 600 "$CO" "$BK/env-$S.company-os"
install -m 600 "$YT" "$BK/env-$S.youtube-auto"
echo "== backups: $BK/env-$S.{company-os,youtube-auto} (mode 600)"

# Appends keep each file's owner and mode: youtube-auto's container reads its .env as non-root.
for n in COMPANY_API_TOKEN_ADMIN COMPANY_API_TOKEN_DESK COMPANY_API_TOKEN_YOUTUBE; do
  [ -n "$(val "$CO" "$n")" ] || add "$CO" "$n=$(openssl rand -hex 32)"
done
[ -n "$(val "$CO" API_HOST_IPS)" ] || add "$CO" "API_HOST_IPS=$GATEWAY"
[ -n "$(val "$CO" API_AUTH_MODE)" ] || add "$CO" "API_AUTH_MODE=log"
YT_TOKEN=$(val "$CO" COMPANY_API_TOKEN_YOUTUBE)
if [ -z "$(val "$YT" COMPANY_OS_TOKEN)" ]; then
  add "$YT" "COMPANY_OS_TOKEN=$YT_TOKEN"
elif [ "$(val "$YT" COMPANY_OS_TOKEN)" != "$YT_TOKEN" ]; then
  echo "WARN: youtube-auto COMPANY_OS_TOKEN differs from COMPANY_API_TOKEN_YOUTUBE; left as is"
fi

echo "== after"
for n in COMPANY_API_TOKEN_ADMIN COMPANY_API_TOKEN_DESK COMPANY_API_TOKEN_YOUTUBE; do show "$CO" "$n"; done
echo "  API_HOST_IPS                 $(val "$CO" API_HOST_IPS)"
echo "  API_AUTH_MODE                $(val "$CO" API_AUTH_MODE)"
show "$YT" COMPANY_OS_TOKEN
echo "== next (nothing was restarted)"
echo "  1. company-api reads the new env on its next recreate (deploy-company-os.sh --apply)."
echo "  2. youtube-auto sends COMPANY_OS_TOKEN after its own PR is deployed (not during a render)."
echo "  rollback: cp $BK/env-$S.company-os $CO; cp $BK/env-$S.youtube-auto $YT  (cp keeps the target's mode)"
