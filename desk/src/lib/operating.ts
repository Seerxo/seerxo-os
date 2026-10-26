// "Is my company operating correctly, what is happening, and do I need to act?" — answered from the read model only.
// The API's overview.system_health is HEALTHY unless a SEV0/1 incident or failing service exists; it ignores a stopped
// worker, missing backups and the reconciler. So the Desk derives its own headline here and never shows "operating
// normally" while any of those is unverified. Every count is the API's; a list the API did not return reads unknown.
import type { DeskState, Tone } from "./api"
import { reconcilerFresh } from "./reconciler.ts"

export type Level = "stale" | "critical" | "decision" | "paused" | "degraded" | "blocked" | "normal"
export type Target = { page: string; key?: string } | "brief"

export interface Vital { key: "automation" | "incidents" | "backups" | "services"; label: string; value: string; tone: Tone; page: string }
export interface Operating {
  level: Level; tone: Tone
  headline: string
  /** Counts that make up the one-sentence summary, in reading order. Zero parts that matter are kept ("no incidents"). */
  summary: { text: string; tone?: Tone }[]
  primary: { label: string; target: Target }
  vitals: Vital[]
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`
const count = (d: DeskState, states: string[]) => d.tasks.filter(t => states.includes(t.status)).length

function ageText(iso: string | null | undefined, now: number) {
  if (!iso) return null
  const s = Math.max(0, (now - new Date(iso).getTime()) / 1000)
  return s < 90 ? `${Math.round(s)}s ago` : s < 5400 ? `${Math.round(s / 60)}m ago` : s < 172800 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`
}

/** The worker's own STALE threshold (app/worker.py HEARTBEAT_STALE, default 10 min). The Desk applies it too: a
 *  status read long ago (an old read, a stopped stream) must not keep saying "running" past the last heartbeat. */
export const HEARTBEAT_STALE_MS = 10 * 60_000

export function vitals(d: DeskState, now: number): Vital[] {
  const w0 = d.worker
  const silent = !!w0?.heartbeat_at && now - Date.parse(w0.heartbeat_at) > HEARTBEAT_STALE_MS
  const w = w0 && silent && ["HEALTHY", "DEGRADED", "PAUSED"].includes(w0.status) ? { ...w0, status: "STALE" } : w0
  const heartbeat = ageText(w?.heartbeat_at, now)
  const automation: Vital = d.control.agents_paused
    ? { key: "automation", label: "Automation", value: "Paused by you", tone: "warning", page: "automation" }
    : !w || w.status === "NO_DATA" ? { key: "automation", label: "Automation", value: "Not reported", tone: "warning", page: "automation" }
    : w.status === "HEALTHY" ? { key: "automation", label: "Automation", value: heartbeat ? `Running · ${heartbeat}` : "Running", tone: "success", page: "automation" }
    : w.status === "STALE" ? { key: "automation", label: "Automation", value: heartbeat ? `Stopped · last seen ${heartbeat}` : "Stopped", tone: "danger", page: "automation" }
    : { key: "automation", label: "Automation", value: w.status === "DEGRADED" ? "No usable provider" : w.status.toLowerCase(), tone: "warning", page: "automation" }
  const severe = d.incidents.filter(i => ["SEV0", "SEV1"].includes(i.severity)).length
  // An empty incident list proves nothing while the watchdog/worker has not reported.
  const unverified = !w || w.status === "NO_DATA"
  const incidents: Vital = d.incidents.length
    ? { key: "incidents", label: "Incidents", value: severe ? `${d.incidents.length} open · ${severe} SEV0–1` : `${d.incidents.length} open`, tone: severe ? "danger" : "warning", page: "incidents" }
    : { key: "incidents", label: "Incidents", value: unverified ? "None recorded · unverified" : "None open", tone: unverified ? "warning" : "success", page: "incidents" }
  const b = d.health.backup
  const backups: Vital = !b ? { key: "backups", label: "Backups", value: "Never recorded", tone: "danger", page: "health" }
    : !b.ok ? { key: "backups", label: "Backups", value: `Last run failed · ${ageText(b.at, now)}`, tone: "danger", page: "health" }
    : !(b.verified ?? b.integrity_verified) ? { key: "backups", label: "Backups", value: `Not verified · ${ageText(b.at, now)}`, tone: "warning", page: "health" }
    : { key: "backups", label: "Backups", value: `Verified · ${ageText(b.at, now)}`, tone: "success", page: "health" }
  const s = servicesCheck(d)
  const services: Vital = { key: "services", label: "Services", value: s.value, tone: s.tone, page: "health" }
  return [automation, incidents, backups, services]
}

export function operating(d: DeskState, opts: { stale: boolean; now: number }): Operating {
  const needs = Array.isArray(d.needs_you) ? d.needs_you : null
  const critical = needs?.filter(n => n.level === "critical").length ?? 0
  const running = count(d, ["IN_PROGRESS"])
  const next = count(d, ["READY", "QUEUED"])
  const stopped = count(d, ["BLOCKED", "FAILED"])
  const review = count(d, ["REVIEW"])
  const approvals = d.approvals.length
  const awaiting = count(d, ["WAITING_APPROVAL"])
  const v = vitals(d, opts.now)
  const automationDown = v[0].tone === "danger" || (v[0].tone === "warning" && !d.control.agents_paused)

  const summary: Operating["summary"] = [
    { text: running ? `${plural(running, "task")} in progress` : "nothing executing" },
    ...(next ? [{ text: `${next} up next` }] : []),
    ...(stopped ? [{ text: `${stopped} blocked or failed`, tone: "danger" as Tone }] : []),
    ...(review ? [{ text: `${review} in review` }] : []),
    // An approval record and a task stopped at WAITING_APPROVAL are different facts; the API can report one without the other.
    ...(approvals ? [{ text: `${plural(approvals, "approval")} waiting`, tone: "warning" as Tone }] : []),
    ...(awaiting ? [{ text: `${plural(awaiting, "task")} awaiting approval`, tone: "warning" as Tone }] : []),
    ...(approvals || awaiting ? [] : [{ text: "no approvals waiting" }]),
    d.incidents.length ? { text: `${plural(d.incidents.length, "open incident")}`, tone: v[1].tone } : { text: v[1].value.toLowerCase().replace("none open", "no open incidents") },
  ]

  const review_n = (n: number) => ({ label: `Review ${plural(n, "item")}`, target: { page: "approvals" } as Target })
  let level: Level, headline: string, tone: Tone, primary: Operating["primary"]
  if (opts.stale) {
    level = "stale"; tone = "warning"; headline = "Showing last known data"
    primary = { label: "Refresh", target: { page: "refresh" } }
  } else if (critical) {
    level = "critical"; tone = "danger"; headline = `${plural(critical, "critical issue")} ${critical === 1 ? "needs" : "need"} you`
    primary = review_n(needs!.length)
  } else if (needs?.length) {
    level = "decision"; tone = "warning"; headline = `${plural(needs.length, "item")} ${needs.length === 1 ? "needs" : "need"} your decision`
    primary = review_n(needs.length)
  } else if (d.control.agents_paused) {
    level = "paused"; tone = "warning"; headline = "Agent execution is paused"
    primary = { label: "Review automation", target: { page: "automation" } }
  } else if (automationDown) {
    level = "degraded"; tone = v[0].tone; headline = v[0].tone === "danger" ? "Automation has stopped" : "Automation is not verified"
    primary = { label: "Check automation", target: { page: "automation" } }
  } else if (stopped) {
    level = "blocked"; tone = "warning"; headline = `${plural(stopped, "task")} ${stopped === 1 ? "is" : "are"} stuck`
    primary = { label: "Open stuck work", target: { page: "tasks", key: "attention" } }
  } else if (needs === null) {
    level = "degraded"; tone = "warning"; headline = "Attention queue not reported"
    primary = { label: "Open approvals", target: { page: "approvals" } }
  } else if (v.slice(1).some(x => x.tone !== "success")) {
    // Nothing is queued for a decision, yet an incident is open, backups are unproven or a service is down: never "normal".
    const bad = v.slice(1).find(x => x.tone === "danger") ?? v.slice(1).find(x => x.tone !== "success")!
    level = "degraded"; tone = bad.tone
    headline = bad.key === "incidents" ? (d.incidents.length ? `${plural(d.incidents.length, "incident")} open` : "Incidents not verified")
      : bad.key === "services" ? `Services: ${bad.value}` : "Backups are not verified"
    primary = { label: bad.key === "incidents" ? "Open incidents" : bad.key === "services" ? "Check services" : "Check backups", target: { page: bad.page } }
  } else {
    level = "normal"; tone = "success"; headline = running ? "Operating normally" : "Operating normally · idle"
    primary = { label: "Brief the CEO", target: "brief" }
  }
  return { level, tone, headline, summary, primary, vitals: v }
}

export interface Check { key: string; label: string; value: string; detail: string; tone: Tone; page?: string }

/** System's order: problems first (danger, warning, then unknown), verified last. Only `success` is verified. */
export function byUrgency(checks: Check[]): { problems: Check[]; verified: Check[] } {
  const rank: Record<Tone, number> = { danger: 0, warning: 1, neutral: 2, info: 3, success: 4 }
  return { problems: checks.filter(c => c.tone !== "success").sort((a, b) => rank[a.tone] - rank[b.tone]),
           verified: checks.filter(c => c.tone === "success") }
}

/** Home's one-line health summary, from the same checklist System shows. Only `success` counts as verified: a neutral,
 *  info or unknown check is "not verified", so Home can never be green while System lists a problem. */
export function healthSummary(checks: Check[]): { tone: Tone; text: string; open: Check[] } {
  const open = checks.filter(c => c.tone !== "success")
  if (!checks.length) return { tone: "neutral", text: "No checks reported", open }
  if (!open.length) return { tone: "success", text: `All ${checks.length} checks verified`, open }
  return { tone: open.some(c => c.tone === "danger") ? "danger" : "warning", open,
    text: `${open.length} of ${checks.length} not verified · ${open.map(c => c.label).join(", ")}` }
}

/** System status as a checklist: one honest line per subsystem, each with the evidence it rests on. A subsystem with no
 *  evidence is "unknown" (warning), never green. The API/PostgreSQL strings in /desk/state are constants, so the API row
 *  rests only on this response having been served. */
export function systemChecks(d: DeskState, now: number): Check[] {
  const [automation, incidents, backups] = vitals(d, now)
  const w = d.worker
  const providers = Object.entries(w?.providers ?? {})
  const bad = providers.filter(([, h]) => h !== "AVAILABLE")
  const rec = d.reconciler_execution
  const restore = d.health.last_restore
  const drift = d.runtime?.drift ?? null
  const r = d.health.resources
  const resourceAge = r.at ? (now - new Date(r.at).getTime()) / 60000 : null
  const peak = Math.max(...[r.cpu_percent, r.ram_percent, r.disk_percent].filter((v): v is number => v != null), -1)
  return [
    { key: "api", label: "Desk API", value: "Answering", tone: "success", detail: `This snapshot was generated ${ageText(d.generated_at, now) ?? "just now"}` },
    { ...automation, detail: w?.last_success_at ? `Last useful run ${ageText(w.last_success_at, now)}` : "No successful run recorded" },
    !providers.length ? { key: "providers", label: "Agent providers", value: "Not reported", tone: "warning", detail: "The worker has not reported provider health", page: "automation" }
      : bad.length ? { key: "providers", label: "Agent providers", value: `${bad.length} of ${providers.length} unavailable`, tone: "warning",
          detail: bad.map(([n, h]) => `${n}: ${h.toLowerCase().replaceAll("_", " ")}`).join(" · "), page: "automation" }
      : { key: "providers", label: "Agent providers", value: "All available", tone: "success", detail: providers.map(([n]) => n).join(" · "), page: "automation" },
    !rec ? { key: "reconciler", label: "Business reconciler", value: "Not reported", tone: "warning", detail: "This API version does not report it", page: "automation" }
      : reconcilerFresh(rec, now) && rec.outcome === "COMPLETED"
        ? { key: "reconciler", label: "Business reconciler", value: rec.per_goal_outcome === "FAILED" ? "Ran with goal errors" : "Running",
            tone: rec.per_goal_outcome === "FAILED" ? "warning" : "success", detail: `Last completed ${ageText(rec.last_completed_at, now) ?? "unknown"}`, page: "automation" }
        : { key: "reconciler", label: "Business reconciler", value: rec.freshness === "STALE" || (rec.freshness === "VERIFIED" && rec.outcome === "COMPLETED") ? "Stale" : "No evidence", tone: "warning",
            detail: `${rec.outcome.toLowerCase().replaceAll("_", " ")} · last attempt ${ageText(rec.last_attempt_at, now) ?? "never"}`, page: "automation" },
    servicesCheck(d),
    { ...incidents, detail: d.incidents.length ? d.incidents.slice(0, 2).map(i => `${i.severity} ${i.title}`).join(" · ") : "Recorded by the watchdog" },
    { ...backups, detail: d.health.backup ? `Target ${d.health.backup.target}` : "Nothing proves a restorable copy exists" },
    !restore ? { key: "restore", label: "Restore test", value: "Never run", tone: "danger", detail: "A backup is only proven by restoring it", page: "health" }
      : { key: "restore", label: "Restore test", value: !restore.ok ? "Failed" : restore.integrity_verified ? "Passed" : "Unverified",
          tone: !restore.ok ? "danger" : restore.integrity_verified ? "success" : "warning", detail: `${restore.target} · ${ageText(restore.at, now)}`, page: "health" },
    drift === null ? { key: "deploy", label: "Deployed version", value: "Not reported", tone: "warning", detail: "Runtime facts are missing from this API", page: "automation" }
      : drift.length ? { key: "deploy", label: "Deployed version", value: `${drift.length} mismatch${drift.length === 1 ? "" : "es"}`, tone: "warning", detail: drift[0], page: "automation" }
      : { key: "deploy", label: "Deployed version", value: "API, worker and schema agree", tone: "success", detail: `API ${d.runtime?.api.git_sha?.slice(0, 7) ?? "unknown"}`, page: "automation" },
    resourceAge === null ? { key: "host", label: "Host", value: "No reading", tone: "warning", detail: "The watchdog has not persisted host metrics" }
      : { key: "host", label: "Host", value: resourceAge > 30 ? "Stale reading" : peak >= 90 ? `${peak}% used` : "Within limits",
          tone: resourceAge > 30 || peak >= 75 ? "warning" : "success",
          detail: `CPU ${r.cpu_percent ?? "—"}% · memory ${r.ram_percent ?? "—"}% · disk ${r.disk_percent ?? "—"}% · ${ageText(r.at, now)}` },
  ]
}

/** Registered services: a verified FAILING/STOPPED one is red, DEGRADED amber; one never verified is unknown, not green. */
function servicesCheck(d: DeskState): Check {
  const s = d.health.services
  const verified = s.filter(x => x.last_verified)
  const down = verified.filter(x => ["FAILING", "STOPPED"].includes(x.status))
  const degraded = verified.filter(x => ["DEGRADED", "UNKNOWN"].includes(x.status))
  const unverified = s.length - verified.length
  if (!s.length) return { key: "services", label: "Services", value: "None registered", tone: "warning", detail: "The service registry is empty", page: "health" }
  if (down.length) return { key: "services", label: "Services", value: `${down.length} down`, tone: "danger",
    detail: down.map(x => `${x.name}: ${x.status.toLowerCase()}`).join(" · "), page: "health" }
  if (degraded.length || unverified) return { key: "services", label: "Services", value: degraded.length ? `${degraded.length} degraded` : `${unverified} not verified`,
    tone: "warning", detail: [...degraded.map(x => x.name), ...(unverified ? [`${unverified} never verified`] : [])].join(" · "), page: "health" }
  return { key: "services", label: "Services", value: `All ${s.length} active`, tone: "success", detail: "Each verified by the watchdog", page: "health" }
}
