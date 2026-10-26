// One reported business series over the last 30 days. Points are joined straight: a smoothed curve would draw
// movement between readings that nobody measured. A series with fewer than two readings is never drawn as a line.
import { useEffect, useState } from "react"
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts"
import { metricSeries, type Metric, type MetricSeries } from "@/lib/api"
import { fmtNum, metricName, when } from "@/lib/format"
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart"
import { Muted } from "@/components/desk"
import { cn } from "@/lib/utils"
import { TREND_MESSAGE, trendFailure, trendRefused, type TrendFailure } from "@/lib/trend-access"

type Load = { status: "loading" } | { status: "error"; why: TrendFailure } | { status: "ok"; series: MetricSeries }

export function MetricTrend({ m, onRefused }: { m: Metric; onRefused?: () => void }) {
  const [load, setLoad] = useState<Load>(() => trendRefused() ? { status: "error", why: "forbidden" } : { status: "loading" })
  useEffect(() => {
    if (trendRefused()) return  // the API already refused the Desk this session; another request would be refused too
    let live = true
    setLoad({ status: "loading" })
    metricSeries(m).then(
      series => { if (live) setLoad({ status: "ok", series }) },
      e => { if (!live) return; const why = trendFailure(e); setLoad({ status: "error", why }); if (why === "forbidden") onRefused?.() },
    )
    return () => { live = false }
  }, [m.source, m.subject, m.metric, m.window])
  return (
    <figure className="surface-panel grid min-w-0 gap-2 p-3.5">
      <figcaption className="flex min-w-0 items-baseline justify-between gap-2">
        <span className="min-w-0 truncate text-sm font-medium" title={m.label ?? m.metric}>{metricName(m)}</span>
        {m.window && <span className="num shrink-0 text-xs text-muted-foreground">{m.window}</span>}
      </figcaption>
      {load.status === "loading" && <Muted className="text-xs">Loading the last 30 days…</Muted>}
      {load.status === "error" && <Muted className="text-xs">{TREND_MESSAGE[load.why]}</Muted>}
      {load.status === "ok" && <TrendBody series={load.series} />}
    </figure>
  )
}

function TrendBody({ series }: { series: MetricSeries }) {
  if (series.points.length === 0) return <Muted className="text-xs">No readings in the last 30 days. Nothing is drawn.</Muted>
  if (series.points.length === 1) return <Muted className="text-xs">One reading so far, {when(series.points[0].observed_at)}. A trend needs two.</Muted>
  const rows = series.points.map(p => ({ t: new Date(p.observed_at).getTime(), value: p.value }))
  const name = series.metric.replaceAll("_", " ") + (series.unit ? ` · ${series.unit}` : "")
  const config = { value: { label: name, color: "var(--chart-1)" } } satisfies ChartConfig
  return (
    <ChartContainer config={config} className="aspect-auto h-36 w-full">
      <LineChart data={rows} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickLine={false} axisLine={false} minTickGap={28}
          tickFormatter={t => new Date(t).toLocaleDateString([], { month: "short", day: "numeric" })} />
        <YAxis domain={["auto", "auto"]} tickLine={false} axisLine={false} width={44} tickFormatter={v => fmtNum(Number(v))} />
        <ChartTooltip content={<ChartTooltipContent labelFormatter={(_, payload) => {
          // The header is when this reading was observed, not the series name again.
          const t = (payload?.[0] as { payload?: { t?: number } } | undefined)?.payload?.t
          return typeof t === "number" ? new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : null
        }} />} />
        <Line dataKey="value" type="linear" stroke="var(--color-value)" strokeWidth={2} dot={rows.length < 12} isAnimationActive={false} />
      </LineChart>
    </ChartContainer>
  )
}

/** The trend cards for a set of series: two columns when there are two or more. Once the API has refused the Desk
 *  history, one line says so instead of the same sentence on every card; the latest values stay where they are shown. */
export function TrendGrid({ metrics }: { metrics: Metric[] }) {
  const [refused, setRefused] = useState(trendRefused)
  if (!metrics.length) return null
  if (refused) return <p className="flex items-start gap-2 rounded-lg border border-dashed px-3.5 py-2.5 text-xs text-muted-foreground">
    <span aria-hidden className="mt-1 size-1.5 shrink-0 rounded-full bg-muted-foreground/50" />Trend history is not available to the Desk: the API does not allow this read. The latest reported values still stand.</p>
  return <div className={cn("grid gap-3", metrics.length > 1 && "sm:grid-cols-2")}>
    {metrics.map(m => <MetricTrend key={`${m.source}|${m.subject}|${m.metric}|${m.window}`} m={m} onRefused={() => setRefused(true)} />)}
  </div>
}
