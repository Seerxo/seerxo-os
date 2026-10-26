// Business numbers with provenance only: a unit with no reported metric says "no verified
// source", never 0. Every value carries its source and age.
import { pageTitle } from "@/lib/nav"
import type { Unit } from "@/lib/api"
import { ago, fmtNum, humanize, isOpen, metricName, reading, subjects, usd, when } from "@/lib/format"
import { dataState, dataSummary, freshLabel, isUnverified, staleSuffix } from "@/lib/freshness"
import { cn } from "@/lib/utils"
import { useData, useDesk } from "@/state"
import { Calm, ListRow, Muted, PageHeader, Section, Status, Summary, FigureStrip, TaskKey } from "@/components/desk"
import { Stat } from "@/pages/finance"
import { EventList } from "@/pages/system"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { DataRows } from "@/components/data-rows"
import { TrendGrid } from "@/components/trend"
import { ChevronRight } from "lucide-react"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"

function useUnitFacts() {
  const d = useData()
  return (key: string) => ({
    work: d.tasks.filter(t => t.business_unit === key && isOpen(t)),
    blocked: d.tasks.filter(t => t.business_unit === key && ["BLOCKED", "FAILED"].includes(t.status)),
    incidents: d.incidents.filter(i => i.business_unit === key),
  })
}

export function MetricRows({ u, limit }: { u: Unit; limit: number }) {
  const subs = subjects(u.metrics).slice(0, limit)
  if (!subs.length) return <Calm title="No metrics reported">No value is shown until a source reports with provenance.</Calm>
  return (
    <div className="grid divide-y divide-border/70">
      {subs.map(x => (
        <div key={x.label + x.win + x.latest} className="grid grid-cols-[1fr_auto] items-baseline gap-x-6 gap-y-0.5 py-2.5 first:pt-0 last:pb-0">
          <span className="min-w-0 truncate text-sm">{x.label}</span>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="num text-right text-sm"><b className="font-semibold">{fmtNum(x.head.value)}</b> <Muted className="text-xs">{metricName(x.head)}{x.win && ` · ${x.win}`}</Muted></span>
            </TooltipTrigger>
            <TooltipContent>{x.head.source} · {when(x.head.observed_at)}{staleSuffix(x.head)}</TooltipContent>
          </Tooltip>
          <span className={cn("col-span-2 truncate text-xs text-muted-foreground", isUnverified(x.head) && "text-warning")}>
            {x.head.source} · {ago(x.head.observed_at)} ago{staleSuffix(x.head)}{x.rest.length > 0 && ` · ${x.rest.map(r => `${fmtNum(r.value)} ${metricName(r)}${staleSuffix(r)}`).join(" · ")}`}
          </span>
        </div>
      ))}
    </div>
  )
}

/** The few numbers that matter per unit, each from a reported series with its own freshness; never a 0 for missing. */
interface Key { text: string; stale: boolean; label: string }
function keyNumbers(u: Unit): Key[] {
  const mrr = reading(u, "mrr")
  if (mrr) {
    const subs = reading(u, "active_subscriptions"), total = reading(u, "revenue", "all_time")
    return [{ text: `MRR ${usd(mrr.value)}`, stale: isUnverified(mrr), label: freshLabel(mrr) },
      ...(subs ? [{ text: `${fmtNum(subs.value)} subscriptions`, stale: isUnverified(subs), label: freshLabel(subs) }] : []),
      ...(total ? [{ text: `${usd(total.value)} all time`, stale: isUnverified(total), label: freshLabel(total) }] : [])]
  }
  const h = subjects(u.metrics)[0]
  return h ? [{ text: `${fmtNum(h.head.value)} ${metricName(h.head)}${h.win ? ` · ${h.win}` : ""}`, stale: isUnverified(h.head), label: freshLabel(h.head) }] : []
}

/** One comparable row per unit, used by Overview and Projects; drill-down on click. */
export function BusinessTable({ only }: { only?: Unit[] } = {}) {
  const d = useData()
  const { go } = useDesk()
  const facts = useUnitFacts()
  const units = only ?? d.portfolio
  if (!Array.isArray(units)) return <Calm status={<Status value="UNKNOWN" />} title="Portfolio unavailable">This API version does not report it.</Calm>
  if (!units.length) return <Calm title="No business units recorded">Units appear once the company records them. Nothing is estimated while none are recorded.</Calm>
  return <>
    <div className="hidden lg:block">
      <DataRows label="Business units" rows={units} rowKey={u => u.key} titleHead="Unit"
        title={u => <>{u.name}{u.status !== "ACTIVE" && <Muted className="ml-2 text-xs font-normal">{humanize(u.status)}</Muted>}</>}
        onRowClick={u => go("project", u.key)}
        cols={[
          { head: "Key numbers", className: "num text-sm whitespace-normal", cell: u => {
            const nums = keyNumbers(u)
            return nums.length ? nums.map((n, i) => <span key={n.text} className={cn("block sm:inline sm:whitespace-nowrap", n.stale && "text-warning")}>
              {i > 0 && <Muted className="max-sm:hidden"> · </Muted>}{n.text}{n.stale && ` · ${n.label}`}</span>) : <Muted>Nothing reported</Muted>
          } },
          { head: "Open work", className: "num text-right", cell: u => {
            const f = facts(u.key), open = f.work.length, attention = f.incidents.length + f.blocked.length
            return <>{open || <Muted>0</Muted>}{attention > 0 && <span className="block text-xs text-warning">{attention} attention</span>}</>
          } },
          { head: "Data", className: "text-right text-xs text-muted-foreground", cell: u => <>
            {u.latest_at ? `${ago(u.latest_at)} ago` : "—"}
            {dataState(u).stale > 0 && <span className="block text-warning">{dataSummary(u)}</span>}</> },
        ]} />
    </div>
    <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:hidden" aria-label="Business units">
      {units.map(u => {
        const f = facts(u.key), nums = keyNumbers(u), attention = f.incidents.length + f.blocked.length
        const stale = dataState(u).stale > 0
        return <li key={u.key}>
          <button type="button" onClick={() => go("project", u.key)}
            className="group grid min-h-[178px] w-full content-start gap-4 rounded-xl border bg-card p-4 text-left transition-[border-color,background-color,box-shadow] duration-150 hover:border-brand/30 hover:bg-surface/70 hover:shadow-xs focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
            <span className="flex min-w-0 items-start justify-between gap-3">
              <span className="grid min-w-0 gap-1"><span className="truncate text-[15px] font-semibold tracking-tight">{u.name}</span>
                <span className="text-xs text-muted-foreground">{u.priority} priority · {humanize(u.status)}</span></span>
              <Status value={u.status} />
            </span>
            <span className="grid gap-2 border-y border-border/70 py-3">
              <span className="text-[11px] font-medium text-muted-foreground">Key numbers</span>
              {nums.length ? <span className="grid gap-1.5">
                {nums.map(n => <span key={n.text} className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-2">
                  <span className={cn("num min-w-0 [overflow-wrap:anywhere] text-[13px] font-semibold tracking-tight", n.stale && "text-warning")}>{n.text}</span>
                  {n.stale && <span className="text-right text-[11px] text-warning">{n.label}</span>}
                </span>)}
              </span>
                : <span className="text-[13px] text-muted-foreground">No verified metrics reported</span>}
            </span>
            <span className="flex items-center justify-between gap-3 text-xs">
              <span className="num text-muted-foreground">{f.work.length} open work{attention > 0 && <span className="text-warning"> · {attention} needs attention</span>}</span>
              <span className="grid min-w-0 justify-items-end gap-1 text-right">
                <span className="num text-muted-foreground">{u.latest_at ? `${ago(u.latest_at)} ago` : "No observation"}</span>
                {stale && <span className="max-w-full text-[11px] text-warning">{dataSummary(u)}</span>}
              </span>
            </span>
          </button>
        </li>
      })}
    </ul>
  </>
}

const LIVE = ["ACTIVE", "EXPERIMENT"]

/** The portfolio: live units first (where agents work and numbers are reported), the rest folded away. */
export function ProjectsPage() {
  const d = useData()
  const { open } = useDesk()
  const facts = useUnitFacts()
  const units = d.portfolio ?? []
  const live = units.filter(u => LIVE.includes(u.status)), rest = units.filter(u => !LIVE.includes(u.status))
  const attention = units.filter(u => { const f = facts(u.key); return f.incidents.length + f.blocked.length > 0 }).length
  const c = d.current_priority
  return <>
    <PageHeader title={pageTitle("projects")} description="Each business, its verified numbers and its work."
      />
    <FigureStrip label="Portfolio at a glance" items={[
      { label: "Live", value: String(live.length), sub: "active business units" },
      { label: "Need attention", value: String(attention), tone: attention ? "warning" : undefined, sub: attention ? "stale or missing data" : "data is current" },
      { label: "Not active", value: String(rest.length), sub: "ideas and paused units" },
    ]} />
    {c && (
      <section className="panel grid gap-1.5 px-4 py-3.5" aria-label="Current priority">
        <span className="eyebrow">Current priority · {c.kind === "goal" ? "goal" : "highest-priority open work"}</span>
        <button type="button" className="text-left text-[15px] font-semibold tracking-tight hover:underline disabled:no-underline"
          disabled={c.kind !== "task"} onClick={() => c.kind === "task" && c.key && open({ kind: "task", key: c.key })}>{c.title}</button>
        {c.why && <p className="text-[13px] text-muted-foreground">{c.why}</p>}
        {c.kind === "goal" && (c.metric || c.target || c.open_tasks === 0 || (c.measure && !c.measure.latest)) && <p className="text-xs text-muted-foreground">{c.metric && c.target && <>{c.metric}: {c.baseline || "—"} → {c.target}</>}
          {c.measure && !c.measure.latest && <span className="text-warning"> · no verified observation yet</span>}
          {c.open_tasks === 0 && <span className="text-warning"> · no open work serves it</span>}</p>}
      </section>
    )}
    {!Array.isArray(d.portfolio) ? <Calm status={<Status value="UNKNOWN" />} title="Project data unavailable">This API version does not report it.</Calm>
      : !units.length ? <Calm title="No business units recorded">Units appear once the company records them. Nothing is estimated while none are recorded.</Calm>
      : <>
        <section className="grid gap-3" aria-label="Live businesses">
          <h2 className="px-0.5 text-[15px] font-semibold tracking-tight">Live <span className="num text-[13px] font-normal text-muted-foreground">{live.length}</span></h2>
          {live.length ? <BusinessTable only={live} /> : <p className="panel px-4 py-4 text-[13px] text-muted-foreground">No business unit is active. Agents only take recurring work for active units.</p>}
        </section>
        {rest.length > 0 && (
          <Collapsible className="grid gap-3">
            <CollapsibleTrigger className="group flex min-h-11 items-center gap-2 px-0.5 text-left text-[15px] font-semibold tracking-tight">
              <ChevronRight className="size-4 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />Not active
              <span className="num text-[13px] font-normal text-muted-foreground">{rest.length} · {[...new Set(rest.map(u => humanize(u.status).toLowerCase()))].join(", ")}</span>
            </CollapsibleTrigger>
            <CollapsibleContent><BusinessTable only={rest} /></CollapsibleContent>
          </Collapsible>
        )}
      </>}
  </>
}

/** The unit's headline numbers: money for a unit with a revenue source, otherwise its first
 *  measured series. Nothing is shown for a unit with no verified source (Key metrics says so). */
function UnitStats({ u }: { u: Unit }) {
  // Each figure carries its own reading's source, age and freshness: series refresh independently,
  // so the unit-level flag (some series stale) must not colour a figure that is current.
  type Cell = [string, string, string, boolean]
  const cell = (label: string, r: ReturnType<typeof reading>, f: (v: number) => string): Cell =>
    [label, r ? f(r.value) : "—", r ? `${ago(r.observed_at)} ago${staleSuffix(r)} · ${r.source}` : "not reported", isUnverified(r)]
  const stats: Cell[] = reading(u, "mrr") || reading(u, "revenue", "all_time")
    ? [cell("Revenue · all time", reading(u, "revenue", "all_time"), usd), cell("MRR", reading(u, "mrr"), usd),
       cell("Active subscriptions", reading(u, "active_subscriptions"), fmtNum), cell("This month", reading(u, "revenue", "mtd"), usd)]
    : subjects(u.metrics).slice(0, 4).map((x): Cell => [`${humanize(x.head.metric)}${x.win ? ` · ${x.win}` : ""}`,
        fmtNum(x.head.value), `${ago(x.head.observed_at)} ago${staleSuffix(x.head)} · ${x.head.source} · ${x.label}`, isUnverified(x.head)])
  if (!stats.length) return null
  // As many columns as numbers: a lone figure must not leave an empty grey cell beside it.
  const cols = ["", "grid-cols-1", "grid-cols-2", "grid-cols-1 sm:grid-cols-3", "grid-cols-2 sm:grid-cols-4"][stats.length]
  return (
    <div className={cn("grid gap-px overflow-hidden rounded-lg border bg-border", cols)}>
      {stats.map(([label, value, sub, stale]) => <Stat key={label} label={label} value={value} sub={sub} warn={stale} />)}
    </div>
  )
}

/** Goals this unit's projects serve, and the projects themselves (from the Desk catalog). */
function UnitGoals({ u }: { u: Unit }) {
  const d = useData()
  if (!d.catalog) return null
  const projects = d.catalog.projects.filter(p => p.business_unit === u.key)
  const keys = new Set(projects.map(p => p.key))
  const goals = d.catalog.goals.filter(g => g.project && keys.has(g.project))
  return (
    <div className="grid gap-10 lg:grid-cols-2">
      <Section title="Goals" action={<span className="num">{goals.length}</span>}>
        {goals.length ? <ul className="grid grid-cols-1 divide-y divide-border/70" aria-label="Goals">{goals.map(g => (
          <li key={g.id} className="flex items-start justify-between gap-3 py-2 text-sm first:pt-0">
            <span className="min-w-0 break-words">{g.title}</span><Status value={g.status} />
          </li>))}</ul> : <Calm title="No open goal">Set a goal so work for this unit is measured against an outcome.</Calm>}
      </Section>
      <Section title="Projects" action={<span className="num">{projects.length}</span>}>
        {projects.length ? <ul className="grid grid-cols-1 divide-y divide-border/70" aria-label="Projects">{projects.map(p => (
          <li key={p.key} className="flex items-start justify-between gap-3 py-2 text-sm first:pt-0">
            <span className="min-w-0 break-words">{p.name}</span><span className="max-w-[45%] shrink-0 font-mono text-xs break-all text-muted-foreground">{p.key}</span>
          </li>))}</ul> : <Calm title="No projects recorded" />}
      </Section>
    </div>
  )
}

export function ProjectDetail({ unitKey }: { unitKey: string }) {
  const d = useData()
  const { open } = useDesk()
  const facts = useUnitFacts()
  const u = d.portfolio?.find(x => x.key === unitKey)
  if (!u) return <Calm title={`Project “${unitKey}” not found`}>It may have been renamed; open Projects to choose again.</Calm>
  const f = facts(u.key)
  const events = d.activity.filter(e => e.business_unit === u.key).slice(0, 10)
  const agents = [...new Set(f.work.map(t => t.agent).filter(Boolean))] as string[]
  const priority = d.tasks.filter(t => t.business_unit === u.key && isOpen(t) && ["P0", "P1"].includes(t.priority))[0]
  return <>
    <PageHeader eyebrow="Business unit" title={u.name} description={`Priority ${u.priority} · ${dataSummary(u).toLowerCase()}`}
      meta={<><Status value={u.status} /><Summary parts={[[f.work.length, "open"], [f.incidents.length, f.incidents.length > 1 ? "incidents" : "incident", "warning"]]} /></>} />
    {/* Results and execution are kept apart: finished tasks are not business results. */}
    <Section title="Business results" description="Verified readings from reporting sources only. Task completion is not counted here.">
      {u.metrics.length ? <div className="grid gap-4"><UnitStats u={u} />
        {/* The unit's own 30-day trends; per-video YouTube series stay in Growth's table. */}
        <TrendGrid metrics={u.metrics.filter(m => !m.subject.startsWith("youtube:")).slice(0, 4)} /></div> : <Calm title="No verified result reported">Nothing is shown until a source reports for this project; that is unknown, not zero.</Calm>}
    </Section>
    {priority && (
      <Section title="Current priority">
        <ListRow onClick={() => open({ kind: "task", key: priority.key })}>
          <div className="grid min-w-0 flex-1 gap-0.5"><span className="truncate font-medium">{priority.title}</span>
            <span className="text-xs text-muted-foreground"><TaskKey k={priority.key} /> · {priority.priority} · {priority.agent ?? "unassigned"}</span></div>
          <Status value={priority.status} />
        </ListRow>
      </Section>
    )}
    <UnitGoals u={u} />
    {u.metrics.length > 0 && <Section title="All readings"><MetricRows u={u} limit={20} /></Section>}
    <div className="grid gap-10 lg:grid-cols-2">
      <Section title="Execution" description={agents.length ? `Agents on this work: ${agents.join(", ")}` : "No agent is assigned to open work here"}
        action={<span className="num">{f.work.length} open</span>}>
        {f.work.length ? f.work.map(t => (
          <ListRow key={t.key} onClick={() => open({ kind: "task", key: t.key })}>
            <TaskKey k={t.key} className="w-[72px] shrink-0" /><span className="min-w-0 flex-1 truncate">{t.title}</span><Status value={t.status} />
          </ListRow>)) : <Calm title="No open work">Nothing linked to this project is in motion.</Calm>}
      </Section>
      <Section title="Incidents">
        {f.incidents.length ? f.incidents.map(i => (
          <div key={i.key} className="flex items-center justify-between gap-3 border-b border-border/70 py-2 text-sm last:border-0">
            <span className="min-w-0"><span className="font-medium">{i.title}</span><span className="block text-xs text-muted-foreground"><span className="font-mono">{i.key}</span> · open {ago(i.started_at)}</span></span>
            <Status value={i.severity} />
          </div>)) : <Calm title="No open incidents" />}
      </Section>
    </div>
    <Section title="Recent activity">{events.length ? <EventList events={events} /> : <Calm title="No events linked yet">Events appear when the system records activity for this project.</Calm>}</Section>
  </>
}
