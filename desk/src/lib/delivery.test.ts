import assert from "node:assert/strict"
import { test } from "node:test"
import type { Improvement } from "./api.ts"
import { DELIVERY_STAGES, deliveryView } from "./delivery.ts"

const imp = (over: Partial<Improvement>): Improvement => ({ task: "TASK-1", title: "t", stage: "IMPLEMENTING", updated_at: null, repository: null, branch: null,
  pr_number: null, pr_state: null, ci_state: null, failed_check: null, repairs: null, review_verdict: null, risk: null, expected_effect: null, verified_at: null, ...over })

test("the stage list mirrors app/stage_c.py in order (ABANDONED is terminal and never reported)", () => {
  assert.deepEqual(DELIVERY_STAGES.map(([k]) => k), ["DETECTED", "PLANNED", "IMPLEMENTING", "TESTING", "INDEPENDENT_REVIEW", "PR_OPEN", "CI", "READY_TO_MERGE", "MERGED", "VERIFYING", "MEASURED"])
})

test("a mission's position and evidence come only from its own record", () => {
  const v = deliveryView(imp({ stage: "MERGED", pr_number: 309, ci_state: "passed" }))
  assert.deepEqual([v.index, v.label, v.tone, v.evidence], [8, "Merged", "success", ["PR #309", "CI passed"]])
  assert.deepEqual(deliveryView(imp({})).evidence, [])
})

test("a failed check is danger; ready to merge waits on the founder; an unknown stage is never placed on the track", () => {
  assert.equal(deliveryView(imp({ stage: "CI", ci_state: "failure", failed_check: "python-tests", repairs: "2" })).tone, "danger")
  assert.deepEqual(deliveryView(imp({ stage: "CI", failed_check: "lint", repairs: "1" })).evidence, ["failed: lint", "1 repair"])
  assert.equal(deliveryView(imp({ stage: "READY_TO_MERGE" })).tone, "warning")
  const u = deliveryView(imp({ stage: "WEIRD" }))
  assert.deepEqual([u.index, u.label, u.tone], [null, "Unknown stage WEIRD", "warning"])
})
