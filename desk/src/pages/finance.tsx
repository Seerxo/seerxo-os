// Money with provenance only. A unit with no revenue source says so; a cost with no recorded
// amount shows its class, never an invented figure. Data: /desk/state (revenue, portfolio, costs).
import { pageTitle } from "@/lib/nav"
import type { ReactNode } from "react"
import { ago, humanize, reading, REVENUE_SOURCE, usd, when } from "@/lib/format"
import { freshLabel, isUnverified, staleSuffix } from "@/lib/freshness"
import { cn } from "@/lib/utils"
import { useData, useDesk } from "@/state"
import { Calm, Expandable, Muted, PageHeader, Section, Status } from "@/components/desk"
import { DataRows } from "@/components/data-rows"


const money = (v: number | null | undefined) => (v == null ? "—" : usd(v))

export function Stat({ label, value, sub, warn }: { label: string; value: ReactNode; sub?: ReactNode; warn?: boolean }) {
  return (
    <div className="grid content-start gap-1 bg-card px-4 py-3.5">
      <span className="eyebrow">{label}</span>
      <span className={cn("num text-2xl leading-8 font-semibold tracking-[-0.035em]", warn && "text-warning")}>{value}</span>
      {sub && <span className="truncate text-xs text-muted-foreground">{sub}</span>}
    </div>
  )
}

export function Revenue() {
  const d = useData()
  const r = d.revenue
  if (r === undefined) return <Calm status={<Status value="UNKNOWN" />} title="Revenue unavailable">This API version does not report it.</Calm>
  if (r === null) return <Calm title="No verified revenue source">No value is shown until a source reports with provenance.</Calm>
  const seerxo = d.portfolio?.find(u => u.key === "seerxo")
  const seen = (metric: string, window = "") => reading(seerxo, metric, window)
  const count = (metric: string, window = "") => seen(metric, window)?.value
  // Each figure's own age and freshness: series refresh independently, so one shared age could mislead.
  const age = (metric: string, window = "") => {
    const x = seen(metric, window)
    return x ? <span title={when(x.observed_at)}>{ago(x.observed_at)} ago{staleSuffix(x)}</span> : undefined
  }
  const countStale = (metric: string, window = "") => isUnverified(seen(metric, window))
  const stale = (key: "today" | "month" | "mrr" | "total") => r.series?.[key]?.freshness === "STALE" ||
    (r.series === undefined && r.freshness === "STALE")
  const anyStale = r.freshness === "STALE"
  return (
    <div className="grid gap-2">
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border bg-border sm:grid-cols-4">
        <Stat label="All time" value={money(r.total)} sub={age("revenue", "all_time")} warn={stale("total")} />
        <Stat label="This month" value={money(r.month)} sub={age("revenue", "mtd")} warn={stale("month")} />
        <Stat label="Today" value={money(r.today)} sub={age("revenue", "today")} warn={stale("today")} />
        <Stat label="MRR" value={money(r.mrr)} sub={age("mrr")} warn={stale("mrr")} />
        <Stat label="Active subscriptions" value={count("active_subscriptions") ?? "—"} sub={age("active_subscriptions")} warn={countStale("active_subscriptions")} />
        <Stat label="New this month" value={count("new_subscriptions", "mtd") ?? "—"} sub={age("new_subscriptions", "mtd")} warn={countStale("new_subscriptions", "mtd")} />
        <Stat label="Cancelled this month" value={count("cancellations", "mtd") ?? "—"} sub={age("cancellations", "mtd")} warn={countStale("cancellations", "mtd")} />
        <Stat label="Source" value={<span className="text-sm font-medium">{r.source}</span>}
          sub={<span className={cn(anyStale && "text-warning")}>{anyStale && "partly stale · "}read-only · hourly</span>} />
      </div>
      <p className="text-xs text-muted-foreground">Seerxo via Polar, read-only, hourly. Amounts in {r.unit ?? "the source currency"}. "—" means the source did not report that value.</p>
    </div>
  )
}

/** Revenue per unit where a source exists; otherwise what is known about the unit. */
function ByUnit() {
  const d = useData()
  const { go } = useDesk()
  const units = d.portfolio ?? []
  if (!units.length) return <Calm title="No business units recorded">Units appear once the company records them. Nothing is estimated while none are recorded.</Calm>
  const money = (m: ReturnType<typeof reading>, empty: ReactNode) => !m ? empty : (
    <span className={cn(isUnverified(m) && "text-warning")}>{usd(m.value)}
      {isUnverified(m) && <span className="block text-xs">{freshLabel(m)} · {ago(m.observed_at)} ago</span>}</span>)
  // Active units and any unit with a revenue reading stay in view; inactive units that report nothing fold below.
  const quiet = (u: (typeof units)[number]) => u.status !== "ACTIVE" && !reading(u, "revenue", "all_time") && !reading(u, "mrr")
  const table = (rows: typeof units, label: string) => <DataRows label={label} rows={rows} rowKey={u => u.key} titleHead="Business unit"
    title={u => u.name} onRowClick={u => go("project", u.key)}
    cols={[
      { head: "Status", cell: u => <Status value={u.status} /> },
      { head: "Revenue (all time)", className: "num text-right", cell: u =>
        money(reading(u, "revenue", "all_time"), <Muted>{u.metrics.length ? "No revenue reported" : "Nothing reported"}</Muted>) },
      { head: "MRR", className: "num text-right", cell: u => money(reading(u, "mrr"), <Muted>—</Muted>) },
      { head: "Signal", className: "text-sm", cell: u => {
        const mrr = reading(u, "mrr"), known = reading(u, "revenue", "all_time") || mrr
        return known ? <span>{(mrr?.value ?? 0) > 0 ? "Recurring revenue" : "Revenue source connected"} <Muted>· {REVENUE_SOURCE}</Muted></span>
          : <Muted>{u.status === "ACTIVE" ? "Unknown: no revenue source connected" : humanize(u.status)}</Muted>
      } },
    ]} />
  const rest = units.filter(quiet)
  return <div className="grid min-w-0 gap-3">
    {table(units.filter(u => !quiet(u)), "Revenue by business unit")}
    {rest.length > 0 && <Expandable summary={<span className="text-[13px]"><span className="font-medium">{rest.length} more {rest.length === 1 ? "unit" : "units"}</span>
      <Muted> · {[...new Set(rest.map(u => humanize(u.status).toLowerCase()))].join(", ")} · nothing reported</Muted></span>}>
      {table(rest, "Inactive business units")}</Expandable>}
  </div>
}

function Costs() {
  const d = useData()
  const c = d.costs
  if (!c) return <Calm status={<Status value="UNKNOWN" />} title="Cost policy unavailable">This API version does not report it.</Calm>
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
        <Muted>New spend created by agents</Muted>
        <b className={cn("num text-base", c.new_autonomous_spend_usd > 0 && "text-destructive")}>{usd(c.new_autonomous_spend_usd)}</b>
        <Muted className="text-xs">must stay $0.00: agents never create, raise or repurpose a cost</Muted>
      </div>
      <DataRows label="Cost items" rows={c.items} rowKey={i => i.key} titleHead="Item" title={i => humanize(i.key)}
        cols={[
          { head: "Class", className: "text-sm", cell: i => humanize(i.class) },
          { head: "Note", className: "text-sm whitespace-normal text-muted-foreground", cell: i => i.note ?? i.purpose ?? "—" },
        ]} />
      <p className="text-xs text-muted-foreground">Amounts are not recorded in Company OS, so no profit figure is shown. Classes come from agents/cost_policy.yaml.</p>
    </div>
  )
}

export function FinancePage() {
  return <>
    <PageHeader title={pageTitle("finance")} description="Revenue and costs, each with its source." />
    <Section title="Revenue"><Revenue /></Section>
    <Section title="By business unit"><ByUnit /></Section>
    <Section title="Costs"><Costs /></Section>
  </>
}
