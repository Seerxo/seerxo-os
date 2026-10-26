#!/usr/bin/env bash
# Host-level Telegram notifier — the fallback path that does NOT depend on Company OS.
#
# The API cannot report that the API is down, so watchdog checks for Company OS itself
# use this script directly. Everything else should POST to /events so the alert is
# recorded in the database first.
#
# Usage: notify.sh "<subject>" "<body>"
set -uo pipefail

ENV_FILE=${ENV_FILE:-/opt/company-os/.env}
# Preserve proxy/CA environment settings used by curl; unexport Telegram credentials below.
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
TELEGRAM_ADMIN_CHAT_IDS=${TELEGRAM_ADMIN_CHAT_IDS:-}
export -n TELEGRAM_BOT_TOKEN TELEGRAM_ADMIN_CHAT_IDS

SUBJECT=${1:?usage: notify.sh "<subject>" "<body>"}
BODY=${2:-}

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_ADMIN_CHAT_IDS:-}" ]; then
  echo "notify.sh: Telegram not configured" >&2
  exit 10
fi

TEXT="[$(hostname)] $SUBJECT"
[ -n "$BODY" ] && TEXT="$TEXT
$BODY"

rc=0
IFS=',' read -ra raw_chats <<< "$TELEGRAM_ADMIN_CHAT_IDS"
CHATS=()
for chat in "${raw_chats[@]}"; do
  chat=$(echo "$chat" | tr -d '[:space:]')
  [ -z "$chat" ] || CHATS+=("$chat")
done
if [ ${#CHATS[@]} -eq 0 ]; then
  echo "notify.sh: no valid Telegram recipients configured" >&2
  exit 10
fi

recipient=0
for chat in "${CHATS[@]}"; do
  recipient=$((recipient + 1))
  # curl exits 0 on HTTP 4xx, and Telegram can answer 200 with {"ok":false}. Neither is
  # delivery, so the body and status are both checked — §130, a zero exit is not a result.
  # Keep the bot token out of curl's argv and environment.
  resp=$(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TELEGRAM_BOT_TOKEN" |
        curl --config - -sS -w '\n%{http_code}' -X POST \
        --data-urlencode "chat_id=${chat}" \
        --data-urlencode "text=${TEXT}" \
        --data "disable_web_page_preview=true" \
        --max-time 15 2>/dev/null)
  code=$(printf '%s' "$resp" | tail -1)
  body=$(printf '%s' "$resp" | sed '$d')
  if [ "$code" != "200" ] || ! printf '%s' "$body" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*true'; then
    # Never echo Telegram's response body, bot token or private chat id.
    echo "notify.sh: delivery FAILED for recipient ${recipient}: HTTP ${code}" >&2
    rc=1
  fi
done
exit $rc
