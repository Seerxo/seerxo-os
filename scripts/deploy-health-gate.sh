#!/usr/bin/env bash
# Deterministic post-switch release gate. No model calls.
set -euo pipefail

SHA=${1:?expected release SHA required}
DOCKER=${DOCKER:-docker}
PYTHON=${PYTHON:-python3}

fail() { echo "RELEASE HEALTH FAILED: $*" >&2; exit 1; }
expect() { [ "$1" = "$2" ] || fail "$3 (got ${1:-empty})"; }

expect "$($DOCKER inspect company-api --format '{{.State.Health.Status}}')" healthy "company-api is not healthy"
expect "$($DOCKER exec company-api printenv COMPANY_OS_GIT_SHA)" "$SHA" "company-api SHA differs"
expect "$($DOCKER exec company-worker printenv COMPANY_OS_GIT_SHA)" "$SHA" "company-worker SHA differs"

# The Desk is the API's real read path, not merely a listening socket.
$DOCKER exec company-api curl -fsS http://localhost:8000/desk/state \
  | "$PYTHON" -c 'import json,sys; d=json.load(sys.stdin); assert "worker" in d'

# A restarted worker must produce a fresh database heartbeat before the release is accepted.
$DOCKER exec company-worker python -c '
from datetime import datetime, timedelta, timezone
from app import worker
from app.db import SessionLocal
with SessionLocal() as db:
    heartbeat = worker.worker_state(db).get("heartbeat_at")
    assert heartbeat
    when = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
    assert when >= datetime.now(timezone.utc) - timedelta(minutes=3)
'

expect "$($DOCKER inspect company-reasoner-egress --format '{{.State.Running}}')" true "reasoner egress is not running"
$DOCKER exec company-reasoner curl -fsS http://localhost:8100/health \
  | "$PYTHON" -c 'import json,sys; d=json.load(sys.stdin); assert {"claude", "codex"} <= set(d)'

# The reasoner can reach an allowlisted provider host, but not an arbitrary host.
allowed=$($DOCKER exec company-reasoner curl -s -o /dev/null -m 15 -w '%{http_code}' https://api.anthropic.com/ 2>/dev/null || true)
denied=$($DOCKER exec company-reasoner curl -s -o /dev/null -m 15 -w '%{http_code}' https://example.com/ 2>/dev/null || true)
[ -n "$allowed" ] && [ "$allowed" != 000 ] || fail "reasoner cannot reach an allowlisted provider host"
expect "$denied" 000 "reasoner egress allowed an arbitrary host"

echo "RELEASE HEALTH PASSED: $SHA"
