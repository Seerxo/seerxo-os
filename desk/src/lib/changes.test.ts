import assert from "node:assert/strict"
import { test } from "node:test"
import { FRESH_MS, prune, statusChanges } from "./changes.ts"

const k = (t: { key: string }) => t.key, s = (t: { status: string }) => t.status

test("the first read marks nothing; later reads mark status changes and new rows only", () => {
  assert.deepEqual(statusChanges("task", undefined, [{ key: "T1", status: "READY" }], k, s, 1), {})
  const prev = [{ key: "T1", status: "READY" }, { key: "T2", status: "DONE" }]
  const next = [{ key: "T1", status: "IN_PROGRESS" }, { key: "T2", status: "DONE" }, { key: "T3", status: "READY" }]
  assert.deepEqual(statusChanges("task", prev, next, k, s, 5), {
    "task:T1": { from: "READY", to: "IN_PROGRESS", at: 5 },
    "task:T3": { from: null, to: "READY", at: 5 },
  })
})

test("a mark expires after FRESH_MS", () => {
  const c = { "task:T1": { from: "READY", to: "DONE", at: 0 } }
  assert.equal(Object.keys(prune(c, FRESH_MS - 1)).length, 1)
  assert.equal(Object.keys(prune(c, FRESH_MS)).length, 0)
})
