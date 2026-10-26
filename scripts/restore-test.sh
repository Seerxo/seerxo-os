#!/usr/bin/env bash
# Periodic restore verification (§76 — "an untested backup is a belief, not a backup").
#
# Restores the newest company-db dump into a throwaway database, checks that the schema
# and rows actually came back, then drops it. Result is reported to Company OS; a failure
# emits RESTORE_VERIFICATION_FAILED and pages the founder.
#
# Prefers the R2 copy when credentials exist, because the off-site copy is the one that
# matters in a disaster. Falls back to the newest local dump.
set -uo pipefail

ENV_FILE=${ENV_FILE:-/opt/company-os/.env}
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

BACKUP_LOCAL_DIR=${BACKUP_LOCAL_DIR:-/opt/company-os/backups}
COMPANY_API=${COMPANY_API:-http://company-api:8000}
API_CONTAINER=${API_CONTAINER:-company-api}
# R2 credentials live only in the host .env (sourced above). They are passed to the one
# `docker exec` that needs them, never stored in the API container's environment.
R2_ENV=(-e R2_ACCOUNT_ID -e R2_ACCESS_KEY_ID -e R2_SECRET_ACCESS_KEY -e R2_BUCKET -e R2_ENDPOINT -e R2_PREFIX)
PG_CONTAINER=${PG_CONTAINER:-company-postgres}
PGUSER_=${POSTGRES_USER:-company}
SCRATCH="restore_test_$(date -u +%Y%m%d%H%M%S)"
LOG=$BACKUP_LOCAL_DIR/restore-test.log

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG"; }

# Optional, founder-run mode (never from cron: it reads the whole ~17 GB renders object from R2):
#   restore-test.sh renders <key> [<sha256>]   key as backup.log prints it, with or without the "r2:" prefix;
#                                              sha256 = the archive SHA-256 the backup reported (anchors the proof)
# Streams the archive once and checks every file against its manifest (scripts/r2.py verify-restore).
# Nothing is extracted to disk.
if [ "${1:-}" = renders ]; then
  LOG=$BACKUP_LOCAL_DIR/renders-restore.log   # never mixed into the DB restore-test log the runbooks grep
  KEY=${2:-}; KEY=${KEY#r2:}; SUM=${3:-}
  [[ "$KEY" =~ ^renders/[A-Za-z0-9._/-]+\.tar\.gz$ && ! "$KEY" =~ (^|/)\.\.?(/|$) && "$KEY" != *//* ]] \
    || { log "renders: refused, expected a renders/...tar.gz key without . or .. segments"; exit 2; }
  [[ -z "$SUM" || "$SUM" =~ ^[0-9a-f]{64}$ ]] || { log "renders: refused, sha256 must be 64 lowercase hex"; exit 2; }
  # Runs inside the live company-api container (no extra limits): start it off-peak.
  log "=== renders restore verification start key=$KEY ==="
  if OUT=$(docker exec -i "${R2_ENV[@]}" "$API_CONTAINER" python -m scripts.r2 verify-restore "$KEY" ${SUM:+"$SUM"} 2>>"$LOG"); then
    log "renders: $OUT"
    if [ -n "$SUM" ]; then log "=== renders restore verification PASSED (anchored to the backup's reported sha256) ==="
    else log "=== renders restore verification PASSED (unanchored: manifest only, no reported sha256 given) ==="; fi
    exit 0
  fi
  log "renders: ${OUT:-no output}"
  log "=== renders restore verification did not pass ==="
  exit 1
fi

SOURCE_UPLOADED=0
SOURCE_DESTINATION=""

report() {  # report <ok> <error> [expected]
  # Routed through the container: `company-api` is a Docker network name the host
  # cannot resolve, and §110 says do not open a port just for this.
  local size="" checksum="" json
  if [ -n "${DUMP:-}" ] && [ -s "$DUMP" ]; then
    read -r size checksum <<< "$(python3 - "$DUMP" <<'PY'
import hashlib, os, sys
path = sys.argv[1]
digest = hashlib.sha256()
with open(path, "rb") as source:
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
print(os.path.getsize(path), digest.hexdigest())
PY
)"
  fi
  json=$(OK="$1" ERR="$2" EXP="${3:-0}" UP="${SOURCE_UPLOADED:-0}" \
         DEST="${SOURCE_DESTINATION:-}" SIZE="$size" SUM="$checksum" python3 -c 'import json, os
size = os.environ["SIZE"]
print(json.dumps({"target": "company-db", "destination": os.environ["DEST"] or None,
                  "size_bytes": int(size) if size.isdigit() else None,
                  "checksum": os.environ["SUM"] or None,
                  "uploaded": os.environ["UP"] == "1",
                  "restore_tested": True,
                  "ok": os.environ["OK"] == "1",
                  "integrity_verified": os.environ["OK"] == "1",
                  "error": os.environ["ERR"] or None,
                  "expected": os.environ["EXP"] == "1"}))')
  if ! printf '%s' "$json" | docker exec -i "$API_CONTAINER" \
      curl -f -sS -o /dev/null -X POST http://localhost:8000/backups \
           -H 'content-type: application/json' --data-binary @-; then
    log "WARN could not report to Company OS"
    return 1
  fi
  return 0
}

cleanup() {
  docker exec -e PGPASSWORD="${POSTGRES_PASSWORD:-}" "$PG_CONTAINER" \
    psql -U "$PGUSER_" -d postgres -c "DROP DATABASE IF EXISTS $SCRATCH;" >/dev/null 2>&1
  [ -n "${TMPDUMP:-}" ] && rm -f "$TMPDUMP"
}
trap cleanup EXIT

log "=== restore test start scratch=$SCRATCH ==="

# ---- pick a dump ------------------------------------------------------------
DUMP=""
if docker exec -i "${R2_ENV[@]}" "$API_CONTAINER" python -m scripts.r2 check >/dev/null 2>&1; then
  KEY=$(docker exec -i "${R2_ENV[@]}" "$API_CONTAINER" python -m scripts.r2 list company-db/ 2>/dev/null \
        | sort -r | head -1 | cut -f3)
  if [ -n "$KEY" ]; then
    # R2 object keys are external data. Keep them out of generated Python/shell source;
    # pass the key as one argv value to the existing downloader instead.
    log "restoring from R2 company-db object"
    TMPDUMP="$BACKUP_LOCAL_DIR/.restore-test.dump"
    if docker exec -i "${R2_ENV[@]}" "$API_CONTAINER" \
         python /app/scripts/r2.py download "$KEY" /backups/.restore-test.dump >/dev/null 2>/dev/null; then
      DUMP=$TMPDUMP
      SOURCE_UPLOADED=1
      SOURCE_DESTINATION="r2:$KEY"
    else
      log "WARN R2 download failed, falling back to local dump"
    fi
  fi
fi
if [ -z "$DUMP" ]; then
  DUMP=$(ls -1t "$BACKUP_LOCAL_DIR"/*company-db*.dump 2>/dev/null | head -1)
  log "restoring from local dump=${DUMP:-none}"
  SOURCE_UPLOADED=0
  [ -z "$DUMP" ] || SOURCE_DESTINATION="local:$DUMP"
fi
if [ -z "$DUMP" ] || [ ! -s "$DUMP" ]; then
  log "FAIL no company-db dump available to test"
  report 0 "no company-db dump available to restore-test"
  exit 1
fi

# ---- restore into a scratch database ----------------------------------------
if ! docker exec -e PGPASSWORD="${POSTGRES_PASSWORD:-}" "$PG_CONTAINER" \
      psql -U "$PGUSER_" -d postgres -c "CREATE DATABASE $SCRATCH;" >>"$LOG" 2>&1; then
  log "FAIL could not create scratch database"
  report 0 "could not create scratch database $SCRATCH"
  exit 1
fi

if ! docker exec -i -e PGPASSWORD="${POSTGRES_PASSWORD:-}" "$PG_CONTAINER" \
      pg_restore -U "$PGUSER_" -d "$SCRATCH" --no-owner --no-privileges < "$DUMP" >>"$LOG" 2>&1; then
  # Any pg_restore error fails the test. The checks below only sample a few tables, so a dump cut off
  # near its end (every table created, the later tables' rows missing) used to PASS. Production never
  # logged a pg_restore error before this became fatal (2026-10-09: 4 runs, 0 warnings).
  log "FAIL pg_restore reported errors (details above in $LOG)"
  report 0 "pg_restore reported errors: the dump does not restore completely"
  exit 1
fi

# ---- verify the restore actually contains the company ----------------------
if ! COUNTS=$(docker exec -e PGPASSWORD="${POSTGRES_PASSWORD:-}" \
  "$PG_CONTAINER" psql -U "$PGUSER_" -d "$SCRATCH" -At -F' ' -c "
    select
      (select count(*) from information_schema.tables where table_schema='public'),
      (select count(*) from business_units),
      (select count(*) from services),
      (select count(*) from events);" 2>>"$LOG"); then
  log "FAIL restore verification query failed"
  report 0 "restore verification query failed"
  exit 1
fi

# A failed query or malformed sample must not count as a restored company.
if ! python3 - "$COUNTS" <<'PY'
import re, sys
counts = sys.argv[1]
if not re.fullmatch(r"[0-9]+(?: [0-9]+){3}", counts):
    sys.exit(1)
tables, business_units, services, events = map(int, counts.split(" "))
sys.exit(0 if tables >= 10 and business_units >= 1 else 1)
PY
then
  log "FAIL restore verification: invalid company counts"
  report 0 "restore verification failed: expected four nonnegative counts, >=10 tables and >=1 business unit"
  exit 1
fi
read -r TABLES BUS SERVICES EVENTS <<< "$COUNTS"
log "restored: tables=$TABLES business_units=$BUS services=$SERVICES events=$EVENTS"

if ! report 1 ""; then
  log "FAIL restore evidence could not be recorded"
  exit 1
fi
if [ "$SOURCE_UPLOADED" = 1 ]; then SOURCE_KIND=r2; else SOURCE_KIND=local; fi
log "=== restore test PASSED === source=$SOURCE_KIND"
