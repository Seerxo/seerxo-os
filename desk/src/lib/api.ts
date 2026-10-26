// Read model served by app/desk.py (/desk/state) and the detail endpoints the Desk scope
// allows (app/api_auth.py DESK). Fields an older API lacks are optional: the UI shows UNKNOWN,
// never an invented value.

export type Tone = "success" | "info" | "warning" | "danger" | "neutral"

export interface TaskRow {
  key: string; title: string; status: string; priority: string; risk: string
  agent: string | null; project: string | null; business_unit: string | null
  approval_required: boolean; created_at: string; updated_at: string
  /** Who opened it and on what trigger. Absent = older API. */
  origin?: Origin
}
export interface Origin { by: string; automatic: boolean; trigger: string | null; reason?: string | null }
export interface Agent {
  key: string; name: string; role: string; team?: string; reports_to: string | null
  enabled?: boolean; status: string; current_task: { key: string; title: string; status: string } | null
  last_run: string | null; last_success: string | null; last_error: string | null
  cost_today_usd?: number; model_class?: string
}
export interface Need {
  kind: string; rank: number; level: "critical" | "warning" | "info" | string
  title: string; why: string; affected: string; since: string | null; evidence: string
  action: string; target: { page: string; key?: string | null }
}
export interface Metric {
  subject: string; metric: string; window: string; value: number; unit: string | null
  source: string; observed_at: string; freshness: "VERIFIED" | "STALE"; label: string | null
}
export interface SeriesPoint { observed_at: string; value: number }
export interface MetricSeries {
  source: string; subject: string; metric: string; window: string; days: number; unit: string | null; points: SeriesPoint[]
}
/** The readings of one reported series over the last `days` days, oldest first. Empty points means no reading. */
export function metricSeries(m: Pick<Metric, "source" | "subject" | "metric" | "window">, days = 30): Promise<MetricSeries> {
  const query = new URLSearchParams({ source: m.source, subject: m.subject, metric: m.metric, window: m.window, days: String(days) })
  return api<MetricSeries>(`/desk/metrics/series?${query}`)
}
export interface Unit {
  key: string; name: string; status: string; priority: string
  freshness: "VERIFIED" | "STALE" | "UNAVAILABLE"; latest_at: string | null; metrics: Metric[]
}
export interface Improvement {
  task: string; title: string; stage: string; updated_at: string | null
  repository: string | null; branch: string | null; pr_number: number | null
  pr_state: string | null; ci_state: string | null; failed_check: string | null
  repairs: string | null; review_verdict: string | null; risk: string | null
  expected_effect: string | null; verified_at: string | null
}
export interface Worker {
  status: "HEALTHY" | "PAUSED" | "STALE" | "DEGRADED" | "NO_DATA" | string
  heartbeat_at?: string | null; heartbeat_age_s?: number | null; last?: string
  autonomy?: { stage: string; code_write_flag: boolean; code_write_implemented: boolean; external_release: boolean
    stage_c_foundation?: string[]; auto_pr?: boolean }
  providers?: Record<string, string>; cooldown?: Record<string, { until: string; health: string }>
  last_run?: { task: string; provider: string; status: string; verdict: string | null; ended_at: string | null } | null
  last_success_at?: string | null
}
export interface Approval {
  key: string; action: string; reason: string | null; expected_effect: string | null; risk: string
  rollback: string | null; requested_by: string; created_at: string
}
export interface Incident {
  key: string; title: string; description: string | null; business_unit: string | null
  service: string | null; severity: string; status: string; detected_by: string | null
  started_at: string; root_cause: string | null; founder_notified: boolean
}
export interface Event {
  id: number; type: string; source: string; severity: string; at: string; title: string
  detail: string | null; task: string | null; business_unit: string | null
  latest_observation?: { title: string; detail: string; severity: string; observed_at: string } | null
}
export interface GoalMeasure {
  selector: { subject: string; metric: string; window?: string; source?: string } | null
  baseline_value: number | null; target_value: number | null
  latest: { value: number; unit: string | null; source: string; observed_at: string } | null
  freshness: "VERIFIED" | "STALE" | "UNAVAILABLE"; progress: number | null
}
export interface Priority {
  kind: "goal" | "task"; key?: string; title: string; why: string | null; success?: string
  owner?: string; status?: string; priority: string; metric?: string; baseline?: string
  target?: string; deadline?: string | null
  id?: number; open_tasks?: number; top_task?: string | null; measure?: GoalMeasure | null
}
export interface Runtime {
  api: { git_sha: string | null; started_at: string }
  worker: { git_sha: string | null; image: string | null; started_at: string | null }
  migration: { database: string | null; code_head: string | null }
  stage_c_enabled: boolean | null; reconciler_autonomous_planning?: boolean | null; guardian_enabled: boolean | null; drift: string[]
}
export interface ReconcilerExecution {
  last_attempt_at: string | null; last_completed_at: string | null
  outcome: "NO_EVIDENCE" | "MALFORMED" | "STARTED" | "UNFINISHED" | "COMPLETED" | "RAISED" | string
  duration_ms: number | null; tasks_created: number | null; goal_errors: number | null
  /** "OK" = every ACTIVE goal examined without error (structured evidence); never "goals on track". */
  per_goal_outcome: "OK" | "FAILED" | "unknown" | string; goals_total?: number | null; error: string | null
  freshness: "VERIFIED" | "STALE" | "UNAVAILABLE" | string
  evidence_source: string; expected_cadence_s: number
}

export interface DeskState {
  runtime?: Runtime
  /** Optional for older APIs; absence is UNKNOWN, never a completed run. */
  reconciler_execution?: ReconcilerExecution
  generated_at: string
  control: { agents_paused: boolean }
  worker?: Worker
  needs_you?: Need[]
  portfolio?: Unit[]
  improvements?: Improvement[]
  current_priority?: Priority | null
  costs?: { new_autonomous_spend_usd: number; items: { key: string; class: string; purpose?: string; note?: string; scope?: string }[] }
  run_series?: { day: string; STARTED: number; SUCCEEDED: number; FAILED: number; ESCALATED: number }[]
  /** Created to DONE, for tasks closed in the last 30 days. Null means no valid sample. */
  task_lead_time?: { median_hours: number | null; sample_size: number; review_pending: number }
  /** What a new task can attach to. Absent = older API. */
  catalog?: { projects: { key: string; name: string; business_unit: string | null }[]; goals: { id: number; title: string; status: string; project: string | null }[] }
  /** Latest verified revenue (Polar); null = no verified source. Absent = older API. */
  revenue?: { today: number | null; month: number | null; mrr: number | null; total?: number | null; unit: string | null; source: string; observed_at: string; freshness: "VERIFIED" | "STALE"; series?: Partial<Record<"today" | "month" | "mrr" | "total", { source: string; observed_at: string; freshness: "VERIFIED" | "STALE" } | null>> } | null
  overview: {
    revenue_today: number | null; revenue_month: number | null; active_projects: number
    running_agents: number; open_tasks: number; blocked_tasks: number; incidents: number
    pending_approvals: number; system_health: string
  }
  agents: Agent[]; tasks: TaskRow[]; approvals: Approval[]; incidents: Incident[]; activity: Event[]
  health: {
    api: string; postgresql: string
    resources: { cpu_percent: number | null; ram_percent: number | null; disk_percent: number | null; at: string | null }
    collection?: { containers: string; container_health: string }
    watchdog_conditions?: { event_id: number; type: string; severity: string; title: string
      detail: string | null; observed_at: string | null; episode: number; delivery_status: string }[]
    watchdog_conditions_total?: number; watchdog_conditions_truncated?: boolean
    services: { key: string; name: string; status: string; risk: string; last_verified: string | null }[]
    backup: { target: string; ok: boolean; uploaded?: boolean; integrity_verified: boolean; verified?: boolean; at: string } | null
    /** Newest restore attempt, including failures; never hide a newer failure behind an older pass. */
    last_restore?: { target: string; ok: boolean; uploaded?: boolean; integrity_verified: boolean; at: string } | null
  }
}
export interface Run {
  id: number; task: string | null; status: string; provider: string | null; started_at: string
  ended_at: string | null; error: string | null; supervisor_verdict: string | null; supervisor_notes: string | null
}
export interface TaskDetail {
  key: string; title: string; description: string | null; objective: string | null; status: string
  priority: string; risk: string; assigned_agent: string | null; project: string | null
  business_unit: string | null; success_criteria: string | null; context: unknown
  origin?: Origin; goal?: { id: number; title: string; status: string } | null
  approval_required: boolean; dependencies: { key: string; status: string }[]; runs: Run[]
  /** Attempts used and allowed (absent on older APIs). */
  retry_count?: number; max_attempts?: number
  artifacts: unknown; result: string | null; error: string | null
  supervisor: { verdict: string; notes: string | null } | null
  created_at: string; started_at: string | null; completed_at: string | null
}
export interface AgentDetail extends Agent {
  unattended_tasks?: { task: string; status: string; reason: string | null; approval_required: boolean }[]
  identity: { description: string; capabilities: string[]; skills: string[]; allowed_tools: string[] }
  schedule: unknown; triggers: { type: string; event_type?: string }[]; memory_policy: unknown
  recent_tasks: { key: string; title: string; status: string }[]; recent_runs: Run[]
  recent_learnings: { title: string; lesson: string }[]
  notebook: { note: string; refs: number; last_seen_at: string }[]
}
export interface Memory {
  decisions: { key: string; title: string; decision: string; reason: string | null; status: string; approved_by: string | null; created_at: string }[]
  learnings: { id: number; title: string; lesson: string; tags: string | string[] | null; created_at: string; status?: string }[]
  notes: { note: string; refs: number; last_seen_at: string; promoted: boolean; tags?: string | string[] | null }[]
  permanent: string | null
}

// company-api serves this page, so every call is same-origin (Access + Tunnel, or SSH).
export class ApiError extends Error {
  readonly status: number
  /** A genuine sign-out: 401, or Access's sign-in page. An unreadable or broken body is not one (status 0 for both). */
  readonly signIn: boolean
  constructor(status: number, detail: string, signIn = status === 401) {
    super(`${status} ${detail}`); this.status = status; this.signIn = signIn
  }
}

/** Positive evidence that a fetch ended on Cloudflare Access's login page: it was redirected to
 *  /cdn-cgi/access/login on the Access team domain (*.cloudflareaccess.com) or on this origin. Probed 2026-10-22: an
 *  expired session gets a 302 to https://<team>.cloudflareaccess.com/cdn-cgi/access/login/<desk-host>?..., and that
 *  page sends no CORS headers, so in a browser the fetch usually fails as a network TypeError instead (which keeps the
 *  draft). Any other HTML (a proxy or challenge page, an error page) is merely unreadable, never a sign-out. */
export function accessLogin(r: Pick<Response, "redirected" | "url">): boolean {
  if (!r.redirected || !r.url) return false
  try {
    const u = new URL(r.url)
    const here = typeof location === "undefined" ? null : location.origin
    const login = u.pathname === "/cdn-cgi/access/login" || u.pathname.startsWith("/cdn-cgi/access/login/")   // segment boundary
    return u.protocol === "https:" && login
      && (u.hostname.endsWith(".cloudflareaccess.com") || u.origin === here)
  } catch { return false }
}

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, body === undefined ? undefined : {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  })
  if (!response.ok) {
    let detail: unknown = response.statusText
    try { detail = (await response.json()).detail || detail } catch { /* not JSON */ }
    throw new ApiError(response.status, typeof detail === "string" ? detail : JSON.stringify(detail))
  }
  if (!response.headers.get("content-type")?.includes("application/json"))
    throw accessLogin(response) ? new ApiError(0, "Sign in again, then reload", true)
      : new ApiError(0, "Unexpected (non-JSON) API response; reload", false)
  try { return await response.json() } catch { throw new ApiError(0, "Invalid API response; reload to sign in", false) }
}

export async function apiText(path: string): Promise<string> {
  const response = await fetch(path)
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
  return response.text()
}
