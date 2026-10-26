import assert from "node:assert/strict"
import { test } from "node:test"
import { watchdogView } from "./watchdog.ts"

test("an API without watchdog_conditions reads 'not reported', never 0", () => {
  const v = watchdogView({})
  assert.equal(v.reported, false)
  assert.equal(v.count, "Not reported")
  assert.doesNotMatch(v.empty, /No active watchdog condition is recorded/)
})

test("a reported empty list is a recorded zero; truncation names both counts", () => {
  assert.deepEqual([watchdogView({ watchdog_conditions: [] }).count, watchdogView({ watchdog_conditions: [] }).reported], ["0", true])
  const c = { event_id: 1, type: "T", severity: "HIGH", title: "x", detail: null, observed_at: null, episode: 1, delivery_status: "SENT" }
  assert.equal(watchdogView({ watchdog_conditions: [c], watchdog_conditions_total: 4, watchdog_conditions_truncated: true }).count, "1 of 4 · truncated")
})
