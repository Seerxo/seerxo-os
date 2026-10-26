-- Read-only Company OS notification audit. Run with psql -X -v ON_ERROR_STOP=1 -f.
-- `notified` means the adapter reported HTTP 200; it is not a Telegram receipt.
-- Historical suppressed decisions were not stored, so those counts are estimates.
BEGIN READ ONLY;

SELECT now() AS audit_at, count(*) AS all_events,
       count(*) FILTER (WHERE notified) AS reported_telegram_http_success,
       count(*) FILTER (WHERE notify_error IS NOT NULL) AS recorded_delivery_failures
FROM events WHERE occurred_at >= now() - interval '7 days';

WITH periods(label, age) AS (VALUES ('24h', interval '24 hours'), ('7d', interval '7 days'))
SELECT p.label, e.event_type, e.severity, e.source,
       coalesce(e.incident_id::text, '') AS incident_id,
       coalesce(e.service_id::text, '') AS service_id,
       count(*) AS events,
       count(*) FILTER (WHERE e.notified) AS reported_telegram_http_success,
       count(*) FILTER (WHERE e.notify_error IS NOT NULL) AS recorded_delivery_failures
FROM periods p JOIN events e ON e.occurred_at >= now() - p.age
GROUP BY 1, 2, 3, 4, 5, 6 ORDER BY p.label, events DESC;

-- A repeated successful send under the same historical dedupe key indicates
-- another interruption without a change of key. The key is not necessarily a
-- true incident identity: it sometimes includes a stale-task count.
WITH sent AS (
    SELECT event_type, source, dedupe_key, occurred_at,
           row_number() OVER (PARTITION BY dedupe_key ORDER BY occurred_at, id) AS send_number
    FROM events
    WHERE notified AND dedupe_key IS NOT NULL
      AND occurred_at >= now() - interval '7 days'
)
SELECT event_type, source, count(*) AS repeated_sends_without_new_key,
       count(DISTINCT dedupe_key) AS affected_keys
FROM sent WHERE send_number > 1
GROUP BY 1, 2 ORDER BY repeated_sends_without_new_key DESC;

-- Historic unnotified events cannot be separated into routine, deduplicated,
-- unconfigured and failed cases unless notify_error was recorded. This query
-- reports the observable candidate pool without claiming a precise suppression count.
SELECT event_type, source, count(*) AS unnotified_with_key_no_error
FROM events
WHERE occurred_at >= now() - interval '7 days'
  AND dedupe_key IS NOT NULL AND NOT notified AND notify_error IS NULL
GROUP BY 1, 2 ORDER BY unnotified_with_key_no_error DESC;

COMMIT;
