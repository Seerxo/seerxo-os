import { ShieldCheck } from "lucide-react"
import { ago, humanize, when } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useData } from "@/state"
import { ClampText, EmptyState, Facts, PageHeader, FigureStrip } from "@/components/desk"

export function IncidentsPage() {
  const d = useData()
  const unit = (k: string | null) => d.portfolio?.find(u => u.key === k)?.name ?? k
  const sev = (s: string) => d.incidents.filter(i => i.severity === s).length
  return <>
    <PageHeader title="Incidents" description="Production problems, business impact first."
      />
    {(d.incidents.length > 0 || d.worker?.status === "HEALTHY") && <FigureStrip label="Incidents at a glance" items={[
      { label: "Open", value: String(d.incidents.length), tone: d.incidents.length ? "warning" : undefined, sub: d.incidents.length ? "recorded and unresolved" : "none recorded" },
      { label: "SEV0–1", value: String(sev("SEV0") + sev("SEV1")), tone: sev("SEV0") + sev("SEV1") ? "danger" : undefined, sub: "highest severity" },
      { label: "Lower severity", value: String(d.incidents.length - sev("SEV0") - sev("SEV1")), sub: "SEV2 and below" },
    ]} />}
    {d.incidents.length ? <div className={cn("grid grid-cols-1 gap-4", d.incidents.length > 1 ? "xl:grid-cols-2" : "max-w-3xl")}>{d.incidents.map(i => (
      <article key={i.key} className="grid min-w-0 content-start gap-4 rounded-xl border bg-card p-5 shadow-xs [overflow-wrap:anywhere]">
        <header className="grid gap-1">
          <div className="flex items-center justify-between gap-3">
            <span className={cn("text-[11px] font-semibold tracking-[0.06em] uppercase", ["SEV0", "SEV1"].includes(i.severity) ? "text-destructive" : "text-warning")}>{i.severity}</span>
            <span className="max-w-[60%] text-right font-mono text-xs text-muted-foreground">{i.key}</span>
          </div>
          <h3 className="text-[15px] leading-snug font-semibold">{i.title}</h3>
          <p className="text-sm text-muted-foreground">{unit(i.business_unit) ?? "No business unit linked"}{i.service && ` · ${i.service}`} · open {ago(i.started_at)}</p>
        </header>
        {i.description && <div className="grid gap-1"><span className="text-xs text-muted-foreground">Impact</span><p className="text-sm"><ClampText>{i.description}</ClampText></p></div>}
        <Facts items={[["Status", humanize(i.status)], ["Root cause", i.root_cause ? <ClampText key="root-cause">{i.root_cause}</ClampText> : "Not yet identified"], ["Detected by", i.detected_by],
          ["Started", when(i.started_at)], ["Founder notified", i.founder_notified ? "Yes" : "No"]]} />
      </article>))}</div>
      : d.worker?.status === "NO_DATA" || !d.worker
        ? <EmptyState icon={<ShieldCheck />} title="No incidents recorded, not verified">The watchdog has not reported, so an empty list is not proof that production is healthy.</EmptyState>
        : <EmptyState icon={<ShieldCheck />} title="No active incidents">Production problems recorded by the Company OS appear here.</EmptyState>}
  </>
}
