#!/usr/bin/env bash
# Founder-run ON THE LAPTOP: prove the age-encrypted off-site backups (coolify-db, config)
# can actually be restored. The private key never leaves the laptop and the host never sees
# plaintext it did not already have: the host fetches the newest .age object from R2 and
# streams the ciphertext over SSH; decryption and the checks happen here, in a temp dir
# that is deleted on exit.
#
#   AGE_KEY=~/secure/company-backup.key bash scripts/restore-drill-encrypted.sh
#
# Checks: coolify-db decrypts to a PostgreSQL custom-format dump, `pg_restore --list`
# confirms table data, a full archive read succeeds, and a disposable LOCAL PostgreSQL
# restores it and queries every public table. Requires a local Docker socket and an
# already-present postgres:18-alpine image (never pulls). Config is an archive-integrity
# check, not an application recovery. Exit 0 only when every check passed.
set -euo pipefail
KEY=${AGE_KEY:?set AGE_KEY to the age private key file}
HOST=${HOST:-company-os-host}
[ -r "$KEY" ] || { echo "cannot read $KEY"; exit 2; }
command -v age >/dev/null || { echo "age is not installed (brew install age)"; exit 2; }
TMP=$(mktemp -d)
chmod 700 "$TMP"
docker_local() { env -u DOCKER_CONTEXT docker --host "$DOCKER_SOCKET" "$@"; }
cleanup() {
  if [ -s "$TMP/restore.cid" ]; then
    docker_local rm -f "$(cat "$TMP/restore.cid")" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

# Runs on the host as root: newest .age key for $1 on stderr, its ciphertext on stdout.
REMOTE='set -euo pipefail
cd /opt/company-os; set -a; . ./.env; set +a
R2=(-e R2_ACCOUNT_ID -e R2_ACCESS_KEY_ID -e R2_SECRET_ACCESS_KEY -e R2_BUCKET -e R2_ENDPOINT -e R2_PREFIX)
key=$(docker exec "${R2[@]}" company-api python -m scripts.r2 list "$1/" | grep "\.age$" | sort -r | head -1 | cut -f3)
[ -n "$key" ] || { echo "NO_AGE_OBJECT $1" >&2; exit 3; }
echo "$key" >&2
f=backups/.drill-$1.age
docker exec "${R2[@]}" company-api python -m scripts.r2 download "$key" "/backups/.drill-$1.age" >/dev/null
cat "$f"; rm -f "$f"'

FAILED=0
for target in coolify-db config; do
  if ! ssh "$HOST" "sudo bash -c '$REMOTE' _ $target" > "$TMP/$target.age" 2> "$TMP/$target.key"; then
    echo "FAIL $target: $(tail -1 "$TMP/$target.key")"; FAILED=1; continue
  fi
  obj=$(tail -1 "$TMP/$target.key")
  if ! age -d -i "$KEY" -o "$TMP/$target.plain" "$TMP/$target.age" 2>"$TMP/$target.err"; then
    echo "FAIL $target ($obj): decrypt: $(head -1 "$TMP/$target.err")"; FAILED=1; continue
  fi
  case $target in
    coolify-db)
      if [ "$(head -c 5 "$TMP/$target.plain")" != PGDMP ]; then
        echo "FAIL $target ($obj): not a pg_dump custom-format file"; FAILED=1; continue
      fi
      if ! command -v pg_restore >/dev/null; then
        echo "FAIL $target ($obj): pg_restore is required to validate the dump"; FAILED=1; continue
      fi
      if ! listing=$(pg_restore --list "$TMP/$target.plain" 2>&1); then
        echo "FAIL $target ($obj): pg_restore --list failed"; FAILED=1; continue
      fi
      n=$(grep -c "TABLE DATA" <<< "$listing" || true)
      [ "$n" -gt 0 ] || { echo "FAIL $target ($obj): pg_restore --list shows no table data"; FAILED=1; continue; }
      if ! pg_restore --file=/dev/null < "$TMP/$target.plain"; then
        echo "FAIL $target ($obj): full archive read failed"; FAILED=1; continue
      fi
      # Pin a local socket: never stream decrypted data to a remote Docker context.
      if ! command -v docker >/dev/null; then
        echo "FAIL $target ($obj): local Docker is required for an isolated restore"; FAILED=1; continue
      fi
      DOCKER_SOCKET=${DOCKER_HOST:-}
      if [ -z "$DOCKER_SOCKET" ]; then
        DOCKER_SOCKET=$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null) || true
      fi
      if [[ "$DOCKER_SOCKET" != unix://* ]]; then
        echo "FAIL $target ($obj): a local Unix Docker socket is required"; FAILED=1; continue
      fi
      image=$(docker_local image inspect postgres:18-alpine --format '{{.Id}}' 2>/dev/null) || image=""
      if [[ ! "$image" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        echo "FAIL $target ($obj): postgres:18-alpine must already be available locally"; FAILED=1; continue
      fi
      # Only dump bytes enter stdin: no key, host mounts, ports, or network; SQL runs as postgres, not host root.
      if ! restored=$(docker_local run --rm --init --interactive --pull=never --network=none --read-only --cap-drop=ALL \
        --security-opt=no-new-privileges --user=postgres --memory=1g --cpus=1 --pids-limit=128 \
        --tmpfs=/tmp:rw,noexec,nosuid,nodev,size=512m --tmpfs=/var/lib/postgresql:rw,noexec,nosuid,nodev,size=16m \
        --cidfile="$TMP/restore.cid" --entrypoint=timeout "$image" -s KILL 600 sh -ec "$(cat <<'RESTORE'
work=$(mktemp -d "${TMPDIR:-/tmp}/encrypted-restore.XXXXXX")
trap 'pg_ctl -D "$work/data" -m immediate -w stop >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
initdb -D "$work/data" -U restore_drill -A trust --no-locale >"$work/init.log" 2>&1
pg_ctl -D "$work/data" -l "$work/server.log" -w start -o "-F -k '$work' -c listen_addresses=''" >/dev/null 2>&1
createdb -h "$work" -U restore_drill -T template0 restore_drill
pg_restore -h "$work" -U restore_drill -d restore_drill --no-owner --no-privileges \
  --exit-on-error --single-transaction >"$work/restore.log" 2>&1
psql -X -h "$work" -U restore_drill -d restore_drill -At -v ON_ERROR_STOP=1 >"$work/counts.sql" <<'SQL'
SELECT format('SELECT count(*) FROM %I.%I;', schemaname, tablename)
FROM pg_tables WHERE schemaname = 'public';
SQL
[ -s "$work/counts.sql" ]
counts=$(psql -X -h "$work" -U restore_drill -d restore_drill -At -v ON_ERROR_STOP=1 -f "$work/counts.sql")
printf '%s\n' "$counts" | awk '
  !/^[0-9]+$/ { bad=1 }
  { tables++; rows+=$1 }
  END { if (bad || tables < 1 || rows < 1) exit 1; printf "tables=%d rows=%.0f\n", tables, rows }
'
RESTORE
)" < "$TMP/$target.plain" 2>"$TMP/restore.err"); then
        echo "FAIL $target ($obj): isolated database restore or verification failed"; FAILED=1; continue
      fi
      if [[ ! "$restored" =~ ^tables=[1-9][0-9]*\ rows=[1-9][0-9]*$ ]]; then
        echo "FAIL $target ($obj): invalid restored table/row counts"; FAILED=1; continue
      fi
      detail="$n table-data entries, full archive read; isolated restore $restored; image=$image" ;;
    config)
      if ! listing=$(tar tzf "$TMP/$target.plain" 2>/dev/null); then
        echo "FAIL $target ($obj): not a readable tar.gz"; FAILED=1; continue
      fi
      grep -q "opt/company-os/.env$" <<< "$listing" || { echo "FAIL $target ($obj): .env missing from archive"; FAILED=1; continue; }
      detail="$(wc -l <<< "$listing" | tr -d ' ') entries incl. opt/company-os/.env" ;;
  esac
  echo "OK   $target ($obj): $(wc -c < "$TMP/$target.age" | tr -d ' ') bytes, decrypted, $detail"
done
[ $FAILED = 0 ] && echo "encrypted DB restore and config archive checks PASSED $(date -u +%FT%TZ)" || echo "restore drill FAILED"
exit $FAILED
