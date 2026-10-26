// The Overview headline: a problem, an unverified signal or stale data never reads as "operating normally".
import assert from "node:assert/strict"
import { test } from "node:test"
import type { DeskState } from "./api.ts"
import { operating } from "./operating.ts"

const NOW = Date.parse("2026-10-15T12:00:00Z")
const task = (status: string, key = status) => ({ key, title: key, status, priority: "P2", risk: "LOW", agent: "research", project: null,
  business_unit: null, approval_required: false, created_at: "2026-10-15T10:00:00Z", updated_at: "2026-10-15T11:00:00Z" })

function state(over: Partial<DeskState> = {}): DeskState {
  return {
    generated_at: "2026-10-15T12:00:00Z", control: { agents_paused: false },
    worker: { status: "HEALTHY", heartbeat_at: "2026-10-15T11:59:40Z" }, needs_you: [],
    overview: { revenue_today: null, revenue_month: null, active_projects: 1, running_agents: 0, open_tasks: 0, blocked_tasks: 0,
      incidents: 0, pending_approvals: 0, system_health: "HEALTHY" },
    agents: [], tasks: [], approvals: [], incidents: [], activity: [],
    health: { api: "ACTIVE", postgresql: "ACTIVE", resources: { cpu_percent: null, ram_percent: null, disk_percent: null, at: null },
      services: [{ key: "api", name: "API", status: "ACTIVE", risk: "LOW", last_verified: "2026-10-15T11:00:00Z" }], backup: { target: "r2", ok: true, integrity_verified: true, verified: true, at: "2026-10-15T03:00:00Z" } },
    ...over,
  } as DeskState
}
const need = (level: string) => ({ kind: "OTHER", rank: 1, level, title: "x", why: "y", affected: "", since: null, evidence: "", action: "", target: { page: "overview" } })

test("a healthy, verified, quiet company is the only 'operating normally'", () => {
  const o = operating(state({ tasks: [task("IN_PROGRESS")] }), { stale: false, now: NOW })
  assert.equal(o.level, "normal")
  assert.equal(o.headline, "Operating normally")
  assert.deepEqual(o.primary.target, "brief")
  assert.equal(o.summary[0].text, "1 task in progress")
  assert.ok(o.summary.some(s => s.text === "no open incidents"))
})

test("headline precedence: stale, critical, decision, paused, automation, stuck, unverified", () => {
  const base = { stale: false, now: NOW }
  assert.equal(operating(state({ needs_you: [need("critical")] }), { stale: true, now: NOW }).level, "stale")
  const crit = operating(state({ needs_you: [need("critical"), need("warning")] }), base)
  assert.equal(crit.level, "critical")
  assert.equal(crit.primary.label, "Review 2 items")
  assert.equal(operating(state({ needs_you: [need("warning")] }), base).headline, "1 item needs your decision")
  assert.equal(operating(state({ control: { agents_paused: true } }), base).level, "paused")
  assert.equal(operating(state({ worker: { status: "STALE", heartbeat_at: "2026-10-15T11:00:00Z" } }), base).headline, "Automation has stopped")
  assert.equal(operating(state({ worker: { status: "NO_DATA" } }), base).headline, "Automation is not verified")
  const stuck = operating(state({ tasks: [task("BLOCKED"), task("FAILED", "f")] }), base)
  assert.equal(stuck.headline, "2 tasks are stuck")
  assert.deepEqual(stuck.primary.target, { page: "tasks", key: "attention" })
  assert.equal(operating(state({ needs_you: undefined }), base).headline, "Attention queue not reported")
})

test("unproven backups or an open incident are never 'normal', even with an empty queue", () => {
  const base = { stale: false, now: NOW }
  const nobackup = operating(state({ health: { ...state().health, backup: null } }), base)
  assert.equal(nobackup.headline, "Backups are not verified")
  assert.equal(nobackup.vitals[2].value, "Never recorded")
  const unverified = operating(state({ health: { ...state().health, backup: { target: "r2", ok: true, integrity_verified: false, at: "2026-10-15T03:00:00Z" } } }), base)
  assert.equal(unverified.vitals[2].tone, "warning")
  const incident = { key: "INC-1", title: "x", description: null, business_unit: null, service: null, severity: "SEV2", status: "OPEN",
    detected_by: null, started_at: "2026-10-15T09:00:00Z", root_cause: null, founder_notified: false }
  assert.equal(operating(state({ incidents: [incident] }), base).headline, "1 incident open")
})

test("an empty incident list without a worker report is unverified, not 'none'", () => {
  const o = operating(state({ worker: { status: "NO_DATA" } }), { stale: false, now: NOW })
  assert.equal(o.vitals[1].value, "None recorded · unverified")
  assert.equal(o.vitals[0].value, "Not reported")
})

test("system checklist: no evidence is never green", async () => {
  const { systemChecks } = await import("./operating.ts")
  const checks = systemChecks(state({ worker: { status: "NO_DATA" }, health: { ...state().health, backup: null } }), NOW)
  const by = Object.fromEntries(checks.map(c => [c.key, c]))
  assert.equal(by.api.tone, "success")
  for (const k of ["automation", "providers", "reconciler", "deploy", "host"]) assert.equal(by[k].tone, "warning", k)
  assert.equal(by.backups.tone, "danger")
  assert.equal(by.restore.value, "Never run")
  const drift = systemChecks(state({ runtime: { api: { git_sha: "abc", started_at: "" }, worker: { git_sha: null, image: null, started_at: null },
    migration: { database: null, code_head: null }, stage_c_enabled: null, guardian_enabled: null, drift: ["worker SHA unknown"] } }), NOW)
  assert.equal(drift.find(c => c.key === "deploy")!.value, "1 mismatch")
})

test("a failing verified service is red; a never-verified one is not green", async () => {
  const { systemChecks } = await import("./operating.ts")
  const svc = (key: string, status: string, last_verified: string | null) => ({ key, name: key, status, risk: "HIGH", last_verified })
  const pick = (services: ReturnType<typeof svc>[]) => systemChecks(state({ health: { ...state().health, services } }), NOW).find(c => c.key === "services")!
  assert.equal(pick([svc("yt", "FAILING", "2026-10-01T00:00:00Z"), svc("api", "ACTIVE", "2026-10-15T00:00:00Z")]).value, "1 down")
  assert.equal(pick([svc("api", "ACTIVE", null)]).tone, "warning")
  assert.equal(pick([svc("api", "ACTIVE", "2026-10-15T00:00:00Z")]).tone, "success")
})

test("a task awaiting approval is never summarised as 'no approvals waiting' (live API: 0 records, 1 waiting task)", () => {
  const base = { stale: false, now: NOW }
  const o = operating(state({ tasks: [task("WAITING_APPROVAL")] }), base)
  assert.ok(!o.summary.some(s => s.text === "no approvals waiting"))
  assert.ok(o.summary.some(s => s.text === "1 task awaiting approval" && s.tone === "warning"))
  assert.ok(operating(state(), base).summary.some(s => s.text === "no approvals waiting"))
})

test("Home's health line comes from System's checklist: never green while a check is failing or unknown", async () => {
  const { healthSummary, systemChecks } = await import("./operating.ts")
  const checks = systemChecks(state({ worker: { status: "NO_DATA" }, health: { ...state().health, backup: null } }), NOW)
  const h = healthSummary(checks)
  assert.equal(h.tone, "danger")                                  // backups/restore never run
  assert.ok(h.open.some(c => c.key === "restore") && h.text.includes("Restore"))
  const neutral = healthSummary([{ key: "a", label: "A", value: "", detail: "", tone: "success" }, { key: "b", label: "B", value: "", detail: "", tone: "neutral" }])
  assert.equal(neutral.tone, "warning")                           // unknown is not verified
  assert.equal(neutral.text, "1 of 2 not verified · B")
  assert.equal(healthSummary([{ key: "a", label: "A", value: "", detail: "", tone: "success" }]).text, "All 1 checks verified")
  assert.equal(healthSummary([]).tone, "neutral")
})

test("System order: failing first, then late, then unknown; only verified checks fold", async () => {
  const { byUrgency } = await import("./operating.ts")
  const c = (key: string, tone: "danger" | "warning" | "neutral" | "info" | "success") => ({ key, label: key, value: "", detail: "", tone })
  const { problems, verified } = byUrgency([c("ok", "success"), c("unknown", "neutral"), c("late", "warning"), c("down", "danger"), c("note", "info")])
  assert.deepEqual(problems.map(x => x.key), ["down", "late", "unknown", "note"])
  assert.deepEqual(verified.map(x => x.key), ["ok"])
  assert.deepEqual(byUrgency([]), { problems: [], verified: [] })
})
