// The Approvals page never says "no approval waiting" while a task waits, and never turns an unreported queue into 0.
import assert from "node:assert/strict"
import { test } from "node:test"
import type { DeskState } from "./api.ts"
import { approvalSummary } from "./approvals.ts"

const task = (status: string, key = status) => ({ key, title: key, status, priority: "P2", risk: "LOW", agent: null, project: null,
  business_unit: null, approval_required: true, created_at: "2026-10-15T10:00:00Z", updated_at: "2026-10-15T11:00:00Z" })
const approval = (key: string, risk = "LOW") => ({ key, action: "x", reason: null, expected_effect: null, risk, rollback: null, requested_by: "growth", created_at: "2026-10-15T10:00:00Z" })
const need = (kind: string, level: string, page = "tasks") => ({ kind, rank: 1, level, title: "t", why: "w", affected: "", since: null, evidence: "", action: "", target: { page } })
type S = Pick<DeskState, "approvals" | "tasks" | "needs_you">
const fig = (s: S, label: string) => approvalSummary(s).figures.find(f => f.label === label)!

test("contradictory live shape: no approval record but a task waits for approval", () => {
  const s: S = { approvals: [], tasks: [task("WAITING_APPROVAL"), task("BLOCKED")], needs_you: [need("APPROVAL", "info")] }
  const sum = approvalSummary(s)
  assert.equal(sum.records, 0)
  assert.equal(sum.waitingTasks, 1)
  assert.equal(sum.clear, false)
  for (const f of sum.figures) assert.doesNotMatch(f.sub, /no approval waiting/)
  assert.equal(fig(s, "Tasks awaiting approval").value, "1")
  assert.equal(fig(s, "Tasks awaiting approval").tone, "warning")
  assert.match(fig(s, "Approval records").sub, /see tasks waiting/)
})

test("counts are derived, not fixed: several waiting tasks and records", () => {
  const s: S = { approvals: [approval("A1", "HIGH"), approval("A2")], tasks: [task("WAITING_APPROVAL", "a"), task("WAITING_APPROVAL", "b"), task("WAITING_APPROVAL", "c")], needs_you: [] }
  assert.equal(fig(s, "Approval records").value, "2")
  assert.equal(fig(s, "Approval records").tone, "warning")
  assert.equal(fig(s, "Tasks awaiting approval").value, "3")
  assert.match(fig(s, "Tasks awaiting approval").sub, /^3 tasks/)
})

test("an unreported needs-you queue reads unknown, never 0, and is never 'clear'", () => {
  const s: S = { approvals: [], tasks: [], needs_you: undefined }
  const sum = approvalSummary(s)
  assert.equal(sum.clear, false)
  assert.equal(fig(s, "Critical").value, "—")
  assert.equal(fig(s, "Other").value, "—")
  assert.match(fig(s, "Critical").sub, /not reported/)
})

test("approval rows already shown as cards are not counted twice; only a fully empty, reported state is clear", () => {
  const dup: S = { approvals: [approval("A1")], tasks: [], needs_you: [need("APPROVAL", "warning", "approvals"), need("SEV1", "critical", "incidents")] }
  assert.equal(fig(dup, "Critical").value, "1")
  assert.equal(fig(dup, "Other").value, "0")
  assert.equal(approvalSummary({ approvals: [], tasks: [task("DONE")], needs_you: [] }).clear, true)
})
