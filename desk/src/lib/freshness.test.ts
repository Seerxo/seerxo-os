// Run with `npm test` (node --test, no extra dependency). These pin what the Desk may claim.
import assert from "node:assert/strict"
import { test } from "node:test"
import type { Metric } from "./api"
import { dataState, dataSummary, freshLabel, freshOf, isStale, isUnverified, staleSuffix } from "./freshness.ts"

const m = (freshness: "VERIFIED" | "STALE", metric = "x"): Metric => ({
  subject: "s", metric, window: "", value: 1, unit: null, source: "src",
  observed_at: "2026-10-07T00:00:00+00:00", freshness, label: null,
})

test("a stale series does not make its fresh neighbours stale", () => {
  const unit = { metrics: [m("VERIFIED", "activation_rate"), m("STALE", "mrr")] }
  assert.equal(isStale(unit.metrics[0]), false)
  assert.equal(isStale(unit.metrics[1]), true)
  assert.equal(staleSuffix(unit.metrics[0]), "")
  assert.equal(staleSuffix(unit.metrics[1]), " · stale")
})

test("a missing series is absent, not stale and not zero", () => {
  assert.equal(isStale(undefined), false)
  assert.equal(isStale(null), false)
  assert.equal(staleSuffix(undefined), "")
})

test("a unit with no metrics says so", () => {
  assert.deepEqual(dataState({ metrics: [] }), { state: "NONE", total: 0, stale: 0, unknown: 0 })
  assert.equal(dataSummary({ metrics: [] }), "No metrics reported")
})

test("unit data is described by counts, never by one flag", () => {
  assert.equal(dataSummary({ metrics: [m("VERIFIED"), m("VERIFIED")] }), "2 series current")
  assert.equal(dataSummary({ metrics: [m("VERIFIED"), m("VERIFIED"), m("STALE")] }), "1 of 3 series stale")
  assert.equal(dataSummary({ metrics: [m("STALE"), m("STALE")] }), "All 2 series stale")
  assert.equal(dataState({ metrics: [m("VERIFIED"), m("STALE")] }).state, "PARTIAL")
  assert.equal(dataState({ metrics: [m("STALE")] }).state, "STALE")
})

const odd = (freshness: unknown) => ({ ...m("VERIFIED"), freshness } as unknown as Metric)

test("an unknown or invalid freshness is UNKNOWN, never silently current", () => {
  for (const bad of ["WEIRD", "", null, undefined, "verified", 3]) {
    assert.equal(freshOf(odd(bad)), "UNKNOWN", String(bad))
    assert.equal(isUnverified(odd(bad)), true)
    assert.equal(isStale(odd(bad)), false)
    assert.equal(staleSuffix(odd(bad)), " · unverified")
    assert.equal(freshLabel(odd(bad)), "unverified")
  }
  assert.equal(isUnverified(m("VERIFIED")), false)
  assert.equal(isUnverified(undefined), false)
})

test("mixed, partly-missing and unknown data are described by counts", () => {
  assert.equal(dataSummary({ metrics: [m("VERIFIED"), odd("WEIRD")] }), "1 unknown of 2 series")
  assert.equal(dataSummary({ metrics: [m("VERIFIED"), m("STALE"), odd(null)] }), "1 stale, 1 unknown of 3 series")
  assert.equal(dataSummary({ metrics: [odd("x"), odd("y")] }), "Freshness unknown for all 2 series")
  assert.equal(dataState({ metrics: [odd("x")] }).state, "UNKNOWN")
  assert.equal(dataState({ metrics: [m("VERIFIED"), odd("x")] }).state, "PARTIAL")
})
