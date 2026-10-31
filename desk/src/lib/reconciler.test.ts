// The Automation page's business-reconciler row: a failing, unfinished or unknown reconciler can never
// look healthy, and nothing is shown that the backend (app/reconciler_heartbeat.py:view) did not prove.
// Run with `npm test`.
import assert from "node:assert/strict"
import { test } from "node:test"
import { reconcilerView } from "./reconciler.ts"

const NOW = Date.parse("2026-10-09T12:00:00Z")
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()
// What view() returns for a fresh, completed invocation that created no task and saw no logged goal error.
const base = {
  last_attempt_at: ago(3), last_completed_at: ago(3), outcome: "COMPLETED", duration_ms: 120, tasks_created: 0,
  goal_errors: null, per_goal_outcome: "unknown", error: null, freshness: "VERIFIED", evidence_source: "watchdog",
  expected_cadence_s: 600,
}
const v = (over: Record<string, unknown> | unknown) =>
  reconcilerView(typeof over === "object" && over !== null && !Array.isArray(over) ? { ...base, ...over } : over, NOW)

test("NO_EVIDENCE is the normal first state: unknown, not corrupt, no timestamps", () => {
  const r = v({ outcome: "NO_EVIDENCE", last_attempt_at: null, last_completed_at: null, freshness: "UNAVAILABLE",
    tasks_created: null, duration_ms: null })
  assert.equal(r.label, "UNKNOWN")
  assert.match(r.detail, /No recorded invocation yet/)
  assert.doesNotMatch(r.detail, /[Mm]alformed/)
  assert.equal(r.last, null)
  assert.notEqual(r.tone, "success")
})

test("a fresh STARTED is an invocation in progress, distinct from UNFINISHED", () => {
  const r = v({ outcome: "STARTED", last_completed_at: ago(13), tasks_created: null })
  assert.equal(r.label, "STARTED")
  assert.match(r.detail, /in progress/)
  assert.doesNotMatch(r.detail, /did not complete/)
  assert.equal(r.tone, "info")
  assert.equal(r.last, base.last_attempt_at)
})

test("UNFINISHED (the backend's STARTED past the cadence) is a warning that it did not complete", () => {
  const r = v({ outcome: "UNFINISHED", last_attempt_at: ago(12), last_completed_at: ago(22), tasks_created: null })
  assert.equal(r.label, "UNFINISHED")
  assert.match(r.detail, /did not complete/)
  assert.equal(r.tone, "warning")
})

test("RAISED is a failure, with the error class, and outranks an older completion", () => {
  const r = v({ outcome: "RAISED", error: "OperationalError", last_completed_at: ago(23) })
  assert.equal(r.label, "FAILED")
  assert.equal(r.tone, "danger")
  assert.match(r.detail, /OperationalError/)
  assert.match(r.detail, /previous completion 23m ago/)          // history, explicitly not this run
  assert.doesNotMatch(r.detail, /^completed/)
})

test("per-goal FAILED on a completed invocation is a failure with the count", () => {
  const r = v({ per_goal_outcome: "FAILED", goal_errors: 2 })
  assert.equal(r.label, "FAILED")
  assert.equal(r.tone, "danger")
  assert.match(r.detail, /2 goal error\(s\)/)
})

test("COMPLETED with per_goal_outcome unknown is invocation completion only, never success", () => {
  const r = v({})
  assert.equal(r.label, "COMPLETED")
  assert.notEqual(r.tone, "success")                               // no green: goals were not proven processed
  assert.match(r.detail, /per-goal outcome unknown/)
  assert.match(r.detail, /0 task\(s\) created/)                    // tasks_created=0 is a normal, healthy fact
  assert.equal(r.last, base.last_attempt_at)
})

test("STALE: a completion older than the freshness window is stale, not completed", () => {
  const r = v({ freshness: "STALE", last_attempt_at: ago(45), last_completed_at: ago(45) })
  assert.equal(r.label, "STALE")
  assert.equal(r.tone, "warning")
  assert.match(r.detail, /stale/)
})

test("a failure stays a failure when its evidence is also stale", () => {
  assert.equal(v({ outcome: "RAISED", freshness: "STALE", error: "X" }).label, "FAILED")
  assert.equal(v({ per_goal_outcome: "FAILED", goal_errors: 1, freshness: "STALE" }).label, "FAILED")
})

test("MALFORMED and unrecognised outcomes are unknown and show no timestamp", () => {
  for (const outcome of ["MALFORMED", "DONE", "", 7, null]) {
    const r = v({ outcome })
    assert.equal(r.label, "UNKNOWN", String(outcome))
    assert.equal(r.last, null)
    assert.notEqual(r.tone, "success")
  }
})

test("a malformed payload or an older API fails closed, without NaN text", () => {
  for (const raw of [undefined, null, 42, "x", [], { outcome: "COMPLETED" }]) {
    const r = reconcilerView(raw, NOW)
    assert.equal(r.label, "UNKNOWN", JSON.stringify(raw))
    assert.doesNotMatch(r.detail + (r.cadence ?? ""), /NaN|undefined/)
  }
  // COMPLETED but the completion time is garbage or in the future: not a completion
  for (const last_completed_at of ["yesterday", null, new Date(NOW + 60_000).toISOString()]) {
    assert.equal(v({ last_completed_at }).label, "UNKNOWN", String(last_completed_at))
  }
  // freshness must be the literal VERIFIED to count as fresh
  assert.equal(v({ freshness: "verified" }).label, "STALE")
  assert.equal(v({ freshness: undefined }).label, "STALE")
  // counts: only non-negative integers are shown
  assert.match(v({ tasks_created: -1 }).detail, /tasks created unknown/)
  assert.match(v({ tasks_created: "3" }).detail, /tasks created unknown/)
  assert.equal(v({ expected_cadence_s: "600" }).cadence, null)
  assert.equal(v({}).cadence, "Every 10 min")
})

test("a historical completion followed by an unfinished attempt never reads as completed", () => {
  const r = v({ outcome: "UNFINISHED", last_attempt_at: ago(15), last_completed_at: ago(25) })
  assert.equal(r.label, "UNFINISHED")
  assert.match(r.detail, /previous completion 25m ago/)
})


test("the Automation row never contradicts the invocation's own task count", async () => {
  const { readFileSync } = await import("node:fs")
  const src = readFileSync(new URL("../pages/automation.tsx", import.meta.url), "utf8")
  assert.doesNotMatch(src, /"no task created"/)                    // read next to "1 task(s) created" it contradicted it
  assert.match(src, /no reconciler task in recent work/)
})


test("evidence is judged by the API's clock, not a browser clock that runs behind (Codex P2 on #196)", () => {
  // The browser is 10 minutes behind the API host: a run the backend verified 1 minute ago is "in the future"
  // for the browser. Judged by the response's generated_at it is valid and fresh.
  const server = NOW, browserBehind = NOW - 10 * 60_000
  const evidence = { ...base, last_attempt_at: ago(1), last_completed_at: ago(1) }
  assert.equal(reconcilerView(evidence, browserBehind).label, "UNKNOWN")       // why the clock matters
  const r = reconcilerView(evidence, server)
  assert.equal(r.label, "COMPLETED")
  assert.match(r.detail, /completed 1m ago/)
})

test("serverNow reads the response's generated_at and falls back only when it is unusable", async () => {
  const { serverNow } = await import("./reconciler.ts")
  assert.equal(serverNow("2026-10-09T12:00:00+00:00", 5), Date.parse("2026-10-09T12:00:00Z"))
  for (const bad of [undefined, null, "", "yesterday", "2026-10-09T12:00:00"]) assert.equal(serverNow(bad, 5), 5)
})

test("STALE measures age from the attempt the backend judged, not from a later completion (Codex P2 on #196)", () => {
  // A run started 25 minutes ago that finished just now: the backend marks it STALE from last_attempt_at.
  const r = v({ freshness: "STALE", last_attempt_at: ago(25), last_completed_at: ago(0) })
  assert.equal(r.label, "STALE")
  assert.match(r.detail, /last attempt 25m ago/)
  assert.doesNotMatch(r.detail, /stale \(0m ago\)/)
})

test("the Automation page judges reconciler evidence by the API's generated_at", async () => {
  const { readFileSync } = await import("node:fs")
  const src = readFileSync(new URL("../pages/automation.tsx", import.meta.url), "utf8")
  assert.match(src, /reconcilerView\(e, serverNow\(generatedAt\)\)/)
  assert.match(src, /reconcilerPresentation\(d\.reconciler_execution, d\.generated_at\)/)
})
