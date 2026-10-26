// What a founder command has become, judged only from the task it created. POST /desk/command answering 201
// means the command was accepted and a task recorded; it says nothing about execution. Each later step is
// claimed only when the task's own status shows it, and nothing here ever claims a verified outcome: the
// task list carries no supervisor verdict, so "verified" stays in the task detail.
import type { Tone } from "./api"

export type Step = "accepted" | "queued" | "running" | "finished" | "done" | "stopped" | "unknown"
export interface Lifecycle { step: Step; label: string; detail: string; tone: Tone }

const QUEUED = ["READY", "QUEUED", "BACKLOG", "IDEA"]
const STOPPED = ["BLOCKED", "FAILED", "CANCELLED", "WAITING_APPROVAL", "WAITING_EXTERNAL"]

/** `status` is the task's current status from the Desk's task list, or undefined when the task is not in
 *  the list the API returned (a recent-window list): then the step is unknown, never assumed. */
export function commandLifecycle(status: string | undefined, accepted: boolean): Lifecycle {
  if (!accepted) return { step: "accepted", label: "Sending", detail: "Waiting for the API to accept the command", tone: "neutral" }
  if (status === undefined)
    return { step: "unknown", label: "Status unknown", detail: "The task is not in the Desk's recent task list; open it to check", tone: "neutral" }
  if (QUEUED.includes(status))
    return { step: "queued", label: "Queued", detail: "Task created; no agent has started it yet", tone: "neutral" }
  if (status === "IN_PROGRESS")
    return { step: "running", label: "Agent executing", detail: "An agent has started this task; not finished", tone: "info" }
  if (status === "REVIEW")
    return { step: "finished", label: "Awaiting review", detail: "The agent finished; the result is not accepted until reviewed", tone: "warning" }
  if (status === "DONE")
    return { step: "done", label: "Done", detail: "Closed as done; check the task for its evidence", tone: "neutral" }
  if (STOPPED.includes(status))
    return { step: "stopped", label: status === "CANCELLED" ? "Cancelled" : "Stopped", detail: `Task is ${status.toLowerCase().replaceAll("_", " ")}`,
      tone: status === "FAILED" || status === "BLOCKED" ? "danger" : "warning" }
  return { step: "unknown", label: "Status unknown", detail: `Unrecognised task status ${status}`, tone: "neutral" }
}

/** The command track the Desk draws: submitted → queued → executing → review → done. "Verified" is deliberately not a
 *  step here: the task list carries no supervisor verdict, so verification is only ever shown from the task detail. */
export const TRACK = ["Submitted", "Queued", "Executing", "Review", "Done"] as const

/** How far along TRACK a lifecycle step is (0-based), or null when the step is not on the track at all (stopped:
 *  where it stopped is not known from the list; unknown: nothing is known). Never rounds a step up. */
export function trackIndex(step: Step): number | null {
  return ({ accepted: 0, queued: 1, running: 2, finished: 3, done: 4 } as Partial<Record<Step, number>>)[step] ?? null
}
