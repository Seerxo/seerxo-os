import { useEffect, useState, type ReactNode } from "react"
import type { Event } from "@/lib/api"
import { ago, when } from "@/lib/format"
import { cn } from "@/lib/utils"
import { ChevronRight } from "lucide-react"
import { byUrgency, systemChecks } from "@/lib/operating"
import { watchdogView } from "@/lib/watchdog"
import { groupEvents, hasRepeats } from "@/lib/event-groups"
import { useChanges, useData, useDesk, useNow, type Page } from "@/state"
import { Calm, ChangeMark, Disclosure, Dot, Expandable, Facts, Muted, PageHeader, Panel, PanelRow, Section, Status, FigureStrip } from "@/components/desk"
import { installed, serviceWorker } from "@/lib/pwa"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"

const dayKey = (iso: string) => new Date(iso).toDateString()
const dayLabel = (iso: string) => {
  const d = new Date(iso), today = new Date(), y = new Date(Date.now() - 864e5)
  return d.toDateString() === today.toDateString() ? "Today" : d.toDateString() === y.toDateString() ? "Yesterday"
    : d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })
}

/** System timeline: event first, time and source secondary; grouped by day when long. */
export function EventList({ events, grouped, onTask }: { events: Event[]; grouped?: boolean; onTask?: (key: string) => void }) {
  const changes = useChanges()
  if (!events.length) return <Calm title="No activity recorded">Events appear as the system does real work.</Calm>
  return (
    <ol className="grid grid-cols-1">
      {events.map((e, i) => (
        <li key={e.id} className="grid">
          {grouped && (i === 0 || dayKey(events[i - 1].at) !== dayKey(e.at)) && (
            <span className="pt-4 pb-1 text-xs font-medium text-muted-foreground first:pt-0">{dayLabel(e.at)}</span>
          )}
          <div className={cn("grid grid-cols-[48px_10px_minmax(0,1fr)] items-baseline gap-x-3 rounded-md py-1.5", changes[`event:${e.id}`] && "desk-flash")}>
            <span className="num text-xs text-muted-foreground">{new Date(e.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}</span>
            <span className={cn("size-1.5 translate-y-[-1px] self-center rounded-full",
              ["CRITICAL", "HIGH"].includes(e.severity) ? "bg-destructive" : e.severity === "MEDIUM" ? "bg-warning" : "bg-muted-foreground/40")} />
            <div className="min-w-0">
              <div className="text-sm leading-snug [overflow-wrap:anywhere]">{e.title} <ChangeMark c={changes[`event:${e.id}`]} className="align-[1px]" /></div>
              <div className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{e.source}{e.task && <> · {onTask
                ? <button type="button" onClick={() => onTask(e.task!)} className="rounded-sm font-mono underline-offset-2 hover:text-foreground hover:underline focus-visible:ring-2 focus-visible:ring-ring/60 focus-visible:outline-none">{e.task}</button>
                : <span className="font-mono">{e.task}</span>}</>}</div>
              {e.latest_observation && <div className="mt-1 text-xs text-muted-foreground [overflow-wrap:anywhere]">
                Latest observation · {e.latest_observation.title}: {e.latest_observation.detail}
              </div>}
            </div>
          </div>
        </li>
      ))}
    </ol>
  )
}

const SEV = { high: ["CRITICAL", "HIGH"], medium: ["MEDIUM"] }
const sevOf = (e: Event) => SEV.high.includes(e.severity) ? "high" : SEV.medium.includes(e.severity) ? "medium" : "other"
type SevFilter = "all" | "high" | "medium" | "other"

/** Append-only events with the filters the founder needs to find one: severity, source and free text. */
export function ActivityPage() {
  const d = useData()
  const { open } = useDesk()
  const [sev, setSev] = useState<SevFilter>("all")
  const [source, setSource] = useState("all")
  const [q, setQ] = useState("")
  const [view, setView] = useState<"grouped" | "timeline" | null>(null)
  const events = d.activity
  const sources = [...new Set(events.map(e => e.source))].sort()
  const n = (f: SevFilter) => f === "all" ? events.length : events.filter(e => sevOf(e) === f).length
  const shown = events.filter(e => (sev === "all" || sevOf(e) === sev) && (source === "all" || e.source === source)
    && (!q || `${e.title} ${e.type} ${e.task ?? ""} ${e.detail ?? ""}`.toLowerCase().includes(q.toLowerCase())))
  const filtered = sev !== "all" || source !== "all" || q !== ""
  const groups = groupEvents(shown)
  const changes = useChanges()
  const mode = view ?? (hasRepeats(events) ? "grouped" : "timeline")
  return <>
    <PageHeader title="Activity" description="Recent recorded events, newest first. Older history is not paged here." />
    {events.length > 0 && <FigureStrip label="Activity at a glance" items={[
      { label: "Recent events", value: String(events.length), sub: `newest ${ago(events.reduce((m, e) => (e.at > m ? e.at : m), events[0].at))} ago` },
      { label: "High or critical", value: String(n("high")), tone: n("high") ? "danger" : undefined, sub: n("high") ? "worth a look" : "none recorded", onClick: () => setSev("high") },
      { label: "Medium", value: String(n("medium")), tone: n("medium") ? "warning" : undefined, sub: "degraded or slow", onClick: () => setSev("medium") },
      { label: "Sources", value: String(sources.length), sub: sources.slice(0, 3).join(", ") + (sources.length > 3 ? "…" : "") },
    ]} />}
    {events.length > 0 && <div className="grid gap-2 sm:grid-cols-[auto_minmax(0,1fr)_200px] sm:items-center">
      <nav aria-label="Filter by severity" className="task-filters-scroll -mx-1 flex flex-nowrap gap-1 px-1">
        {(["all", "high", "medium", "other"] as const).map(f => (
          <button key={f} type="button" aria-current={sev === f ? "true" : undefined} onClick={() => setSev(f)}
            className={cn("inline-flex h-9 shrink-0 items-center gap-1.5 rounded-full border px-3.5 text-[13px] transition-colors duration-150",
              sev === f ? "border-foreground/15 bg-foreground text-background" : "border-border bg-card text-muted-foreground hover:text-foreground", !n(f) && sev !== f && "border-dashed")}>
            {({ all: "All", high: "High+", medium: "Medium", other: "Other" })[f]}<span className={cn("num text-xs", sev === f ? "text-background/70" : "text-muted-foreground")}>{n(f)}</span>
          </button>))}
      </nav>
      <Input type="search" value={q} onChange={e => setQ(e.target.value)} placeholder="Find an event, type or task…" aria-label="Find an event" className="h-10" />
      <Select value={source} onValueChange={setSource}>
        <SelectTrigger className="h-10 w-full" aria-label="Filter by source"><SelectValue /></SelectTrigger>
        <SelectContent><SelectItem value="all">All sources</SelectItem>{sources.map(x => <SelectItem key={x} value={x}>{x}</SelectItem>)}</SelectContent>
      </Select>
    </div>}
    {events.length > 0 && !shown.length
      ? <div className="panel grid justify-items-start gap-2 px-4 py-5 text-sm"><span className="font-medium">No event matches these filters</span>
          <Button size="sm" variant="outline" onClick={() => { setSev("all"); setSource("all"); setQ("") }}>Clear filters</Button></div>
      : <div className="grid gap-2">
          {events.length > 0 && <div className="flex flex-wrap items-center justify-between gap-2 px-0.5">
            <span className="text-xs text-muted-foreground">{mode === "grouped"
              ? `${groups.length} distinct ${groups.length === 1 ? "event" : "events"} in ${shown.length} recorded`
              : `${shown.length}${filtered ? ` of ${events.length}` : ""} recent events, newest first`}</span>
            <div role="group" aria-label="Activity view" className="inline-flex rounded-md border bg-card p-0.5">
              {(["grouped", "timeline"] as const).map(v => <button key={v} type="button" aria-pressed={mode === v} onClick={() => setView(v)}
                className={cn("h-7 rounded-[5px] px-2.5 text-xs font-medium transition-colors", mode === v ? "bg-foreground text-background" : "text-muted-foreground hover:text-foreground")}>
                {v === "grouped" ? "Grouped" : "Timeline"}</button>)}
            </div>
          </div>}
          {mode === "grouped" && events.length > 0
            ? <Panel>{groups.map(g => <PanelRow key={g.key} className={changes[`event:${g.latest.id}`] && "desk-flash"} lead={<Dot tone={SEV.high.includes(g.latest.severity) ? "danger" : SEV.medium.includes(g.latest.severity) ? "warning" : "neutral"} />}
                title={<>{g.latest.title} <ChangeMark c={changes[`event:${g.latest.id}`]} className="align-[1px]" /></>} onClick={g.latest.task ? () => open({ kind: "task", key: g.latest.task! }) : undefined}
                meta={<>{g.latest.source}{g.latest.task && <> · <span className="font-mono">{g.latest.task}</span></>}{" · "}{g.count > 1
                  ? <span title={`${when(g.first)} – ${when(g.last)}`}>last {ago(g.last)} ago · first {ago(g.first)} ago</span>
                  : <span title={when(g.last)}>{ago(g.last)} ago</span>}</>}
                trail={g.count > 1 ? <span className="num rounded-full bg-muted px-2 py-0.5 font-medium text-foreground" aria-label={`recorded ${g.count} times`}>×{g.count}</span> : undefined} />)}</Panel>
            : <div className={cn(events.length > 0 && "panel px-4 py-3")}><EventList events={shown} grouped onTask={key => open({ kind: "task", key })} /></div>}
        </div>}
  </>
}

const IMPACT: Record<string, string> = { CRITICAL: "business-critical", HIGH: "high impact", MEDIUM: "medium impact", LOW: "low impact" }
const ServiceRow = ({ name, sub, risk, status }: { name: string; sub: string; risk?: string; status: ReactNode }) => (
  <div className="flex min-h-12 items-center gap-3 border-b border-border/70 py-2 text-sm last:border-0">
    <div className="min-w-0 flex-1 [overflow-wrap:anywhere]"><div className="font-medium">{name}</div><div className="text-xs text-muted-foreground">{sub}</div>
      {/* Phones: the criticality tag sits under the name; beside it, it left the name ~70 px and split words. */}
      {risk && <div className="text-xs text-muted-foreground sm:hidden">{IMPACT[risk] ?? risk.toLowerCase()}</div>}</div>
    {/* Business criticality, not state: a red tag next to a healthy service read as an alarm. */}
    {risk && <span className="w-28 shrink-0 text-right text-xs whitespace-nowrap text-muted-foreground max-sm:hidden" title="How critical this service is to the business">{IMPACT[risk] ?? risk.toLowerCase()}</span>}
    {/* Sized to its content, capped so a long state label wraps instead of running into the next column. */}
    <span className="max-w-44 shrink-0 text-right">{status}</span>
  </div>
)

function InstallApp() {
  const [error, setError] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const standalone = installed()
  const ios = /iPhone|iPad|iPod/i.test(navigator.userAgent)
  useEffect(() => {
    if (serviceWorker) void serviceWorker.then(() => setReady(true)).catch(() => setError("Service worker registration failed. Reload while online, then try again."))
  }, [])
  return <Section title="Install on your phone">
    {standalone ? <p className="text-sm">SEZ Company is running as an installed app.</p>
      : !serviceWorker ? <p className="text-sm text-muted-foreground">Installation is unavailable here. Open this site over HTTPS in a browser that supports service workers.</p>
        : <div className="grid gap-2 text-sm">
            <p>{ios ? "In Safari, tap Share, then Add to Home Screen, then Add." : "Open your browser menu and choose Install app or Add to Home Screen."}</p>
            <p className="text-xs text-muted-foreground">The installed app needs a connection and your Cloudflare Access sign-in to show company data.</p>
            {error ? <p role="alert" className="text-destructive">{error}</p> : <p className="text-xs text-muted-foreground">{ready ? "App support ready" : "Checking app support…"}</p>}
          </div>}
  </Section>
}

const sha7 = (v: string | null | undefined) => (v && /^[0-9a-f]{7,40}$/.test(v) ? v.slice(0, 7) : null)

/** What runs in production, from the runtime facts the API reports. A release gateway, ledger or founder signature is
 *  not in this API's read model, so it is named as not reported: a merged PR or a green CI is never shown as a deploy. */
function Releases() {
  const d = useData()
  const r = d.runtime
  const api = sha7(r?.api.git_sha), worker = sha7(r?.worker.git_sha)
  const schemaOk = !!r && !!r.migration.database && r.migration.database === r.migration.code_head
  return (
    <Panel title="Releases" description="What runs in production, from the API's own runtime facts">
      <PanelRow lead={<Dot tone={!r ? "warning" : r.drift.length ? "danger" : api ? "success" : "warning"} />}
        title={<span className="flex min-w-0 flex-wrap items-baseline gap-x-3"><span>Running version</span>
          <span className="num font-mono text-[13px]">{api ?? "unknown"}</span></span>}
        meta={!r ? "Runtime facts are missing from this API" : [
          r.api.started_at && `API started ${ago(r.api.started_at)} ago`,
          worker ? (worker === api ? "worker on the same commit" : `worker on ${worker}`) : "worker commit unknown",
          schemaOk ? `schema ${r.migration.database} matches the code` : `schema ${r.migration.database ?? "unknown"} · code head ${r.migration.code_head ?? "unknown"}`,
          ...r.drift].filter(Boolean).join(" · ")} />
      <PanelRow lead={<Dot tone="neutral" />} title="Release gateway"
        meta="Not reported by this API: no candidate, review, signature, host verification or ledger state is visible to the Desk. A merged pull request or green CI is not a deployment; the running version above is the only deployment evidence shown." />
    </Panel>
  )
}

export function HealthPage() {
  const d = useData()
  const r = d.health.resources
  const collection = d.health.collection ?? { containers: "UNKNOWN", container_health: "UNKNOWN" }
  const seen = r.at ? new Date(r.at) : null
  const stale = !!seen && Date.now() - seen.getTime() > 30 * 60e3
  const { go } = useDesk()
  const b = d.health.backup
  const restore = d.health.last_restore
  const watchdogConditions = d.health.watchdog_conditions ?? []
  const watchdog = watchdogView(d.health)
  // health.api / health.postgresql are constants in the read model, not evidence: only the registry's verified rows are listed.
  const services = d.health.services
  const now = useNow("minute")
  const checks = systemChecks(d, now)
  const { problems, verified } = byUrgency(checks)
  const row = (c: (typeof checks)[number]) => (
    <PanelRow key={c.key} onClick={c.page ? () => go(c.page as Page) : undefined} lead={<Dot tone={c.tone} />}
      title={<span className="flex min-w-0 items-baseline gap-3"><span className="shrink-0">{c.label}</span>
        <span className={cn("truncate text-[13px] font-medium", c.tone === "danger" ? "text-destructive" : c.tone === "warning" ? "text-warning" : c.tone === "success" ? "text-success" : "text-muted-foreground")}>{c.value}</span></span>}
      meta={c.detail} trail={c.page ? <ChevronRight className="size-4 text-muted-foreground/50" /> : undefined} />)
  return <>
    <PageHeader title="System status" description="What needs attention first; every verified check, with its evidence, below." />
    {/* Problems first. Anything not verified (failing, late or unknown) is a problem; only verified checks fold away. */}
    {problems.length > 0 ? <Panel title="Needs attention" count={problems.length} description="Failing, late or not proven by evidence">{problems.map(row)}</Panel>
      : checks.length ? <Calm title="Every check is verified">Each one is backed by recorded evidence.</Calm>
      : <Calm title="No checks reported">This API reported nothing to verify; that is not a healthy result.</Calm>}
    {verified.length > 0 && <div className="-mt-2"><Expandable summary={<span className="text-[13px]"><span className="font-medium">{verified.length} of {checks.length} checks verified</span>
      <Muted> · {verified.map(c => c.label).join(", ")}</Muted></span>}><Panel title="Verified">{verified.map(row)}</Panel></Expandable></div>}
    <Releases />
    <Disclosure title="Details" hint="host, services, watchdog conditions, backup and restore, phone install">
    <Section title="Host" action={seen ? <span className={cn("num", stale && "text-warning")} title={`Watchdog reading · ${when(r.at)}`}>
      {stale ? `Stale · read ${ago(r.at)} ago · watchdog may not be running` : `Read ${ago(r.at)} ago`}</span> : <span>No metrics persisted</span>}>
      <div className="grid gap-6 sm:grid-cols-3">
        {([["CPU", r.cpu_percent], ["Memory", r.ram_percent], ["Disk", r.disk_percent]] as const).map(([label, v]) => (
          <div key={label} className="grid gap-2">
            <div className="flex items-baseline justify-between text-sm"><Muted>{label}</Muted>
              <b className={cn("num text-lg font-semibold", v == null && "text-muted-foreground", stale && "opacity-70")}>{v == null ? "—" : `${v}%`}</b></div>
            <div className="h-1 overflow-hidden rounded-full bg-muted">
              {v != null && <i className={cn("block h-full rounded-full", v >= 90 ? "bg-destructive" : v >= 75 ? "bg-warning" : "bg-success", stale && "opacity-50")} style={{ width: `${Math.min(100, v)}%` }} />}
            </div>
            {v == null && <span className="text-xs text-muted-foreground">{seen ? "Not collected" : "No metrics persisted"}</span>}
          </div>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">The latest watchdog reading only: host history is not recorded, so no trend is drawn.</p>
      <Facts items={[
        ["Container snapshot", <Status key="snapshot" value={collection.containers}
          label={collection.containers === "OK" ? "Complete" : "Unknown"}
          tone={collection.containers === "OK" ? "success" : "warning"} />],
        ["Container health", <Status key="health" value={collection.container_health}
          label={collection.container_health === "OK" ? "Complete" : "Unknown"}
          tone={collection.container_health === "OK" ? "success" : "warning"} />],
      ]} />
    </Section>
    <div className="grid grid-cols-1 gap-10 lg:grid-cols-[1.4fr_1fr]">
      <Section title="Services">
        {services.map(s => <ServiceRow key={s.key} name={s.name} sub={`${s.key} · ${s.last_verified ? `verified ${ago(s.last_verified)} ago` : "not verified yet"}`} risk={s.risk} status={s.last_verified ? <Status value={s.status} /> : <Status value="UNKNOWN" label="Registered, not verified" />} />)}
      </Section>
      <Section title="Active watchdog conditions" action={<span className={cn("num text-xs", watchdog.reported ? "text-muted-foreground" : "text-warning")}>{watchdog.count}</span>}>
        {watchdogConditions.length ? <div className="grid">
          {watchdogConditions.map(condition => <div key={`${condition.event_id}:${condition.episode}`} className="grid gap-1 border-b border-border/70 py-3 last:border-0">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0"><div className="text-sm font-medium [overflow-wrap:anywhere]">{condition.title}</div>
                <div className="text-xs text-muted-foreground">{condition.type} · episode {condition.episode} · {condition.delivery_status}</div></div>
              <Status value={condition.severity} />
            </div>
            {condition.detail && <div className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{condition.detail}</div>}
            <div className="text-xs text-muted-foreground">Last observed {condition.observed_at ? `${ago(condition.observed_at)} ago` : "UNKNOWN"}</div>
          </div>)}
        </div> : <p className="text-sm text-muted-foreground">{watchdog.empty}</p>}
      </Section>
      <div className="grid content-start gap-10">
        <Section title="Backup" action={b ? <Status
          value={!b.ok ? "FAILED" : b.verified === true ? "VERIFIED"
            : b.integrity_verified && b.uploaded === false ? "LOCAL_ONLY" : "UNKNOWN"}
          label={!b.ok ? "Failed" : b.verified === true ? "Off-site verified"
            : b.integrity_verified && b.uploaded === false ? "Local only"
              : b.uploaded === true ? "Off-site proof missing" : "Source or integrity unknown"}
          tone={!b.ok ? "danger" : b.verified === true ? "success"
            : b.integrity_verified && b.uploaded === false ? "warning" : "neutral"} /> : undefined}>
          {b ? <Facts items={[["Target", b.target], ["Last run", `${when(b.at)} · ${ago(b.at)} ago`],
            ["Run succeeded", b.ok ? "Yes" : "No"], ["Uploaded off-site", b.uploaded == null ? "Unknown" : b.uploaded ? "Yes" : "No"],
            ["Integrity reported", b.integrity_verified ? "Yes" : "No"],
            ["Verified event linked", b.verified === true ? "Yes" : "No"]]} />
            : <Calm title="No backup recorded">Nothing proves a restorable copy exists.</Calm>}
        </Section>
        <Section title="Restore test" action={restore ? <Status
          value={!restore.ok ? "FAILED" : restore.integrity_verified ? "PASSED" : "UNKNOWN"}
          label={!restore.ok ? "Failed" : restore.integrity_verified ? "Passed" : "Unverified"}
          tone={!restore.ok ? "danger" : restore.integrity_verified ? "success" : "warning"} /> : undefined}>
          {restore ? <Facts items={[["Target", restore.target], ["Last attempt", `${when(restore.at)} · ${ago(restore.at)} ago`],
            ["Outcome", !restore.ok ? "Failed" : restore.integrity_verified ? "Passed" : "Unverified"],
            ["Source", restore.uploaded === true ? "Off-site copy" : restore.uploaded === false ? "Local copy" : "Unknown"]]} />
            : <Calm title="No restore test recorded">Backup integrity checks do not prove a restore succeeded.</Calm>}
        </Section>
      </div>
    </div>
    <p className="text-sm text-muted-foreground">Worker, providers and loops: <button className="inline-flex min-h-6 items-center underline-offset-2 hover:text-foreground hover:underline" onClick={() => go("automation")}>Automation</button> · Costs: <button className="inline-flex min-h-6 items-center underline-offset-2 hover:text-foreground hover:underline" onClick={() => go("finance")}>Finance</button></p>
    <InstallApp />
    </Disclosure>
  </>
}
