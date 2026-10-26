// The task detail shows the deliverable only from well-formed `artifacts.actions`. Run with `npm test`.
import assert from "node:assert/strict"
import { test } from "node:test"
import { deliverableItems } from "./deliverable.ts"

test("only non-empty strings of artifacts.actions are shown", () => {
  assert.deepEqual(deliverableItems({ actions: ["H1: x", "  ", "", "H2: y"], evidence: ["e"] }), ["H1: x", "H2: y"])
  assert.deepEqual(deliverableItems({ actions: [1, null, { a: 1 }, "ok"] }), ["ok"])
})

test("anything that is not an object with a list of actions shows nothing", () => {
  for (const bad of [null, undefined, "x", 3, [], [{ actions: ["a"] }], {}, { actions: "a" }, { actions: null }])
    assert.deepEqual(deliverableItems(bad), [], JSON.stringify(bad))
})

test("a runaway list is capped", () => {
  assert.equal(deliverableItems({ actions: Array.from({ length: 80 }, (_, i) => `item ${i}`) }).length, 50)
})
