// Operations charts (shadcn chart = Recharts). Debugging detail: rendered only when opened.
import { cn } from "@/lib/utils"
import { Bar, BarChart, CartesianGrid, Pie, PieChart, XAxis, YAxis } from "recharts"
import type { DeskState } from "@/lib/api"
import { useDesk } from "@/state"
import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart"
import { Muted } from "@/components/desk"

const runConfig = {
  SUCCEEDED: { label: "Succeeded", color: "var(--chart-1)" },
  ESCALATED: { label: "Escalated", color: "var(--chart-2)" },
  FAILED: { label: "Failed", color: "var(--chart-3)" },
  STARTED: { label: "In flight", color: "var(--chart-4)" },
} satisfies ChartConfig

export function RunsChart({ series, height = "h-56" }: { series: NonNullable<DeskState["run_series"]>; height?: string }) {
  const rows = series.map(d => ({ ...d, label: new Date(d.day + "T00:00:00").toLocaleDateString([], { month: "short", day: "numeric" }) }))
  const total = series.reduce((n, d) => n + d.SUCCEEDED + d.FAILED + d.ESCALATED + d.STARTED, 0)
  const done = series.reduce((n, d) => n + d.SUCCEEDED + d.FAILED + d.ESCALATED, 0)
  const ok = series.reduce((n, d) => n + d.SUCCEEDED, 0)
  if (!total) return <Muted>No agent runs in the last 14 days.</Muted>
  return (
    <div className="grid gap-2">
      <p className="text-xs text-muted-foreground"><b className="text-foreground">{total}</b> runs · <b className="text-foreground">{done ? Math.round(ok / done * 100) : 0}%</b> success</p>
      <ChartContainer config={runConfig} className={cn("aspect-auto w-full", height)}>
        <BarChart data={rows} margin={{ left: -20, right: 4 }}>
          <CartesianGrid vertical={false} />
          <XAxis dataKey="label" tickLine={false} axisLine={false} minTickGap={16} />
          <YAxis allowDecimals={false} tickCount={3} tickLine={false} axisLine={false} width={40} />
          <ChartTooltip content={<ChartTooltipContent />} />
          <ChartLegend content={<ChartLegendContent />} />
          {(Object.keys(runConfig) as (keyof typeof runConfig)[]).map((k, i, all) => (
            <Bar key={k} dataKey={k} stackId="runs" fill={`var(--color-${k})`} radius={i === all.length - 1 ? [3, 3, 0, 0] : 0} maxBarSize={28} isAnimationActive={false} />
          ))}
        </BarChart>
      </ChartContainer>
    </div>
  )
}

export const COLUMNS: [string, string[]][] = [
  ["Needs attention", ["BLOCKED", "FAILED", "WAITING_APPROVAL"]],
  ["In progress", ["IN_PROGRESS"]],
  ["Ready", ["READY", "QUEUED"]],
  ["Waiting", ["WAITING_EXTERNAL"]],
  ["Review", ["REVIEW"]],
  ["Backlog", ["IDEA", "BACKLOG"]],
  ["Done", ["DONE", "CANCELLED"]],
]
const COLUMN_COLORS = ["var(--chart-3)", "var(--chart-4)", "oklch(0.72 0.12 220)", "var(--chart-2)", "var(--chart-5)", "var(--muted-foreground)", "var(--chart-1)"]

export function PipelineChart() {
  const { data } = useDesk()
  const parts = COLUMNS.map(([label, states], i) => ({ label, value: data!.tasks.filter(t => states.includes(t.status)).length, fill: COLUMN_COLORS[i] }))
  const total = parts.reduce((n, p) => n + p.value, 0)
  const config = Object.fromEntries(parts.map(p => [p.label, { label: p.label, color: p.fill }])) satisfies ChartConfig
  return (
    <div className="flex flex-wrap items-center gap-6">
      <ChartContainer config={config} className="aspect-square h-40">
        <PieChart>
          <ChartTooltip content={<ChartTooltipContent nameKey="label" hideLabel />} />
          <Pie data={parts.filter(p => p.value)} dataKey="value" nameKey="label" innerRadius={48} outerRadius={68} strokeWidth={2} paddingAngle={2} isAnimationActive={false} />
          <text x="50%" y="48%" textAnchor="middle" className="fill-foreground text-2xl font-semibold">{total}</text>
          <text x="50%" y="62%" textAnchor="middle" className="fill-muted-foreground text-[10px] tracking-wider">TASKS</text>
        </PieChart>
      </ChartContainer>
      <ul className="grid flex-1 gap-1.5 text-sm">
        {parts.map(p => (
          <li key={p.label} className="flex items-center gap-2">
            <i className="size-2.5 rounded-sm" style={{ background: p.fill }} /><span className="flex-1 text-muted-foreground">{p.label}</span><b className="tabular-nums">{p.value}</b>
          </li>
        ))}
      </ul>
    </div>
  )
}

const LOAD: [string, string[], string][] = [
  ["Running", ["IN_PROGRESS"], "var(--chart-4)"],
  ["Queued", ["READY", "QUEUED"], "oklch(0.72 0.12 220)"],
  ["Review", ["REVIEW", "WAITING_APPROVAL"], "var(--chart-2)"],
  ["Blocked", ["BLOCKED", "WAITING_EXTERNAL", "FAILED"], "var(--chart-3)"],
]

export function WorkloadChart() {
  const { data, open } = useDesk()
  const rows = data!.agents.map(a => {
    const mine = data!.tasks.filter(t => t.agent === a.key)
    return { a, done: mine.filter(t => t.status === "DONE").length, parts: LOAD.map(([label, s, color]) => ({ label, color, n: mine.filter(t => s.includes(t.status)).length })) }
  })
  const openOf = (r: (typeof rows)[number]) => r.parts.reduce((n, p) => n + p.n, 0)
  const max = Math.max(1, ...rows.map(openOf))
  if (!rows.length) return <Muted>No synced agents.</Muted>
  return (
    <div className="grid gap-2">
      {rows.map(r => (
        <button key={r.a.key} onClick={() => open({ kind: "agent", key: r.a.key })}
          className="grid grid-cols-[minmax(90px,140px)_1fr_auto] items-center gap-3 rounded-md px-1 py-1 text-left text-sm hover:bg-muted">
          <span className="truncate">{r.a.name}</span>
          <span className="flex h-2 overflow-hidden rounded-full bg-muted">
            {r.parts.filter(p => p.n).map(p => <i key={p.label} title={`${p.label}: ${p.n}`} style={{ width: `${p.n / max * 100}%`, background: p.color }} />)}
          </span>
          <span className="text-xs tabular-nums text-muted-foreground"><b className="text-foreground">{openOf(r)}</b> open · {r.done} done</span>
        </button>
      ))}
      <ul className="mt-1 flex flex-wrap gap-3 text-xs text-muted-foreground">
        {LOAD.map(([l, , c]) => <li key={l} className="flex items-center gap-1.5"><i className="size-2 rounded-sm" style={{ background: c }} />{l}</li>)}
      </ul>
    </div>
  )
}

export function OrgTree() {
  const { data, open } = useDesk()
  const agents = data!.agents
  if (!agents.some(a => a.reports_to === "founder")) return <Muted>No synced agent hierarchy.</Muted>
  const node = (parent: string, depth: number): React.ReactNode =>
    agents.filter(a => a.reports_to === parent).map(a => (
      <li key={a.key}>
        <button onClick={() => open({ kind: "agent", key: a.key })} style={{ paddingLeft: depth * 20 + 8 }}
          className="flex w-full items-center gap-2 rounded-md py-1.5 pr-2 text-left text-sm hover:bg-muted">
          <span className="text-muted-foreground">{depth ? "└" : "•"}</span><b className="font-medium">{a.name}</b>
          <span className="truncate text-xs text-muted-foreground">{a.role}</span>
          <span className="ml-auto text-xs" style={{ color: a.status === "RUNNING" ? "var(--info)" : undefined }}>{a.status.toLowerCase()}</span>
        </button>
        <ul>{node(a.key, depth + 1)}</ul>
      </li>
    ))
  return <ul className="grid"><li className="px-2 py-1 text-sm font-semibold">Founder <span className="font-normal text-muted-foreground">· final authority</span></li>{node("founder", 1)}</ul>
}
