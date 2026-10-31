// What runs by itself, from evidence the Desk already receives. No toggles: a loop's state is
// what the backend reports, and a loop with no evidence says so instead of claiming health.
// Expected cadences mirror scripts/company-os.cron (watchdog */10, Polar :17 hourly, backup daily).
import { useState, type ReactNode } from "react"
import { FileText } from "lucide-react"
import { apiText } from "@/lib/api"
import { InfoDialog, Pre } from "@/components/detail"
import { AgentsSwitch, RevokeStageC } from "@/components/controls"
import { Button } from "@/components/ui/button"
import { ago, capacity, REVENUE_SOURCE, toneOf, when } from "@/lib/format"
import { dataState, dataSummary } from "@/lib/freshness"
import type { Tone, Unit } from "@/lib/api"
import { cn } from "@/lib/utils"
import { reconcilerView, serverNow } from "@/lib/reconciler"
import { useData, useDesk, useNow } from "@/state"
import { Disclosure, Dot, Expandable, FigureStrip, Muted, PageHeader, Panel, PanelEmpty, PanelRow, Section, Status, TEXT } from "@/components/desk"
import { DeliveryPanel } from "@/components/delivery"
import { RunsChart } from "@/components/charts"
import { DataRows } from "@/components/data-rows"

const MIN = 60_000, HOUR = 60 * MIN
const unpack = (x: State) => ({ state: x.node, tone: x.tone })

/** `tone` is the state's own tone (the figures above the table count it); the node is what the row shows. */
interface Loop { name: string; what: ReactNode; cadence: string; last: string | null; state: ReactNode; tone: Tone }
type State = { node: ReactNode; tone: Tone }

/** Fresh enough for its cadence (with slack for one missed run)? null = no evidence yet. */
const health = (at: string | null | undefined, now: number, maxAge: number): "HEALTHY" | "STALE" | "NO_DATA" =>
  !at ? "NO_DATA" : now - new Date(at).getTime() <= maxAge ? "HEALTHY" : "STALE"

const LABEL = { HEALTHY: "On schedule", STALE: "Late", NO_DATA: "No evidence yet" }
const st = (value: string, label: string | undefined, tone: Tone): State => ({ node: <Status value={value} label={label} tone={tone} />, tone })
const fresh = (h: keyof typeof LABEL): State => st(h, LABEL[h], h === "HEALTHY" ? "success" : h === "STALE" ? "warning" : "neutral")
/** A data loop's state is about the DATA (newest reading per series), not proof that a poll ran: a
 *  successful poll of an unchanged upstream snapshot leaves the data stale, and that is what shows. */
function dataLoop(u: Pick<Unit, "metrics"> | undefined): State {
  if (!u) return st("NO_DATA", "Not reported", "neutral")
  const { state } = dataState(u)
  return state === "CURRENT" ? st("HEALTHY", "Data current", "success")
    : state === "PARTIAL" ? st("STALE", dataSummary(u), "warning")
    : state === "STALE" ? st("STALE", "Data stale", "warning")
    : state === "UNKNOWN" ? st("UNKNOWN", "Freshness unknown", "warning")
    : st("NO_DATA", "No data yet", "neutral")
}
const flag = (on: boolean | null | undefined) => on == null ? <Muted>Not reported</Muted>
  : <Status value={on ? "ENABLED" : "DISABLED"} label={on ? "On" : "Off"} tone={on ? "warning" : "neutral"} />

function reconcilerPresentation(e: unknown, generatedAt: unknown): { last: string | null; state: ReactNode; tone: Tone; detail: string; cadence: string } {
  // One source of truth for label, tone, wording and time (src/lib/reconciler.ts): nothing here re-derives
  // a status or formats an unchecked timestamp.
  const view = reconcilerView(e, serverNow(generatedAt))
  return { last: view.last, cadence: view.cadence ?? "Not reported", detail: view.detail, tone: view.tone,
    state: <Status value={view.label} tone={view.tone} /> }
}

export function AutomationPage() {
  const d = useData()
  const now = useNow("minute")  // loop cadences are minutes; seconds would re-render the page for nothing
  const w = d.worker
  // The Desk's recent task list, not the invocation's own count (tasks_created): the two can differ.
  const reconciled = d.tasks.filter(t => t.origin?.by === "business-reconciler").map(t => t.created_at).sort().at(-1) ?? null
  const reconciler = reconcilerPresentation(d.reconciler_execution, d.generated_at)
  const youtube = d.portfolio?.find(u => u.key === "youtube")
  const seerxo = d.portfolio?.find(u => u.key === "seerxo")
  const backup = d.health.backup
  const loops: Loop[] = [
    { name: "Unattended worker", what: "Picks up READY work; idle ticks make no model call", cadence: "Continuous",
      last: w?.heartbeat_at ?? null, state: <Status value={w?.status ?? "NO_DATA"} />, tone: toneOf(w?.status ?? "NO_DATA") },
    { name: "Watchdog", what: "Service, disk and publishing checks; opens incidents", cadence: "Every 10 min",
      last: d.health.resources.at, ...unpack(fresh(health(d.health.resources.at, now, 25 * MIN))) },
    { name: "Business reconciler", cadence: reconciler.cadence, last: reconciler.last, state: reconciler.state, tone: reconciler.tone,
      // The evidence explanation lives under the name, where it has room, not squeezed into the state column.
      what: <span className="grid gap-0.5"><span>Turns measured ACTIVE goals into work (runs with the watchdog)</span>
        {reconciler.detail && <span className={cn(reconciler.tone === "neutral" ? "text-muted-foreground" : TEXT[reconciler.tone])}>{reconciler.detail}</span>}
        <span>{reconciled ? `newest reconciler task in recent work ${ago(reconciled)} ago` : "no reconciler task in recent work"}</span></span> },
    { name: "Seerxo revenue sync", what: "Polar metrics, read-only. Evidence is the newest reading, not proof of a poll", cadence: "Hourly at :17",
      last: d.revenue?.observed_at ?? null,
      ...unpack(d.revenue === undefined ? st("NO_DATA", "Not reported", "neutral") : dataLoop(seerxo && { metrics: seerxo.metrics.filter(m => m.source === REVENUE_SOURCE) })) },
    { name: "YouTube metrics", what: "Reported by youtube-auto after its runs. Evidence is the newest reading", cadence: "Per youtube-auto run",
      last: youtube?.latest_at ?? null, ...unpack(dataLoop(youtube)) },
    { name: "Backup", what: backup ? `Latest ${backup.target} run` : "Off-site backup to R2", cadence: "Daily",
      last: backup?.at ?? null, ...unpack(!backup ? fresh("NO_DATA") : !backup.ok ? st("FAILED", undefined, "danger")
        : backup.uploaded === false ? st("LOCAL_ONLY", "Local only", "warning")
          : backup.verified !== true ? st("UNKNOWN", "Off-site proof missing", "warning")
            : fresh(health(backup.at, now, 30 * HOUR))) },
  ]
  const count = (t: Tone) => loops.filter(l => l.tone === t).length
  const missions = (d.improvements?.length ?? 0) > 0
  const r = d.runtime
  interface Row { name: string; what: string; state: ReactNode }
  const runtime: Row[] = [
    ...(["claude", "codex"] as const).map((n): Row => {
      const c = capacity(n, w ?? { status: "NO_DATA" })
      return { name: n.charAt(0).toUpperCase() + n.slice(1), what: "Subscription login in company-reasoner",
        state: c.health ? <Status value={c.health} label={c.health === "AVAILABLE" ? "Available" : c.text} /> : <Muted>No data yet</Muted> }
    }),
    { name: "Last task run", what: w?.last_run ? `${w.last_run.task} · ${w.last_run.provider}` : "—",
      state: <span className="text-sm">{w?.last_run ? <>{w.last_run.verdict || w.last_run.status}{w.last_run.ended_at && <Muted> · {ago(w.last_run.ended_at)} ago</Muted>}</> : <Muted>None recorded</Muted>}</span> },
    { name: "Live code", what: "API · worker commit, database migration",
      state: <span className="num text-sm">{r ? `${r.api.git_sha?.slice(0, 7) ?? "unknown"} · ${r.worker.git_sha?.slice(0, 7) ?? "unknown"} · ${r.migration.database ?? "unknown"}` : <Muted>Not reported</Muted>}</span> },
  ]
  const gates: Row[] = [
    { name: "Stage C code writes", what: "Isolated coder → PR → CI → founder merge", state: flag(r?.stage_c_enabled) },
    { name: "Repository Guardian", what: "Scheduled repository findings", state: flag(r?.guardian_enabled) },
    { name: "Autonomous goal planning", what: "CEO plans NO_WORK / BEHIND goals without being asked", state: flag(r?.reconciler_autonomous_planning) },
    { name: "Runtime autonomy", what: "Stage the worker reports", state: <span className="text-sm">{w?.autonomy ? `Stage ${w.autonomy.stage}` : <Muted>Not reported</Muted>}</span> },
    { name: "Automatic PRs", what: "Worker opens pull requests itself", state: flag(w?.autonomy && "auto_pr" in w.autonomy ? w.autonomy.auto_pr : null) },
    { name: "External wait release", what: "Release WAITING_EXTERNAL work on evidence", state: flag(w?.autonomy?.external_release) },
    // No runtime flag exists for this: it is a repository rule the Desk cannot see being enforced, so it says so.
    { name: "Merge & deploy", what: "Founder only, by repository policy; enforcement is not verified by the Desk",
      state: <Status value="POLICY" label="Founder, by policy" tone="warning" /> },
    { name: "Runtime drift", what: "Deployed API / worker / schema vs source",
      state: <span className={cn("text-sm", r?.drift.length && "text-warning")}>{!r ? <Muted>Not reported</Muted> : r.drift.length ? r.drift.join(" · ") : <Status value="NONE" label="None" tone="success" />}</span> },
  ]
  return <>
    <PageHeader title="Automation" description="What runs by itself, and the one switch that stops it."><KillSwitch /></PageHeader>
    <FigureStrip label="Automation at a glance" items={[
      { label: "On schedule", value: `${count("success") + count("info")} of ${loops.length}`, sub: "loops with fresh evidence" },
      { label: "Need attention", value: String(count("warning") + count("danger")), tone: count("danger") ? "danger" : count("warning") ? "warning" : undefined,
        sub: count("warning") + count("danger") ? "late, failing or unproven" : "nothing late" },
      { label: "No evidence", value: String(count("neutral")), sub: "not reported yet, never assumed healthy" },
      { label: "Agent execution", value: d.control.agents_paused ? "Paused" : "On", tone: d.control.agents_paused ? "warning" : undefined,
        sub: d.control.agents_paused ? "paused by you; services keep running" : "kill switch not engaged" },
    ]} />
    <Intervention loops={loops} />
    {/* A code mission in flight stays in view (its retry lives there); an empty or unreported panel folds below. */}
    {missions && <DeliveryPanel items={d.improvements} limit={4} expandable />}
    <Section title="Loops" count={loops.length}>
      {/* Loops that are late, failing or unproven stay in view; loops on schedule fold under one line. */}
      {(() => {
        const table = (rows: Loop[], label: string) => <DataRows label={label} rows={rows} rowKey={l => l.name} titleHead="Loop" title={l => l.name} desc={l => l.what}
          cols={[
            { head: "Expected", className: "text-sm text-muted-foreground", cell: l => l.cadence },
            { head: "Last evidence", className: "num text-sm", cell: l => <span title={l.last ? when(l.last) : undefined}>{l.last ? `${ago(l.last)} ago` : <Muted>—</Muted>}</span> },
          ]}
          aside={{ head: "State", cell: l => l.state }} />
        const fine = loops.filter(l => l.tone === "success" || l.tone === "info"), watch = loops.filter(l => !fine.includes(l))
        return <div className="grid min-w-0 gap-3">
          {watch.length > 0 ? table(watch, "Loops needing attention") : <p className="text-sm text-muted-foreground">Every loop is on schedule.</p>}
          {fine.length > 0 && <Expandable summary={<span className="text-[13px]"><span className="font-medium">{fine.length} {fine.length === 1 ? "loop" : "loops"} on schedule</span>
            <Muted> · {fine.map(l => l.name).join(", ")}</Muted></span>}>{table(fine, "Loops on schedule")}</Expandable>}
        </div>
      })()}
    </Section>
    {/* Folded, but its failures stay readable on the closed line: the hint carries the 14-day outcome counts. */}
    <Disclosure title="Run history" hint={runHint(d.run_series) + (missions ? "" : " · code missions")}>
      <Outcomes />
      {!missions && <DeliveryPanel items={d.improvements} />}
    </Disclosure>
    <Disclosure title="Technical details" hint="runtime, providers and autonomy gates">
    <Section title="Runtime">
      <DataRows label="Runtime" headless descColumn rows={runtime} rowKey={x => x.name} title={x => x.name} desc={x => x.what}
        aside={{ head: "State", cell: x => x.state }} />
    </Section>
    <Section title="Autonomy gates" description="Permission flags reported by the runtime; this page cannot change them. Stage C authority can be withdrawn here; it is granted per task, from the task." action={<RevokeStageC />}>
      <DataRows label="Autonomy gates" headless descColumn rows={gates} rowKey={x => x.name} title={x => x.name} desc={x => x.what}
        aside={{ head: "State", cell: x => x.state }} />
      <p className="mt-3 max-w-3xl rounded-lg border border-border/70 bg-surface/70 px-3.5 py-3 text-[13px] leading-5 text-muted-foreground">
        Stage C runtime readiness, code-writing permission and a completed independently reviewed pilot are separate facts.
        This read model reports the permission flag above; it does not prove preflight readiness or pilot completion.
      </p>
    </Section>
    </Disclosure>
  </>
}

/** The closed Run history line: the 14-day outcome counts, or that they are not reported (never zero for missing). */
function runHint(series: { SUCCEEDED: number; FAILED: number; ESCALATED: number; STARTED: number }[] | undefined): string {
  if (series === undefined) return "run outcomes not reported"
  const n = (k: "SUCCEEDED" | "FAILED" | "ESCALATED") => series.reduce((t, x) => t + x[k], 0)
  return `14 days: ${n("SUCCEEDED")} succeeded · ${n("FAILED")} failed · ${n("ESCALATED")} escalated`
}

/** What the agents' runs ended in, from run_series (the API's 14-day window). A run that ended is counted once. */
function Outcomes() {
  const d = useData()
  const series = d.run_series
  const sum = (k: "SUCCEEDED" | "FAILED" | "ESCALATED" | "STARTED") => (series ?? []).reduce((n, x) => n + x[k], 0)
  return (
    <Panel title="Execution outcomes" description="Agent runs, last 14 days">
      {series === undefined ? <PanelEmpty title="Not reported">This API version does not report runs.</PanelEmpty> : <div className="grid gap-3 p-4">
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4">
          {([["Succeeded", sum("SUCCEEDED"), "success"], ["Failed", sum("FAILED"), "danger"], ["Escalated", sum("ESCALATED"), "warning"], ["Still running", sum("STARTED"), "info"]] as const)
            .map(([label, n, tone]) => <div key={label} className="grid gap-0.5">
              <span className="eyebrow">{label}</span>
              <span className={cn("num text-xl leading-7 font-semibold tracking-[-0.03em]", n && tone === "danger" && "text-destructive", n && tone === "warning" && "text-warning")}>{n}</span>
            </div>)}
        </div>
        <RunsChart series={series} height="h-40" />
      </div>}
    </Panel>
  )
}

/** Where the founder can unblock the system, each linking to where it is resolved. Counts only, from the read model. */
function Intervention({ loops }: { loops: { name: string; tone: Tone }[] }) {
  const d = useData()
  const { go } = useDesk()
  const w = d.worker ?? { status: "NO_DATA" }
  const cooling = Object.keys(w.providers ?? {}).filter(p => { const c = capacity(p, w); return c.health && !["AVAILABLE", "CREDENTIALS_CONFIGURED"].includes(c.health) })
  const failed = d.tasks.filter(t => t.status === "FAILED").length, blocked = d.tasks.filter(t => t.status === "BLOCKED").length
  const awaiting = d.tasks.filter(t => t.status === "WAITING_APPROVAL").length + d.approvals.length
  const late = loops.filter(l => l.tone === "warning" || l.tone === "danger")
  const rows: { key: string; title: string; meta: string; n: number; tone: Tone; go: () => void }[] = [
    { key: "failed", title: "Failed tasks", meta: "an agent run ended in failure", n: failed, tone: "danger", go: () => go("tasks", "attention") },
    { key: "blocked", title: "Blocked tasks", meta: "cannot continue on their own", n: blocked, tone: "danger", go: () => go("tasks", "attention") },
    { key: "approval", title: "Waiting for your approval", meta: "approval records and tasks stopped for a decision", n: awaiting, tone: "warning", go: () => go("approvals") },
    { key: "providers", title: "Providers cooling down", meta: cooling.length ? cooling.join(", ") : "quota or rate limit", n: cooling.length, tone: "warning", go: () => go("agents") },
    { key: "loops", title: "Loops late or failing", meta: late.length ? late.map(l => l.name).join(", ") : "every loop on schedule", n: late.length, tone: "warning", go: () => go("automation") },
  ]
  return (
    <Panel title="Needs intervention" description="What will not resolve by itself">
      {rows.map(r => <PanelRow key={r.key} onClick={r.go} className="items-center py-2.5" lead={<Dot tone={r.n ? r.tone : "neutral"} />} title={r.title} meta={r.meta}
        trail={<span className={cn("num text-[15px] font-semibold", r.n ? (r.tone === "danger" ? "text-destructive" : "text-warning") : "text-muted-foreground")}>{r.n}</span>} />)}
    </Panel>
  )
}

/** The kill switch and the read-only brief. */
function KillSwitch() {
  const [brief, setBrief] = useState<string | null>(null)
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button variant="outline" className="h-10" onClick={() => apiText("/company/brief").then(setBrief, e => setBrief(`Brief unavailable: ${(e as Error).message}`))}>
        <FileText />Company brief
      </Button>
      <AgentsSwitch />
      <InfoDialog title="Company brief" open={brief !== null} onOpenChange={o => !o && setBrief(null)}><Pre>{brief}</Pre></InfoDialog>
    </div>
  )
}
