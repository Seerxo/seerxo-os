#!/usr/bin/env bash
# Founder-run on <production-host> (as root): rotate the Company OS Postgres role password.
#
#   ssh <production-host> 'sudo bash -s' < scripts/rotate-postgres-password.sh              read-only preflight
#   ssh <production-host> 'sudo bash -s -- --apply' < scripts/rotate-postgres-password.sh   rotate + verify
#
# Never prints a password. The new one is generated here (hex: URL-safe in DATABASE_URL), goes
# to psql on stdin (not argv), and is written only to /opt/company-os/.env (mode 600).
# Order: back up .env -> ALTER ROLE -> update .env -> recreate company-api + company-worker
# (they read DATABASE_URL at start) -> verify new works, old fails, API healthy, worker beats.
# Any failed verification restores the old password and the old .env automatically.
# company-postgres is not recreated: its env still shows the old password, which no longer works.
set -euo pipefail
set +x
cd /opt/company-os
APPLY=${1:-}
umask 077
PGU=$(sed -n 's/^POSTGRES_USER=//p' .env | tail -1); PGU=${PGU:-company}
PGD=$(sed -n 's/^POSTGRES_DB=//p' .env | tail -1); PGD=${PGD:-company}
OLD=$(sed -n 's/^POSTGRES_PASSWORD=//p' .env | tail -1)
[ -n "$OLD" ] || { echo "ABORT: POSTGRES_PASSWORD not found in .env"; exit 1; }

# Password-checked connection test. Not 127.0.0.1: the official image trusts loopback
# (pg_hba "host all all 127.0.0.1/32 trust"), which made every password "work" on 2026-10-05.
# The service name resolves to the container's network address, covered by scram-sha-256.
# `-e PGPASSWORD` without a value copies it from the docker CLI's environment (never in argv).
try_pw() { PGPASSWORD="$1" docker exec -e PGPASSWORD company-postgres psql -h company-postgres -U "$PGU" -d "$PGD" -Atc 'select 1' >/dev/null 2>&1; }
BOGUS=$(openssl rand -hex 16)

echo "== preflight"
echo ".env mode        : $(stat -c %a .env)"
echo "role / database  : $PGU / $PGD"
echo "auth enforced    : $(try_pw "$BOGUS" && echo 'NO - a random password was accepted' || echo 'yes (random password rejected)')"
echo "current password : $(try_pw "$OLD" && echo 'works' || echo 'DOES NOT WORK')"
echo "api / worker     : $(docker inspect company-api --format '{{.State.Health.Status}}') / $(curl -fsS localhost:8099/desk/state | python3 -c 'import json,sys; print(json.load(sys.stdin)["worker"]["status"])' 2>/dev/null || echo unknown)"
echo "DATABASE_URL in .env: $(grep -c '^DATABASE_URL=' .env || true) line(s)"
echo "clients          :"
docker exec company-postgres psql -U "$PGU" -d "$PGD" -Atc \
  "select c.who || ': ' || c.n from (select coalesce(nullif(application_name,''),'?') || ' from ' ||
   coalesce(host(client_addr),'local') as who, count(*) as n from pg_stat_activity
   where datname = current_database() and pid <> pg_backend_pid() group by 1) c order by 1" | sed 's/^/  /'
RUN=$(docker exec company-postgres psql -U "$PGU" -d "$PGD" -Atc "select string_agg(key, ', ') from tasks where status = 'IN_PROGRESS'")
echo "in-progress tasks: ${RUN:-none}"
[ "$APPLY" = "--apply" ] || { echo "read-only run; nothing changed. Re-run with --apply."; exit 0; }

! try_pw "$BOGUS" || { echo "ABORT: password auth is not enforced for the test path; the checks would prove nothing"; exit 1; }
try_pw "$OLD" || { echo "ABORT: the current password does not work; investigate before rotating"; exit 1; }
[ -z "$RUN" ] || { echo "ABORT: a task is IN_PROGRESS; recreating the worker would cut it"; exit 1; }
exec 9>/var/lock/company-os-deploy.lock
flock -n 9 || { echo "ABORT: a Company OS deploy is running"; exit 1; }

S=$(date -u +%Y%m%dT%H%M%SZ)
cp -p .env ".env.bak-rotate-$S"; chmod 600 ".env.bak-rotate-$S"
NEW=$(openssl rand -hex 32)

set_role_pw() {  # SQL on stdin; the literal never appears in argv or logs
  printf "ALTER ROLE \"%s\" WITH PASSWORD '%s';\n" "$PGU" "$1" |
    docker exec -i company-postgres psql -U "$PGU" -d "$PGD" -v ON_ERROR_STOP=1 -q >/dev/null
}
rollback() {
  echo "== ROLLBACK: restoring the old password and .env"
  set_role_pw "$OLD" || echo "  ALTER ROLE back FAILED: run it by hand with the value in .env.bak-rotate-$S"
  cp -p ".env.bak-rotate-$S" .env
  scripts/compose-bound.sh up -d --no-deps --no-build company-api company-worker || echo "  compose up FAILED: check containers"
  echo "  old password works: $(try_pw "$OLD" && echo PASS || echo FAIL)"
  exit 1
}

echo "== rotate"
set_role_pw "$NEW" || { echo "ALTER ROLE failed; nothing changed"; exit 1; }
python3 - "$NEW" <<'PY' || rollback
import re, sys
new = sys.argv[1]
text = open(".env").read()
old = re.search(r"^POSTGRES_PASSWORD=(.*)$", text, re.M).group(1)
text = re.sub(r"^POSTGRES_PASSWORD=.*$", "POSTGRES_PASSWORD=" + new, text, flags=re.M)
text = "\n".join(l.replace(":" + old + "@", ":" + new + "@") if l.startswith("DATABASE_URL=") else l
                 for l in text.split("\n"))
open(".env", "w").write(text)
PY
chmod 600 .env
echo "  role password changed, .env updated (backup .env.bak-rotate-$S)"

echo "== recreate company-api + company-worker"
scripts/compose-bound.sh up -d --no-deps --no-build company-api company-worker >/dev/null || rollback
for _ in $(seq 1 30); do
  docker inspect company-api --format '{{.State.Health.Status}}' | grep -q healthy && break; sleep 2
done

echo "== verify"
ok=1
check() { if eval "$2"; then echo "  PASS  $1"; else echo "  FAIL  $1"; ok=0; fi; }
check "new password works over TCP"     'try_pw "$NEW"'
check "old password rejected"           '! try_pw "$OLD"'
check "random password rejected"        '! try_pw "$BOGUS"'
check "api healthy"                     'docker inspect company-api --format "{{.State.Health.Status}}" | grep -q healthy'
check "api reads the database"          'curl -fsS localhost:8099/desk/state | python3 -c "import json,sys; d=json.load(sys.stdin); assert d[\"overview\"]"'
check "running api env has new password" '[ "$(docker inspect company-api --format "{{range .Config.Env}}{{println .}}{{end}}" | grep -c ":$NEW@")" = 1 ]'
sleep 70  # one worker interval (60 s) plus margin
check "worker heartbeat after restart"  'curl -fsS localhost:8099/desk/state | python3 -c "import json,sys; w=json.load(sys.stdin)[\"worker\"]; assert w[\"status\"] in (\"HEALTHY\",\"PAUSED\"), w[\"status\"]"'
[ "$ok" = 1 ] || rollback
echo "rotated at $S. Old .env kept as .env.bak-rotate-$S (contains the OLD password; delete it once satisfied)."
echo "Scripts that read .env at run time (backup.sh, restore-test.sh) pick up the new value automatically."
