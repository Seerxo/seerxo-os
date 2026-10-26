// An agent's state as the Desk shows it. The API's agent status comes from PostgreSQL; "RUNNING" there is only evidence
// while the worker that runs agents is itself heard from. With the worker silent (no heartbeat within its own stale
// threshold) or unreported, a RUNNING agent is shown as not confirmed, never as executing.
import type { Agent, DeskState, Tone } from "./api"
import { HEARTBEAT_STALE_MS } from "./operating.ts"

export function workerHeard(d: DeskState, now: number): boolean {
  const w = d.worker
  if (!w || w.status === "NO_DATA" || w.status === "STALE" || !w.heartbeat_at) return false
  return now - Date.parse(w.heartbeat_at) <= HEARTBEAT_STALE_MS
}

export interface AgentState { value: string; label?: string; tone?: Tone; running: boolean }

export function agentState(a: Pick<Agent, "status" | "enabled">, d: DeskState, now: number): AgentState {
  if (a.enabled === false) return { value: "DISABLED", label: "Disabled", tone: "neutral", running: false }
  if (a.status === "RUNNING" && !workerHeard(d, now)) return { value: "UNCONFIRMED", label: "Running · not confirmed", tone: "warning", running: false }
  return { value: a.status, running: a.status === "RUNNING" }
}

/** Calm states that may fold away on the Agents page. Anything else (unknown, stalled, waiting, unconfirmed, disabled)
 *  stays visible. */
const CALM = ["IDLE", "SLEEPING", "ACTIVE"]

/** An agent folds only with nothing held, nothing open, no recorded failure and a named calm state. */
export function foldable(a: Pick<Agent, "status" | "enabled" | "current_task" | "last_error">, d: DeskState, now: number, open: number): boolean {
  return !a.current_task && !a.last_error && open === 0 && CALM.includes(agentState(a, d, now).value)
}
