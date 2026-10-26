// What the Approvals page may claim. Three sources are kept apart because the API can disagree with itself:
// approval records (d.approvals), tasks whose own status is WAITING_APPROVAL, and the needs-you queue. Live data
// (2026-10-16) had 0 records while a task waited for approval, so "no approval waiting" must never come from the
// records alone. A list the API did not report reads as unknown, never as 0.
import type { DeskState, Tone } from "./api"

export interface ApprovalFigure { label: string; value: string; sub: string; tone?: Tone }
export interface ApprovalSummary {
  records: number
  waitingTasks: number
  /** needs_you rows not already shown as an approval card; null = the API did not report the queue. */
  others: number | null
  critical: number | null
  /** Nothing to decide anywhere, and every source was reported. */
  clear: boolean
  figures: ApprovalFigure[]
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`
const HIGH = ["HIGH", "CRITICAL"]

export function approvalSummary(d: Pick<DeskState, "approvals" | "tasks" | "needs_you">): ApprovalSummary {
  const records = d.approvals.length
  const waitingTasks = d.tasks.filter(t => t.status === "WAITING_APPROVAL").length
  const needs = Array.isArray(d.needs_you) ? d.needs_you.filter(n => !(n.kind === "APPROVAL" && n.target.page === "approvals")) : null
  const critical = needs ? needs.filter(n => n.level === "critical").length : null
  const others = needs ? needs.length : null
  const high = d.approvals.some(a => HIGH.includes(a.risk))
  const unknown = "not reported by this API"
  return {
    records, waitingTasks, others, critical,
    clear: records === 0 && waitingTasks === 0 && others === 0,
    figures: [
      { label: "Approval records", value: String(records), tone: records && high ? "warning" : undefined,
        sub: records ? "nothing runs until you decide" : waitingTasks ? "none recorded; see tasks waiting" : "none recorded" },
      { label: "Tasks awaiting approval", value: String(waitingTasks), tone: waitingTasks ? "warning" : undefined,
        sub: waitingTasks ? `${plural(waitingTasks, "task")} stopped for your decision` : "no task is waiting" },
      { label: "Critical", value: critical === null ? "—" : String(critical), tone: critical ? "danger" : undefined,
        sub: critical === null ? unknown : critical ? "decide these first" : "none critical" },
      { label: "Other", value: others === null || critical === null ? "—" : String(others - critical),
        sub: others === null ? unknown : "also needs you" },
    ],
  }
}
