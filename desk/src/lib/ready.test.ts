import assert from "node:assert/strict"
import { test } from "node:test"
import { canOfferReady, readyNote } from "./ready.ts"

const run = (status: string) => ({ id: 1, task: "T", status, provider: "claude", started_at: "x", ended_at: null, error: null, supervisor_verdict: null, supervisor_notes: null })

test("offered only for stopped or not-yet-released work", () => {
  for (const s of ["BACKLOG", "IDEA", "BLOCKED", "FAILED", "WAITING_EXTERNAL"]) assert.equal(canOfferReady({ status: s, runs: [] }), true, s)
  for (const s of ["READY", "IN_PROGRESS", "REVIEW", "DONE", "CANCELLED", "WAITING_APPROVAL"]) assert.equal(canOfferReady({ status: s, runs: [] }), false, s)
})

test("never while an agent run is still open, even if the status says stopped", () => {
  assert.equal(canOfferReady({ status: "BLOCKED", runs: [run("STARTED")] }), false)
  assert.equal(canOfferReady({ status: "BLOCKED", runs: [run("FAILED")] }), true)
})

test("the dialog says when READY will not make the worker run it", () => {
  assert.match(readyNote({ context: { autonomous: true }, retry_count: 3, max_attempts: 3 })!, /All 3 attempts are used/)
  assert.match(readyNote({ context: null })!, /Not opted in/)
  assert.equal(readyNote({ context: { autonomous: true }, retry_count: 1, max_attempts: 3 }), null)
  assert.equal(readyNote({ context: { autonomous: true } }), null)   // older API: no counts, no claim
})
