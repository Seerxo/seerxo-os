// Where one task is in its execution, from the task detail only (GET /tasks/{key}). A step is "done" only with a
// recorded fact (a timestamp, a run, a verdict); the current step is the task's own status; every later step is
// "pending" however the diagram draws it. Code missions use their recorded Stage C history (stage_c.advance).
import type { Run, TaskDetail } from "./api"
import { DELIVERY_STAGES } from "./delivery.ts"

export type StepState = "done" | "current" | "waiting" | "failed" | "blocked" | "cancelled" | "pending"
export interface Step { key: string; label: string; state: StepState; at?: string | null; until?: string | null; detail?: string
  /** A link the backend recorded (the PR URL from stage_c_runner.publish), never one the Desk builds. */
  href?: string }

const GITHUB = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/(\d+)$/

const STOPPED = ["BLOCKED", "WAITING_EXTERNAL", "WAITING_APPROVAL"]
const latest = (runs: Run[]) => [...runs].sort((a, b) => b.id - a.id)[0]

type History = { stage: string; actor?: string; at?: string; evidence?: Record<string, unknown> }[]

function mission(t: TaskDetail): Step[] | null {
  const imp = (t.context as { improvement?: { stage?: string; history?: History } } | null)?.improvement
  if (!imp?.stage) return null
  const history = imp.history ?? []
  const at = (stage: string) => [...history].reverse().find(h => h.stage === stage)
  const order: string[] = DELIVERY_STAGES.map(([k]) => k)
  const current = order.indexOf(imp.stage)
  const abandoned = imp.stage === "ABANDONED"
  return DELIVERY_STAGES.map(([key, label], i) => {
    const h = at(key)
    const ev = (h?.evidence ?? {}) as Record<string, unknown>
    const url = typeof ev.pr === "string" && GITHUB.test(ev.pr) ? ev.pr : undefined
    const pr = url ? url.match(GITHUB)![1] : typeof ev.pr === "number" ? ev.pr : typeof ev.pr_number === "number" ? ev.pr_number : null
    const detail = [pr !== null && `PR #${pr}`, typeof ev.commit === "string" && ev.commit.slice(0, 7),
      typeof ev.ci_state === "string" && `CI ${ev.ci_state}`, typeof ev.failed_check === "string" && `failed: ${ev.failed_check}`,
      typeof ev.verdict === "string" && `review ${ev.verdict.toLowerCase()}`].filter(Boolean).join(" · ") || undefined
    const state: StepState = abandoned ? (h ? "done" : "cancelled")
      : i < current ? (h ? "done" : "pending")          // no record of a past stage: not claimed done
      : i === current ? (t.status === "BLOCKED" ? "blocked" : t.status === "FAILED" ? "failed" : "current")
      : "pending"
    return { key, label, state, at: h?.at ?? null, detail, href: url }
  })
}

export function taskPipeline(t: TaskDetail): Step[] {
  const m = mission(t)
  if (m) return m
  const run = t.runs.length ? latest(t.runs) : undefined
  const s = t.status
  const closed = s === "DONE" || s === "CANCELLED"
  const created: Step = { key: "created", label: "Created", state: "done", at: t.created_at,
    detail: t.origin ? `${t.origin.automatic ? "automatically by" : "by"} ${t.origin.by}` : undefined }
  const queued: Step = { key: "queued", label: "Queued for an agent", at: null,
    state: ["READY", "QUEUED"].includes(s) ? "waiting" : s === "BACKLOG" || s === "IDEA" ? "pending" : run || t.started_at ? "done" : "pending",
    detail: s === "BACKLOG" ? "In the backlog: not released to the worker" : t.assigned_agent ? `for ${t.assigned_agent}` : "no agent assigned" }
  const attempts = t.runs.length
  const runStep: Step = !run ? { key: "run", label: "Agent run", state: s === "IN_PROGRESS" ? "current" : "pending",
      at: t.started_at, detail: s === "IN_PROGRESS" ? "Claimed; the run has not been recorded yet" : undefined }
    : { key: "run", label: attempts > 1 ? `Agent run · attempt ${attempts}` : "Agent run", at: run.started_at, until: run.ended_at,
        state: run.status === "STARTED" ? "current" : run.status === "SUCCEEDED" ? "done" : run.status === "ESCALATED" ? "blocked" : "failed",
        detail: [run.provider, run.status === "STARTED" ? "running" : run.error].filter(Boolean).join(" · ") || undefined }
  const verdict = run?.supervisor_verdict
  const check: Step = { key: "check", label: "Supervisor check", at: verdict ? run?.ended_at : null,
    state: !verdict ? "pending" : verdict === "APPROVED" ? "done" : verdict === "ESCALATED" ? "blocked" : "failed",
    detail: verdict ? [verdict.toLowerCase().replaceAll("_", " "), run?.supervisor_notes].filter(Boolean).join(" · ") : undefined }
  const review: Step = { key: "review", label: "Founder review", at: null,
    state: s === "REVIEW" ? "waiting" : s === "DONE" ? "done" : s === "CANCELLED" ? "cancelled" : "pending",
    detail: s === "REVIEW" ? "Finished by the agent; not done until you accept it" : undefined }
  const done: Step = { key: "done", label: s === "CANCELLED" ? "Cancelled" : "Done", at: closed ? t.completed_at : null,
    state: s === "DONE" ? "done" : s === "CANCELLED" ? "cancelled" : "pending" }
  const steps = [created, queued, runStep, check, review, done]
  // A stopped task stops at the first step that is not done, with its reason; nothing after it moves.
  if (STOPPED.includes(s) || s === "FAILED") {
    const i = steps.findIndex(x => x.state !== "done")
    if (i >= 0 && steps[i].state !== "failed") steps[i] = { ...steps[i], state: s === "FAILED" ? "failed" : "blocked",
      detail: [s === "WAITING_APPROVAL" ? "Waiting for your approval" : s === "WAITING_EXTERNAL" ? "Waiting on something outside the company" : null, t.error].filter(Boolean).join(" · ") || steps[i].detail }
  }
  return steps
}
