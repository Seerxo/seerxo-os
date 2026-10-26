// What a task means for the founder, read only from recorded fields. Every sentence here is derived from the task's
// status, owner, timestamps, error, supervisor verdict and dependencies; nothing is invented and nothing is promoted
// (REVIEW is never "done", queued is never "running", a missing reason says it is missing).
import type { TaskDetail, TaskRow, Tone } from "./api"

/** Founder-facing groups, in the order the Work page shows them. */
export type Group = "attention" | "running" | "review" | "next" | "waiting" | "backlog" | "closed"

export const GROUPS: { key: Group; label: string; tone: Tone; statuses: string[] }[] = [
  { key: "attention", label: "Needs attention", tone: "danger", statuses: ["WAITING_APPROVAL", "BLOCKED", "FAILED"] },
  { key: "running", label: "Running", tone: "info", statuses: ["IN_PROGRESS"] },
  { key: "review", label: "In review", tone: "warning", statuses: ["REVIEW"] },
  { key: "next", label: "Up next", tone: "neutral", statuses: ["READY", "QUEUED"] },
  { key: "waiting", label: "Waiting outside", tone: "warning", statuses: ["WAITING_EXTERNAL"] },
  { key: "backlog", label: "Backlog", tone: "neutral", statuses: ["IDEA", "BACKLOG"] },
  { key: "closed", label: "Closed", tone: "neutral", statuses: ["DONE", "CANCELLED"] },
]

/** The group of a status; an unrecognised status is attention, never silently filed as healthy. */
export function groupOf(status: string): Group {
  return GROUPS.find(g => g.statuses.includes(status))?.key ?? "attention"
}

export const isActive = (status: string) => !["closed", "backlog"].includes(groupOf(status))

/** One line for a list row: what happens next. `reason` is a recorded explanation (task error or a needs-you
 *  `why`) when the caller has one; without it a stopped task says where the reason is, not what it might be. */
export function nextLine(t: Pick<TaskRow, "status" | "agent">, opts: { reason?: string | null; paused?: boolean } = {}): string {
  const who = t.agent ?? null
  switch (t.status) {
    case "WAITING_APPROVAL": return "Waiting for your approval"
    case "BLOCKED": return opts.reason ? `Blocked: ${opts.reason}` : "Blocked. The reason is recorded on the task"
    case "FAILED": return opts.reason ? `Failed: ${opts.reason}` : "Failed. The error is recorded on the task"
    case "IN_PROGRESS": return `${who ? cap(who) : "An agent"} is working on it`
    case "REVIEW": return "Agent finished. Not done until the result is reviewed"
    case "READY": case "QUEUED":
      if (opts.paused) return "Queued, but agent execution is paused"
      return who ? `Queued for ${who}; not started yet` : "Queued with no agent assigned"
    case "WAITING_EXTERNAL": return "Waiting for evidence from outside the company"
    case "IDEA": case "BACKLOG": return "Not scheduled"
    case "DONE": return "Closed as done"
    case "CANCELLED": return "Cancelled"
    default: return `Unrecognised status ${t.status}`
  }
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

export interface Story {
  /** One sentence: where the task stands. */
  now: string
  tone: Tone
  /** The recorded reason it is stopped, or null when it is not stopped. `"unrecorded"` when stopped without a reason. */
  blocker: string | null
  /** Unfinished dependencies (they hold the task back regardless of its own status). */
  waitingOn: { key: string; status: string }[]
  next: string
  /** What the founder can do from the Desk for this task. Only operations the Desk is authorized to call. */
  founder: ("approve" | "assign" | "follow-up")[]
}

/** The founder-facing explanation of a task, from its detail record. */
export function story(t: TaskDetail, opts: { paused?: boolean } = {}): Story {
  const waitingOn = t.dependencies.filter(d => d.status !== "DONE")
  const lastRun = t.runs[0]
  const recorded = t.error?.trim() || (t.supervisor && t.supervisor.verdict !== "APPROVED" ? t.supervisor.notes?.trim() : null)
    || lastRun?.error?.trim() || null
  const stopped = ["BLOCKED", "FAILED"].includes(t.status)
  const blocker = stopped ? (recorded ?? "unrecorded")
    : t.status === "WAITING_APPROVAL" || (t.approval_required && ["READY", "QUEUED"].includes(t.status))
      ? "Founder approval is required before it can continue"
    : t.status === "WAITING_EXTERNAL" ? "Waiting for evidence from outside the company"
    : waitingOn.length && isActive(t.status) && t.status !== "IN_PROGRESS" ? `Waiting for ${waitingOn.map(d => d.key).join(", ")}`
    : null
  const tone: Tone = stopped ? "danger" : ["WAITING_APPROVAL", "WAITING_EXTERNAL", "REVIEW"].includes(t.status) ? "warning"
    : t.status === "IN_PROGRESS" ? "info" : "neutral"
  const now = t.status === "BLOCKED" ? "Stopped. It will not continue on its own"
    : t.status === "FAILED" ? "Failed. It will not retry on its own"
    : nextLine({ status: t.status, agent: t.assigned_agent }, { paused: opts.paused })
  const next = ({
    WAITING_APPROVAL: "It continues after the approval is granted in Approvals.",
    BLOCKED: "It stays blocked until the cause is fixed, then it can be re-queued.",
    FAILED: "It will not retry on its own. Reassign it or create a follow-up task.",
    IN_PROGRESS: "The agent records a result; the supervisor then reviews it.",
    REVIEW: "A reviewer accepts or rejects the result. Only then can it be closed as done.",
    READY: opts.paused ? "It starts once agent execution is resumed." : "The worker picks it up on a coming tick.",
    QUEUED: opts.paused ? "It starts once agent execution is resumed." : "The worker picks it up on a coming tick.",
    WAITING_EXTERNAL: "It returns to the queue when the outside evidence arrives.",
    DONE: "Nothing further. The result and evidence are below.",
    CANCELLED: "Nothing further.",
  } as Record<string, string>)[t.status] ?? "Not scheduled."
  const founder: Story["founder"] = []
  if (t.approval_required && isActive(t.status)) founder.push("approve")
  if (isActive(t.status)) founder.push("assign")
  if (["FAILED", "BLOCKED", "CANCELLED", "DONE"].includes(t.status)) founder.push("follow-up")
  return { now, tone, blocker, waitingOn, next, founder }
}

/** What the Needs attention count is made of, from task statuses only: "5 failed · 5 blocked · 1 awaiting approval". */
export function attentionBreakdown(tasks: Pick<TaskRow, "status">[]): string | null {
  const n = (s: string) => tasks.filter(t => t.status === s).length
  const parts = ([["FAILED", "failed"], ["BLOCKED", "blocked"], ["WAITING_APPROVAL", "awaiting approval"]] as const)
    .filter(([s]) => n(s)).map(([s, label]) => `${n(s)} ${label}`)
  return parts.length ? parts.join(" · ") : null
}

/** The oldest last-update among tasks in one status. updated_at is the last change to the task, not proof of when it
 *  entered the status, so callers say "untouched", never "waiting since". */
export function oldestUpdate(tasks: Pick<TaskRow, "status" | "updated_at">[], status: string): string | null {
  return tasks.filter(t => t.status === status).map(t => t.updated_at).sort()[0] ?? null
}
