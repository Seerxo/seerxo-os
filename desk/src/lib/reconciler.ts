// The business reconciler's execution evidence (DeskState.reconciler_execution, built by
// app/reconciler_heartbeat.py:view) as the Automation page shows it. Fail closed: anything the backend did
// not prove is UNKNOWN; a failure outranks an older completion; COMPLETED only means the invocation finished
// (per-goal "OK" from the backend means every ACTIVE goal was examined without an error, not that any goal is on
// track), so it is never shown as a green "healthy".
import type { Tone } from "./api"

export type ReconcilerLabel = "UNKNOWN" | "STARTED" | "COMPLETED" | "FAILED" | "UNFINISHED" | "STALE"
export interface ReconcilerView {
  label: ReconcilerLabel
  tone: Tone
  detail: string
  /** The last attempt's time, only when the evidence is well-formed; never a guessed time. */
  last: string | null
  /** "Every N min" from the backend's cadence, or null when it did not report a sane one. */
  cadence: string | null
}

type Obj = Record<string, unknown>
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v)
const count = (v: unknown): number | null => (Number.isInteger(v) && (v as number) >= 0 ? (v as number) : null)
/** An ISO timestamp with a timezone that is not in the future; anything else is no timestamp at all. */
function time(v: unknown, now: number): string | null {
  if (typeof v !== "string" || !/(Z|[+-]\d\d:\d\d)$/.test(v)) return null
  const t = Date.parse(v)
  return Number.isFinite(t) && t <= now ? v : null
}
function since(at: string, now: number): string {
  const m = Math.max(0, Math.floor((now - Date.parse(at)) / 60_000))
  return m < 60 ? `${m}m` : m < 1440 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${Math.floor(m / 1440)}d ${Math.floor((m % 1440) / 60)}h`
}

/** The API's clock for this response (DeskState.generated_at, set just after the evidence was read). The
 *  browser's clock may run behind the host and would reject backend-verified evidence as "future". Falls
 *  back to `fallback` only when generated_at is missing or not a timezone-aware time. */
export function serverNow(generatedAt: unknown, fallback: number = Date.now()): number {
  if (typeof generatedAt !== "string" || !/(Z|[+-]\d\d:\d\d)$/.test(generatedAt)) return fallback
  const t = Date.parse(generatedAt)
  return Number.isFinite(t) ? t : fallback
}

/** `now` should be serverNow(DeskState.generated_at): ages and the "not in the future" check use the API clock. */
/** Fresh only if the backend said VERIFIED *and* the last attempt is within three expected cycles of `now`: a read taken
 *  long ago (an old snapshot, a stopped stream) must not keep saying "running". Without a cadence, the backend decides. */
export function reconcilerFresh(raw: { freshness?: unknown; last_attempt_at?: unknown; expected_cadence_s?: unknown } | null | undefined, now: number): boolean {
  if (!raw || raw.freshness !== "VERIFIED") return false
  const cadence = typeof raw.expected_cadence_s === "number" && raw.expected_cadence_s > 0 ? raw.expected_cadence_s : null
  const at = typeof raw.last_attempt_at === "string" ? Date.parse(raw.last_attempt_at) : NaN
  return cadence === null || (Number.isFinite(at) && now - at <= 3 * cadence * 1000)
}

export function reconcilerView(raw: unknown, now: number = Date.now()): ReconcilerView {
  const unknown = (detail: string, cadence: string | null = null): ReconcilerView =>
    ({ label: "UNKNOWN", tone: "neutral", detail, last: null, cadence })
  if (!isObj(raw)) return unknown("No execution evidence from this API (older API or unreadable response)")
  const cadenceS = count(raw.expected_cadence_s)
  const cadence = cadenceS && cadenceS % 60 === 0 ? `Every ${cadenceS / 60} min` : null
  const outcome = raw.outcome
  if (outcome === "NO_EVIDENCE") return unknown("No recorded invocation yet", cadence)
  if (outcome === "MALFORMED") return unknown("Malformed execution evidence; nothing is proven", cadence)
  if (typeof outcome !== "string" || !["STARTED", "UNFINISHED", "COMPLETED", "RAISED"].includes(outcome))
    return unknown("Unrecognised execution evidence; nothing is proven", cadence)
  const last = time(raw.last_attempt_at, now)
  if (!last) return unknown("Execution evidence without a valid attempt time; nothing is proven", cadence)
  const completed = time(raw.last_completed_at, now)
  const tasks = count(raw.tasks_created)
  const goalErrors = count(raw.goal_errors)
  const error = typeof raw.error === "string" && raw.error ? raw.error : null
  const previous = completed && completed !== last ? `previous completion ${since(completed, now)} ago` : "no earlier completion"
  const fresh = reconcilerFresh(raw, now)
  const view = (label: ReconcilerLabel, tone: Tone, parts: (string | null)[]): ReconcilerView =>
    ({ label, tone, detail: parts.filter(Boolean).join(" · "), last, cadence })

  // Failures first: a failed latest attempt outranks any older completion and any freshness.
  if (outcome === "RAISED")
    return view("FAILED", "danger", [`Invocation raised ${error ?? "an error"}`, previous])
  if (raw.per_goal_outcome === "FAILED")
    return view("FAILED", "danger", [`Invocation finished with ${goalErrors ?? "unknown"} goal error(s)`,
      tasks === null ? "tasks created unknown" : `${tasks} task(s) created`])
  if (outcome === "UNFINISHED")
    return view("UNFINISHED", "warning", ["Invocation started but did not complete", previous])
  if (outcome === "STARTED")
    return view("STARTED", "info", [`Invocation in progress, started ${since(last, now)} ago`, previous])
  // COMPLETED: only a completion with a valid time counts, and only while fresh.
  if (!completed) return unknown("Completion claimed without a valid completion time; nothing is proven", cadence)
  const goals = count(raw.goals_total)
  const perGoal = raw.per_goal_outcome === "OK" && goals !== null && goalErrors === 0
    ? `${goals} active goal(s) examined without error` : "per-goal outcome unknown"
  const facts = [tasks === null ? "tasks created unknown" : `${tasks} task(s) created`, perGoal]
  // The backend judges freshness by the attempt time, so the age shown is the attempt's.
  if (!fresh) return view("STALE", "warning", [`Evidence is stale: last attempt ${since(last, now)} ago, completed ${since(completed, now)} ago`, ...facts])
  return view("COMPLETED", "info", [`Invocation completed ${since(completed, now)} ago`, ...facts])
}
