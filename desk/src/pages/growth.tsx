// Growth per business unit, only from series a source actually reports. A unit without a growth
// source says so; signups, activation or search data appear here once a reporter sends them.
import type { Metric, Unit } from "@/lib/api"
import { TrendGrid } from "@/components/trend"
import { ago, fmtNum, reading, when } from "@/lib/format"
import { dataState, dataSummary, isUnverified, staleSuffix } from "@/lib/freshness"
import { cn } from "@/lib/utils"
import { useData, useDesk } from "@/state"
import { Calm, Muted, PageHeader, Section, Status } from "@/components/desk"
import { Stat } from "@/pages/finance"
import { MetricRows } from "@/pages/projects"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"

const WINDOWS = ["24h", "72h", "7d"] as const

const isVideoViews = (m: Metric) =>
  m.metric === "views" && m.subject.startsWith("youtube:") && WINDOWS.includes(m.window as typeof WINDOWS[number])

/** One row per reported video: views per window, newest reading per series (portfolio order). */
function Videos({ u }: { u: Unit }) {
  const by = new Map<string, { subject: string; label: string; views: Partial<Record<string, Metric>> }>()
  for (const m of u.metrics) {
    if (!isVideoViews(m)) continue
    const row = by.get(m.subject) ?? { subject: m.subject, label: m.label || m.subject.slice("youtube:".length), views: {} }
    row.views[m.window] ??= m
    by.set(m.subject, row)
  }
  const rows = [...by.values()].sort((a, b) => (b.views["7d"]?.value ?? b.views["24h"]?.value ?? 0) - (a.views["7d"]?.value ?? a.views["24h"]?.value ?? 0))
  return (
    <>
    <div className="max-lg:hidden">
    <Table>
      <TableHeader><TableRow><TableHead>Video</TableHead>{WINDOWS.map(w => <TableHead key={w} className={cn("text-right", w === "72h" && "max-lg:hidden")}>{w}</TableHead>)}<TableHead className="text-right max-lg:hidden">Reported</TableHead></TableRow></TableHeader>
      <TableBody>
        {rows.map(r => {
          const latest = Object.values(r.views).map(m => m!.observed_at).sort().at(-1)!
          const staleCount = Object.values(r.views).filter(m => isUnverified(m)).length
          return (
            <TableRow key={r.subject}>
              <TableCell className="max-w-[420px] truncate font-medium max-sm:max-w-[160px]" title={r.label}>{r.label}</TableCell>
              {WINDOWS.map(w => <TableCell key={w} className={cn("num text-right", w === "72h" && "max-lg:hidden", isUnverified(r.views[w]) && "text-warning")} title={r.views[w] ? `${when(r.views[w]!.observed_at)}${staleSuffix(r.views[w])}` : undefined}>{r.views[w] ? fmtNum(r.views[w]!.value) : <Muted>—</Muted>}</TableCell>)}
              <TableCell className="num text-right text-xs text-muted-foreground max-lg:hidden" title={when(latest)}>{ago(latest)} ago{staleCount > 0 && <span className="block text-warning">{staleCount} not verified</span>}</TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
    </div>
    <ul className="grid grid-cols-1 gap-2 lg:hidden" aria-label="Video views by reporting window">
      {rows.map(r => {
        const latest = Object.values(r.views).map(m => m!.observed_at).sort().at(-1)!
        const staleCount = Object.values(r.views).filter(m => isUnverified(m)).length
        return <li key={r.subject} className="surface-panel grid gap-3 p-3.5">
          <div className="min-w-0">
            <h3 className="text-sm font-medium [overflow-wrap:anywhere]">{r.label}</h3>
            <p className="num mt-1 text-xs text-muted-foreground">Reported {ago(latest)} ago{staleCount > 0 && <span className="text-warning"> · {staleCount} not verified</span>}</p>
          </div>
          <dl className="grid grid-cols-3 divide-x divide-border/70 border-t border-border/70 pt-2">
            {WINDOWS.map(w => <div key={w} className="grid gap-1 px-2 first:pl-0 last:pr-0">
              <dt className="text-xs text-muted-foreground">{w}</dt>
              <dd className={cn("num text-sm font-semibold", isUnverified(r.views[w]) && "text-warning")}>{r.views[w] ? fmtNum(r.views[w]!.value) : <Muted>—</Muted>}</dd>
            </div>)}
          </dl>
        </li>
      })}
      {!rows.length && <li><Calm title="No video readings">No views are reported for the selected windows.</Calm></li>}
    </ul>
    </>
  )
}

function Subscriptions({ u }: { u: Unit }) {
  const active = reading(u, "active_subscriptions"), added = reading(u, "new_subscriptions", "mtd"), lost = reading(u, "cancellations", "mtd")
  const age = (m?: Metric) => (m ? `${ago(m.observed_at)} ago${staleSuffix(m)} · ${m.source}` : "not reported")
  return (
    <div className="grid gap-2">
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border bg-border sm:grid-cols-4">
        <Stat label="Active subscriptions" value={active ? fmtNum(active.value) : "—"} sub={age(active)} warn={isUnverified(active)} />
        <Stat label="New this month" value={added ? fmtNum(added.value) : "—"} sub={age(added)} warn={isUnverified(added)} />
        <Stat label="Cancelled this month" value={lost ? fmtNum(lost.value) : "—"} sub={age(lost)} warn={isUnverified(lost)} />
        {/* Net only when both sides are measured: one unknown side makes the net unknown. */}
        <Stat label="Net this month" value={added && lost ? `${added.value - lost.value > 0 ? "+" : ""}${fmtNum(added.value - lost.value)}` : "—"} sub={added && lost ? `new − cancelled${isUnverified(added) || isUnverified(lost) ? " · uses an unverified figure" : ""}` : "needs both figures"} warn={isUnverified(added) || isUnverified(lost)} />
      </div>
      <p className="text-xs text-muted-foreground">Subscription readings come from Polar. Missing readings are not treated as zero.</p>
    </div>
  )
}

function SeerxoFunnel({ u }: { u: Unit }) {
  const metric = (name: string, window = "") => reading(u, name, window, "seerxo-backend")
  const signups = metric("signups", "7d"), cohort = metric("activation_cohort", "7d")
  const activated = metric("activated", "7d"), rate = metric("activation_rate", "7d")
  const active = metric("active_users", "7d"), quotaHits = metric("quota_hits", "7d"), paid = metric("paid_users")
  const checkouts = metric("checkout_started", "7d")
  const generated = metric("new_signups_generated", "7d"), failedOnly = metric("new_signups_failed_only", "7d")
  // Not tried yet = signups - generated - failed-only: the same population, so it is exact; unknown if any part is.
  const notTried = signups && generated && failedOnly ? signups.value - generated.value - failedOnly.value : null
  const age = (m?: Metric) => m ? `${ago(m.observed_at)} ago${staleSuffix(m)} · ${m.source}` : "not reported"
  const value = (m?: Metric, percent = false) => m ? percent ? `${fmtNum(m.value * 100)}%` : fmtNum(m.value) : "—"
  return (
    <div className="grid gap-2">
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border bg-border sm:grid-cols-4">
        <Stat label="Signups · 7d" value={value(signups)} sub={age(signups)} warn={isUnverified(signups)} />
        <Stat label="Activation cohort · 7d" value={value(cohort)} sub={age(cohort)} warn={isUnverified(cohort)} />
        <Stat label="Activated · 7d" value={value(activated)} sub={age(activated)} warn={isUnverified(activated)} />
        <Stat label="Activation rate · 7d" value={value(rate, true)} sub={age(rate)} warn={isUnverified(rate)} />
        <Stat label="Active users · 7d" value={value(active)} sub={age(active)} warn={isUnverified(active)} />
        <Stat label="Quota hits · 7d" value={value(quotaHits)} sub={age(quotaHits)} warn={isUnverified(quotaHits)} />
        <Stat label="Checkout started · 7d" value={value(checkouts)} sub={age(checkouts)} warn={isUnverified(checkouts)} />
        <Stat label="Paid users" value={value(paid)} sub={age(paid)} warn={isUnverified(paid)} />
      </div>
      <div className="grid grid-cols-1 gap-px overflow-hidden rounded-lg border bg-border sm:grid-cols-3" aria-label="New signups, last 7 days">
        <Stat label="New signups · got a result" value={value(generated)} sub={age(generated)} warn={isUnverified(generated)} />
        <Stat label="New signups · only errors" value={value(failedOnly)} sub={age(failedOnly)} warn={isUnverified(failedOnly) || (failedOnly?.value ?? 0) > 0} />
        <Stat label="New signups · not tried yet" value={notTried === null ? "—" : fmtNum(notTried)}
          sub={notTried === null ? "needs all three readings" : "signups − result − only errors"} warn={isUnverified(signups) || isUnverified(generated) || isUnverified(failedOnly)} />
      </div>
      <p className="text-xs text-muted-foreground">Funnel readings are aggregate backend observations. Checkout started counts users who began paying, not purchases. Free → paid conversion is not derived until matching cohort data exists.</p>
    </div>
  )
}

/** Up to four trend charts per unit, for its reported series other than per-video views (those have their table). */
function Trends({ u }: { u: Unit }) {
  return <TrendGrid metrics={u.metrics.filter(m => !isVideoViews(m)).slice(0, 4)} />
}

export function GrowthPage() {
  const d = useData()
  const { go } = useDesk()
  const units = d.portfolio ?? []
  const measured = units.filter(u => u.metrics.length)
  const silent = units.filter(u => !u.metrics.length)
  return <>
    <PageHeader title="Growth" description="Reported series, per business unit." />
    {!Array.isArray(d.portfolio) && <Calm status={<Status value="UNKNOWN" />} title="Portfolio unavailable">This API version does not report it.</Calm>}
    {Array.isArray(d.portfolio) && !units.length && <Calm title="No business units recorded">Growth shows per unit once one is recorded and reports a series with its source. Nothing is estimated meanwhile.</Calm>}
    {measured.map(u => (
      <Section key={u.key} title={<button className="inline-flex min-h-6 items-center text-left hover:underline" onClick={() => go("project", u.key)}>{u.name}</button>}
        action={<span>{u.latest_at ? `newest reading ${ago(u.latest_at)} ago` : ""}{dataState(u).stale > 0 && <span className="text-warning"> · {dataSummary(u)}</span>}</span>}>
        {/* Known growth shapes get their own view; any other reported series falls back to the plain list. */}
        <div className="grid gap-4">
          <Trends u={u} />
          {u.key === "seerxo" ? <div className="grid gap-4"><SeerxoFunnel u={u} /><Subscriptions u={u} /></div>
            : u.metrics.some(isVideoViews) ? <Videos u={u} /> : <MetricRows u={u} limit={12} />}
        </div>
      </Section>
    ))}
    {silent.length > 0 && (
      <Section title="No growth source connected" count={silent.length}>
        {/* One line for all of them: the same sentence per unit was most of this page. */}
        <p className="text-sm"><span className="font-medium">{silent.map(u => u.name).join(", ")}</span>
          <Muted className="block text-xs">No metrics reported: nothing is shown until a source reports with provenance.</Muted></p>
      </Section>
    )}
  </>
}
