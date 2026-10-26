import assert from "node:assert/strict"
import { test } from "node:test"
import type { TaskDetail } from "./api.ts"
import { taskPipeline } from "./pipeline.ts"

const task = (over: Partial<TaskDetail> = {}): TaskDetail => ({
  key: "TASK-1", title: "t", description: null, objective: null, status: "READY", priority: "P2", risk: "LOW",
  assigned_agent: "research", project: null, business_unit: null, success_criteria: "s", context: null,
  approval_required: false, dependencies: [], runs: [], artifacts: null, result: null, error: null, supervisor: null,
  created_at: "2026-10-20T10:00:00Z", started_at: null, completed_at: null, ...over }) as TaskDetail
const run = (over = {}) => ({ id: 1, task: "TASK-1", status: "STARTED", provider: "claude", started_at: "2026-10-20T10:01:00Z",
  ended_at: null, error: null, supervisor_verdict: null, supervisor_notes: null, ...over })
const states = (t: TaskDetail) => taskPipeline(t).map(s => s.state)

test("a ready task waits in the queue; nothing after it is shown active", () => {
  assert.deepEqual(states(task()), ["done", "waiting", "pending", "pending", "pending", "pending"])
})

test("a running agent is the current step; the check and review stay pending", () => {
  assert.deepEqual(states(task({ status: "IN_PROGRESS", started_at: "2026-10-20T10:01:00Z", runs: [run()] })),
    ["done", "done", "current", "pending", "pending", "pending"])
})

test("finished and approved by the supervisor, waiting for the founder; done only when closed as done", () => {
  const r = run({ status: "SUCCEEDED", ended_at: "2026-10-20T10:05:00Z", supervisor_verdict: "APPROVED" })
  assert.deepEqual(states(task({ status: "REVIEW", runs: [r] })), ["done", "done", "done", "done", "waiting", "pending"])
  assert.deepEqual(states(task({ status: "DONE", runs: [r], completed_at: "2026-10-20T11:00:00Z" })), ["done", "done", "done", "done", "done", "done"])
})

test("a failed run and a rejected verdict are failures with their reason, never done", () => {
  const steps = taskPipeline(task({ status: "READY", runs: [run({ status: "FAILED", ended_at: "2026-10-20T10:02:00Z", error: "timeout",
    supervisor_verdict: "REVISION_REQUIRED", supervisor_notes: "no evidence" })] }))
  assert.equal(steps[2].state, "failed"); assert.match(steps[2].detail!, /timeout/)
  assert.equal(steps[3].state, "failed"); assert.match(steps[3].detail!, /no evidence/)
})

test("a blocked task stops at its first unfinished step with the error; later steps stay pending", () => {
  const steps = taskPipeline(task({ status: "BLOCKED", error: "DNS change needed" }))
  assert.equal(steps[1].state, "blocked"); assert.match(steps[1].detail!, /DNS change needed/)
  assert.deepEqual(steps.slice(2).map(s => s.state), ["pending", "pending", "pending", "pending"])
})

test("a code mission follows its recorded Stage C history; an unrecorded past stage is not claimed done", () => {
  const history = [{ stage: "DETECTED", at: "2026-10-20T10:00:00Z" }, { stage: "PLANNED", at: "2026-10-20T10:01:00Z" },
    { stage: "PR_OPEN", at: "2026-10-20T10:30:00Z", evidence: { pr: "https://github.com/Seerxo/os/pull/371", commit: "abcdef1234567" } }]
  const steps = taskPipeline(task({ status: "REVIEW", context: { improvement: { stage: "PR_OPEN", history } } }))
  const by = Object.fromEntries(steps.map(s => [s.key, s]))
  assert.equal(by.DETECTED.state, "done"); assert.equal(by.IMPLEMENTING.state, "pending")   // no record
  assert.equal(by.PR_OPEN.state, "current"); assert.match(by.PR_OPEN.detail!, /PR #371 · abcdef1/)
  assert.equal(by.PR_OPEN.href, "https://github.com/Seerxo/os/pull/371")
  assert.equal(by.DETECTED.href, undefined)                                     // no recorded URL, no link
  assert.equal(by.CI.state, "pending"); assert.equal(by.MERGED.state, "pending")
})

test("closed as done without a supervisor verdict never draws a passed check", () => {
  const steps = taskPipeline(task({ status: "DONE", started_at: "2026-10-20T10:01:00Z", completed_at: "2026-10-20T11:00:00Z" }))
  assert.equal(steps.find(s => s.key === "check")!.state, "pending")
  assert.equal(steps.find(s => s.key === "done")!.state, "done")
})

test("a PR link is only ever a recorded GitHub pull URL", () => {
  const history = [{ stage: "DETECTED", at: "x" }, { stage: "PLANNED", at: "x" }, { stage: "IMPLEMENTING", at: "x" }, { stage: "TESTING", at: "x" },
    { stage: "INDEPENDENT_REVIEW", at: "x" }, { stage: "PR_OPEN", at: "x", evidence: { pr: "javascript:alert(1)" } }]
  const step = taskPipeline(task({ status: "REVIEW", context: { improvement: { stage: "PR_OPEN", history } } })).find(s => s.key === "PR_OPEN")!
  assert.equal(step.href, undefined)
})
