#!/usr/bin/env bash
# Host-side fact collector for the watchdog (§68).
#
# Collects only. All evaluation happens in app/watchdog.py, so thresholds live in one
# place. This script holds the Docker privilege it already needs; the API container
# stays unprivileged, because mounting the Docker socket into it would give the API
# root-equivalent control of the host.
#
#   scripts/watchdog.sh | docker exec -i company-api python -m app.watchdog
#
# With --emit (the cron form) it pipes itself and reports failure to deliver.
set -uo pipefail

ENV_FILE=${ENV_FILE:-/opt/company-os/.env}
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a
API_CONTAINER=${API_CONTAINER:-company-api}

# Hours since the newest published long-form video ("" when unknown). Kept as a variable so
# tests/test_watchdog_publish_age.py can run the exact same code against fixtures.
YT_AGE_JS='
const fs=require("fs"),S=process.env.YT_STATE||"/app/state";
const age=t=>t.length?((Date.now()-Math.max(...t))/3600000).toFixed(1):"";
try{
  const q=JSON.parse(fs.readFileSync(S+"/upload-kuyruk.json","utf8"));
  console.log(age((q.isler||[]).filter(x=>x.durum==="yayinda"&&x.yayinlandi&&!String(x.is||"").endsWith("-short"))
    .map(x=>Date.parse(x.yayinlandi)).filter(Boolean)));
}catch(e){
  try{const d=JSON.parse(fs.readFileSync(S+"/video-performans.json","utf8"));
    console.log(age(Object.values(d.videos||{}).map(v=>Date.parse(v.yayinTarihi||v.publishedAt||0)).filter(Boolean)));
  }catch(e2){console.log("");}
}'

collect() {
  # Stamp before probing so a slow, older tick cannot appear newer than a later one.
  local collected_at
  collected_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  # Container states and health in one pass.
  local states health running inspected
  local containers_complete=true
  local container_health_complete=true
  states=$(docker ps -a --format '{{.Names}}\t{{.State}}' 2>/dev/null) || { states=""; containers_complete=false; }
  running=$(docker ps -q 2>/dev/null) || { running=""; container_health_complete=false; }
  health=""
  if [ "$container_health_complete" = true ]; then
    for c in $running; do
      inspected=$(docker inspect "$c" --format '{{.Name}}	{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null) || {
        container_health_complete=false
        continue
      }
      health+="${inspected#/}"$'\n'
    done
  fi

  # CPU busy % over one second, from two /proc/stat samples (idle = idle + iowait).
  local cpu u1 n1 s1 i1 w1 q1 sq1 st1 u2 n2 s2 i2 w2 q2 sq2 st2 total idle
  read -r _ u1 n1 s1 i1 w1 q1 sq1 st1 _ < /proc/stat
  sleep 1
  read -r _ u2 n2 s2 i2 w2 q2 sq2 st2 _ < /proc/stat
  total=$(( (u2+n2+s2+i2+w2+q2+sq2+st2) - (u1+n1+s1+i1+w1+q1+sq1+st1) ))
  idle=$(( (i2+w2) - (i1+w1) ))
  cpu=$(( total > 0 ? 100 * (total - idle) / total : 0 ))

  local disk mem_total mem_avail
  disk=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
  mem_total=$(awk '/MemTotal/{print $2}' /proc/meminfo)
  mem_avail=$(awk '/MemAvailable/{print $2}' /proc/meminfo)

  # Endpoints worth checking from outside the Docker network.
  local seerxo labubu
  seerxo=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 https://api.seerxo.com/health)
  labubu=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 https://labubucheck.co.uk/)

  # Last long-form publish age. The upload queue records the publish instant; the
  # performance file only gains a video after its 24h checkpoint, which made the
  # watchdog report "no publish" for up to a day after a real publish (2026-10-02).
  # Shorts are cut from the long video, so they must not mask a stalled long pipeline.
  local yt_age
  yt_age=$(docker exec youtube-auto node -e "$YT_AGE_JS" </dev/null 2>/dev/null | tr -dc '0-9.')

  # Only provenance, never `docker inspect .Config.Env` (it contains production secrets).
  local api_sha worker_sha stage_c_enabled stage_c_report
  api_sha=$(docker exec "$API_CONTAINER" python -c 'import os; print(os.getenv("COMPANY_OS_GIT_SHA", ""))' </dev/null 2>/dev/null)
  worker_sha=$(docker exec company-worker python -c 'import os; print(os.getenv("COMPANY_OS_GIT_SHA", ""))' </dev/null 2>/dev/null)
  stage_c_enabled=$(docker exec company-worker printenv ENABLE_AUTONOMOUS_CODE_WRITE 2>/dev/null || true)
  stage_c_report=""
  # The same zero-model preflight as enablement, every tick, flag on or off: app.watchdog stores it and
  # worker admission refuses code writes without a fresh passing report. Alerts stay flag-gated.
  stage_c_report=$(python3 /opt/company-os/scripts/stage-c-host-facts.py \
    | docker exec -i company-worker python -m app.stage_c_preflight --json 2>/dev/null || true)

  STATES="$states" HEALTH="$health" CONTAINERS_COMPLETE="$containers_complete" \
  HEALTH_COMPLETE="$container_health_complete" COLLECTED_AT="$collected_at" \
  CPU="$cpu" DISK="$disk" MT="$mem_total" MA="$mem_avail" \
  SEERXO="$seerxo" LABUBU="$labubu" YT="$yt_age" API_SHA="$api_sha" WORKER_SHA="$worker_sha" \
  STAGE_C_ENABLED="$stage_c_enabled" STAGE_C_REPORT="$stage_c_report" python3 -c '
import json, os, re
def sha(name):
    v = os.environ.get(name, "")
    return v if re.fullmatch(r"[a-f0-9]{40}", v) else None
states = {}
for line in os.environ["STATES"].splitlines():
    if "\t" in line:
        name, state = line.split("\t", 1)
        states[name.strip()] = state.strip()
health = {}
for line in os.environ["HEALTH"].splitlines():
    if "\t" in line:
        name, h = line.split("\t", 1)
        health[name.strip()] = h.strip()
mt, ma = int(os.environ["MT"] or 0), int(os.environ["MA"] or 0)
yt = os.environ["YT"]
try:
    stage_c_report = json.loads(os.environ["STAGE_C_REPORT"]) if os.environ["STAGE_C_REPORT"] else None
except ValueError:
    stage_c_report = None
print(json.dumps({
    "collected_at": os.environ["COLLECTED_AT"],
    "containers": states,
    "containers_complete": os.environ.get("CONTAINERS_COMPLETE") == "true",
    "container_health_complete": os.environ.get("HEALTH_COMPLETE") == "true",
    "container_health": health,
    "cpu_used_pct": int(os.environ["CPU"]) if os.environ.get("CPU") else None,
    "disk_used_pct": int(os.environ["DISK"]) if os.environ["DISK"].isdigit() else None,
    "mem_used_pct": round((mt - ma) / mt * 100) if mt else None,
    "http": {
        "seerxo": {"code": int(os.environ["SEERXO"] or 0),
                   "url": "https://api.seerxo.com/health", "service": "seerxo-api"},
        "labubu": {"code": int(os.environ["LABUBU"] or 0),
                   "url": "https://labubucheck.co.uk/", "service": None},
    },
    "youtube_last_publish_age_h": float(yt) if yt else None,
    "runtime": {"api_sha": sha("API_SHA"), "worker_sha": sha("WORKER_SHA")},
    "stage_c": {"enabled": os.environ["STAGE_C_ENABLED"] == "true", "report": stage_c_report},
}))'
}

if [ "${1:-}" = "--collect-only" ]; then
  collect
  exit 0
fi

FACTS=$(collect)
# Not `if ! cmd; then rc=$?`: there $? is the status of `!` (always 0), which paged the founder
# on every run with findings and let a broken watchdog exit 0.
rc=0
printf '%s' "$FACTS" | docker exec -i "$API_CONTAINER" python -m app.watchdog || rc=$?
# Exit 1 from app.watchdog means "findings were recorded", which is normal. Anything
# else means the watchdog itself could not run — and the API cannot report that the
# API is broken, so this goes straight to Telegram.
if [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then
  command -v company-notify >/dev/null 2>&1 && \
    company-notify "Watchdog could not run" "app.watchdog exited $rc on $(hostname). Company OS may be down." || true
  exit "$rc"
fi
