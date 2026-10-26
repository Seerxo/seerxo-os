// Task meaning for the founder: groups, next steps and the lifecycle are read from recorded fields only.
import assert from "node:assert/strict"
import { test } from "node:test"
import type { TaskDetail } from "./api.ts"
import { GROUPS, groupOf, nextLine, story } from "./task-story.ts"

const ALL = ["IDEA", "BACKLOG", "READY", "QUEUED", "IN_PROGRESS", "WAITING_EXTERNAL", "WAITING_APPROVAL", "BLOCKED", "REVIEW", "FAILED", "DONE", "CANCELLED"]

const detail = (over: Partial<TaskDetail> = {}): TaskDetail => ({
  key: "TASK-0001", title: "t", description: null, objective: null, status: "READY", priority: "P2", risk: "LOW",
  assigned_agent: "research", project: null, business_unit: null, success_criteria: "x", context: null,
  approval_required: false, dependencies: [], runs: [], artifacts: null, result: null, error: null, supervisor: null,
  created_at: "2026-10-15T08:00:00Z", started_at: null, completed_at: null, ...over,
})

test("every backend task status belongs to exactly one founder group", () => {
  for (const s of ALL) assert.equal(GROUPS.filter(g => g.statuses.includes(s)).length, 1, s)
  assert.equal(groupOf("SOMETHING_NEW"), "attention")   // unknown is surfaced, never filed as healthy
})

test("list lines never promote a state", () => {
  assert.match(nextLine({ status: "REVIEW", agent: "research" }), /Not done until/)
  assert.match(nextLine({ status: "READY", agent: "ceo" }), /not started/)
  assert.match(nextLine({ status: "READY", agent: null }), /no agent/)
  assert.match(nextLine({ status: "QUEUED", agent: "ceo" }, { paused: true }), /paused/)
  assert.equal(nextLine({ status: "BLOCKED", agent: "dev" }, { reason: "SPF fail" }), "Blocked: SPF fail")
  assert.match(nextLine({ status: "BLOCKED", agent: "dev" }), /reason is recorded on the task/)
})

test("the story explains a stopped task with its recorded reason, or says none was recorded", () => {
  assert.equal(story(detail({ status: "BLOCKED", error: "SPF fail" })).blocker, "SPF fail")
  assert.equal(story(detail({ status: "FAILED" })).blocker, "unrecorded")
  assert.match(story(detail({ status: "WAITING_APPROVAL" })).blocker!, /approval/)
  assert.match(story(detail({ status: "READY", approval_required: true })).blocker!, /approval/)
  assert.equal(story(detail({ status: "IN_PROGRESS" })).blocker, null)
  const deps = story(detail({ dependencies: [{ key: "TASK-0002", status: "IN_PROGRESS" }, { key: "TASK-0003", status: "DONE" }] }))
  assert.deepEqual(deps.waitingOn.map(d => d.key), ["TASK-0002"])
  assert.match(deps.blocker!, /TASK-0002/)
})

test("founder actions are limited to what the Desk is authorized to call", () => {
  assert.deepEqual(story(detail({ status: "WAITING_APPROVAL", approval_required: true })).founder, ["approve", "assign"])
  assert.deepEqual(story(detail({ status: "FAILED" })).founder, ["assign", "follow-up"])
  assert.deepEqual(story(detail({ status: "DONE" })).founder, ["follow-up"])
})

test("the attention breakdown and the oldest untouched review come from statuses and timestamps only", async () => {
  const { attentionBreakdown, oldestUpdate } = await import("./task-story.ts")
  const t = (status: string, updated_at = "2026-10-15T00:00:00Z") => ({ status, updated_at })
  assert.equal(attentionBreakdown([t("FAILED"), t("FAILED"), t("BLOCKED"), t("WAITING_APPROVAL"), t("REVIEW")]), "2 failed · 1 blocked · 1 awaiting approval")
  assert.equal(attentionBreakdown([t("REVIEW")]), null)
  assert.equal(oldestUpdate([t("REVIEW", "2026-10-10T00:00:00Z"), t("REVIEW", "2026-10-01T00:00:00Z"), t("DONE", "2026-09-01T00:00:00Z")], "REVIEW"), "2026-10-01T00:00:00Z")
  assert.equal(oldestUpdate([t("DONE")], "REVIEW"), null)
})
