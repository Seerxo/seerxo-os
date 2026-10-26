import type { Metric, TaskRow, Tone } from "./api"

const TONES: Record<Exclude<Tone, "neutral">, string[]> = {
  success: ["HEALTHY", "ACTIVE", "AVAILABLE", "DONE", "APPROVED", "SUCCEEDED", "RESOLVED", "CLEAR", "VERIFIED", "SLEEPING"],
  info: ["RUNNING", "READY", "QUEUED", "IN_PROGRESS", "STARTED", "REVIEW"],
  warning: ["WAITING", "NEEDS_APPROVAL", "WAITING_APPROVAL", "WAITING_EXTERNAL", "STALLED", "PAUSED",
    "QUOTA_EXHAUSTED", "RATE_LIMITED", "AUTH_REQUIRED", "DEGRADED", "STALE", "INVESTIGATING", "MEDIUM",
    "SEV2", "SEV3", "ESCALATED", "UNKNOWN", "MAINTENANCE", "EXPERIMENT", "OPEN"],
  danger: ["FAILED", "BLOCKED", "STOPPED", "BROKEN", "METERED_BLOCKED", "CRITICAL", "HIGH", "SEV0", "SEV1", "FAILING"],
}
export const toneOf = (v: string | null | undefined): Tone =>
  (Object.keys(TONES) as (keyof typeof TONES)[]).find(k => TONES[k].includes(String(v))) ?? "neutral"

// Founder-facing words for states whose enum reads like an error code.
const LABEL: Record<string, string> = {
  QUOTA_EXHAUSTED: "Subscription limit reached", RATE_LIMITED: "Rate limited", AUTH_REQUIRED: "Login required",
  METERED_BLOCKED: "Metered key blocked", WAITING_EXTERNAL: "Waiting on external", WAITING_APPROVAL: "Awaiting approval",
  NEEDS_APPROVAL: "Needs approval", IN_PROGRESS: "In progress", NOT_STARTED: "Not started", NO_DATA: "No data",
  SLEEPING: "Idle", STALE: "Stale",
}

/** Presentation only: turns upper-case status tokens inside API text (WAITING_EXTERNAL) into words.
 *  Identifiers such as TASK-0033 or INC-0001 are left untouched. The data itself is never changed. */
export const humanizeTokens = (text: string) =>
  text.replace(/(?<![\w-])[A-Z][A-Z_]{2,}(?![\w-])/g, w => humanize(w) as string)

export const humanize = (v: unknown) => {
  if (LABEL[String(v)]) return LABEL[String(v)]
  const t = String(v ?? "").replace(/_/g, " ").toLowerCase()
  return /^sev\d|^p\d$/.test(t) ? t.toUpperCase() : t.charAt(0).toUpperCase() + t.slice(1)
}

/** A metric as words beside its number: the source's own label, else its key without underscores (views_total → views total). */
export const metricName = (m: { metric: string; label?: string | null }) => m.label || m.metric.replaceAll("_", " ")

/** An exact moment, readable: "Sep 28, 2026, 3:00 AM" in the viewer's locale (no seconds; ago() gives the age). */
export const when = (v: string | null | undefined) =>
  (v ? new Date(v).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "No data yet")

export function ago(v: string | number | null | undefined) {
  if (!v) return "—"
  const t = Math.max(0, (Date.now() - new Date(v).getTime()) / 1e3)
  return t < 60 ? `${Math.round(t)}s`
    : t < 3600 ? `${Math.floor(t / 60)}m`
    : t < 86400 ? `${Math.floor(t / 3600)}h ${Math.floor((t % 3600) / 60)}m`
    : `${Math.floor(t / 86400)}d ${Math.floor((t % 86400) / 3600)}h`
}

export function duration(a: string | null, b: string | null) {
  if (!a || !b) return "—"
  const t = (new Date(b).getTime() - new Date(a).getTime()) / 1e3
  return t < 60 ? `${Math.round(t)}s` : t < 3600 ? `${Math.floor(t / 60)}m ${Math.round(t % 60)}s`
    : `${Math.floor(t / 3600)}h ${Math.floor((t % 3600) / 60)}m`
}

export const fmtNum = (v: number) =>
  Math.abs(v) >= 1e6 ? (v / 1e6).toFixed(1) + "M" : Math.abs(v) >= 1e3 ? (v / 1e3).toFixed(1) + "k"
    : String(Math.round(v * 100) / 100)

export const usd = (v: number | null | undefined) => (v == null ? "No data yet" : `$${Number(v).toFixed(2)}`)

const CLOSED = ["DONE", "CANCELLED", "IDEA", "BACKLOG"]
export const isOpen = (t: TaskRow) => !CLOSED.includes(t.status)

// One row per measured subject (e.g. one video): headline metric at the longest window.
const WIN_ORDER = ["7d", "72h", "24h", ""]
export interface Subject { label: string; win: string; head: Metric; rest: Metric[]; latest: string }
export function subjects(metrics: Metric[]): Subject[] {
  const by = new Map<string, { label: string; rows: Metric[] }>()
  for (const m of metrics) {
    if (!by.has(m.subject)) by.set(m.subject, { label: m.label || m.subject, rows: [] })
    by.get(m.subject)!.rows.push(m)
  }
  return [...by.values()].map(g => {
    const win = WIN_ORDER.find(w => g.rows.some(r => r.window === w)) ?? g.rows[0].window
    const at = g.rows.filter(r => r.window === win)
    const head = at.find(r => r.metric === "views") ?? at[0]
    return { label: g.label, win, head, rest: at.filter(r => r !== head),
      latest: g.rows.map(r => r.observed_at).sort().at(-1)! }
  })
}

// Capacity is not failure: quota/rate limits read as waiting, never as a broken provider.
export function capacity(name: string, w: { providers?: Record<string, string>; cooldown?: Record<string, { until: string; health: string }> }) {
  const c = w.cooldown?.[name]
  const live = c && new Date(c.until) > new Date()
  const health = live ? c!.health : w.providers?.[name]
  const until = live ? ` · until ${new Date(c!.until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}` : ""
  const text = ({
    AVAILABLE: "Live model response verified", CREDENTIALS_CONFIGURED: "Credentials configured · live access unverified",
    QUOTA_EXHAUSTED: "Subscription limit · waiting for reset" + until,
    RATE_LIMITED: "Rate limited · cooling down" + until, AUTH_REQUIRED: "Login required",
    METERED_BLOCKED: "Metered key blocked", DISABLED: "Disabled", BROKEN: "Not working",
  } as Record<string, string>)[health ?? ""] ?? (health || "No data yet")
  return { health, text }
}

export const WORKER_LABEL: Record<string, string> = {
  HEALTHY: "RUNNING", PAUSED: "PAUSED", STALE: "STOPPED", DEGRADED: "DEGRADED", NO_DATA: "NOT_STARTED",
}

export const REVENUE_SOURCE = "polar-api"
/** Latest reading of one series from a unit's portfolio metrics, or undefined (never 0). */
export const reading = (u: { metrics: Metric[] } | undefined, metric: string, window = "", source = REVENUE_SOURCE) =>
  u?.metrics.find(m => m.metric === metric && m.window === window && m.source === source)
