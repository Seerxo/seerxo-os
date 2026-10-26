// The founder's one decision inbox. First the approvals the Desk can decide here (POST /approvals/{key}); then every
// other thing that needs the founder (needs_you), each with the place it is resolved. Opening never approves; HIGH and
// CRITICAL grants need the verified founder session (the API refuses others) and a typed confirmation.
import { CircleCheck, ShieldAlert } from "lucide-react"
import type { Approval, Need, Tone } from "@/lib/api"
import { ago, humanize, humanizeTokens, when } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useData, useDesk } from "@/state"
import { Dot, EmptyState, Facts, PageHeader, Panel, PanelRow, FigureStrip, Status } from "@/components/desk"
import { ApprovalDecision, HIGH_RISK, RetryDelivery } from "@/components/controls"
import { needAction, useOpenNeed } from "@/lib/needs"
import { retryEventId } from "@/lib/needs-pure"
import { approvalSummary } from "@/lib/approvals"
import { Button } from "@/components/ui/button"

const KIND: Record<string, string> = {
  SEV0: "SEV0 incident", SECURITY: "Security", SEV1: "SEV1 incident", STOPPED: "Stopped", APPROVAL: "Decision",
  BLOCKED_P0: "Blocked P0 work", BLOCKED_P1: "Blocked P1 work", PROVIDER: "Agent provider", OTHER: "Note",
}
const LEVEL: Record<string, Tone> = { critical: "danger", warning: "warning", info: "info" }

// Context first (what, why, effect, risk, rollback); the decision after it.
function ApprovalCard({ a }: { a: Approval }) {
  const high = HIGH_RISK.includes(a.risk)
  return (
    <article className={cn("panel grid content-start gap-5 p-5", high && "border-destructive/35")} aria-labelledby={`apr-${a.key}`}>
      <header className="grid gap-1.5">
        <div className="flex items-center justify-between gap-3 text-xs">
          <span className="font-mono text-muted-foreground">{a.key} · requested by {a.requested_by} · {ago(a.created_at)} ago</span>
          <Status value={a.risk} label={`${humanize(a.risk)} risk`} pill />
        </div>
        <h3 id={`apr-${a.key}`} className="text-[16px] leading-snug font-semibold tracking-tight">{a.action}</h3>
      </header>
      <Facts items={[["Why", a.reason], ["If approved", a.expected_effect], ["Rollback", a.rollback || "No rollback recorded"]]} />
      {high && <p className="flex items-start gap-2 rounded-lg bg-warning/[0.07] px-3 py-2 text-xs text-warning">
        <ShieldAlert className="mt-px size-3.5 shrink-0" />Only your verified founder sign-in can grant a {a.risk.toLowerCase()}-risk approval. You type its key to confirm.</p>}
      <footer className="grid gap-3 border-t border-border/70 pt-4 sm:flex sm:items-center sm:justify-end">
        <span className="text-xs text-muted-foreground sm:mr-auto">Your decision is recorded in the audit log; it does not run the action itself.</span>
        <ApprovalDecision a={a} />
      </footer>
    </article>
  )
}

function NeedRow({ n }: { n: Need }) {
  const openNeed = useOpenNeed()
  const retry = retryEventId(n)
  const row = (
    <PanelRow onClick={() => openNeed(n)} lead={<Dot tone={LEVEL[n.level] ?? "info"} />}
      title={n.title}
      meta={<span className="grid gap-0.5">
        <span className="text-foreground/80">{n.why}</span>
        <span className="text-xs"><span className={cn("font-medium", n.level === "critical" ? "text-destructive" : "text-muted-foreground")}>{KIND[n.kind] ?? n.kind}</span>
          {n.since && <span title={when(n.since)}> · {ago(n.since)}</span>}
          {n.evidence && <span className="font-mono text-[11.5px]"> · {humanizeTokens(n.evidence)}</span>}</span>
      </span>}
      trail={<span className="hidden h-8 items-center rounded-md border px-2.5 text-xs font-medium text-foreground sm:inline-flex">{needAction(n)}</span>} />
  )
  if (retry === null) return row
  // The retry sits beside the row (a button inside the row's button would be invalid and unreachable).
  return <div className="flex items-center gap-2 pr-3 max-sm:flex-wrap max-sm:pb-3 max-sm:pl-11"><div className="min-w-0 flex-1 max-sm:-ml-11 max-sm:basis-[calc(100%+2.75rem)]">{row}</div><RetryDelivery eventId={retry} compact /></div>
}

export function ApprovalsPage() {
  const d = useData()
  const { go } = useDesk()
  const needs = d.needs_you
  // An approval row in needs_you duplicates a card above; keep only the ones resolved elsewhere (e.g. a task decision).
  const others = Array.isArray(needs) ? needs.filter(n => !(n.kind === "APPROVAL" && n.target.page === "approvals")) : null
  const sum = approvalSummary(d)
  return <>
    <PageHeader title="Approvals" description="Your decisions, most urgent first. Opening an item never approves it."
      />
    <FigureStrip label="Decisions at a glance" items={sum.figures.map(f => f.label === "Tasks awaiting approval" && sum.waitingTasks
      ? { ...f, onClick: () => go("tasks", "attention") } : f)} />
    {sum.clear
      ? <EmptyState icon={<CircleCheck />} title="Nothing needs you">The company can continue on its own. New approvals, incidents and stuck high-priority work land here first.</EmptyState>
      : <>
        <section className="grid gap-3" aria-label="Approvals waiting">
          <h2 className="px-0.5 text-[15px] font-semibold tracking-tight">Approval records <span className="num text-[13px] font-normal text-muted-foreground">{d.approvals.length}</span></h2>
          {d.approvals.length ? <div className="grid gap-4 xl:grid-cols-2">{d.approvals.map(a => <ApprovalCard key={a.key} a={a} />)}</div>
            : sum.waitingTasks
              ? <div className="panel flex flex-wrap items-center justify-between gap-3 px-4 py-4 text-[13px]">
                  <span className="text-muted-foreground"><span className="font-medium text-foreground">No approval record, but {sum.waitingTasks === 1 ? "1 task is" : `${sum.waitingTasks} tasks are`} waiting for approval.</span> The decision is made on the task itself.</span>
                  <Button size="sm" variant="outline" onClick={() => go("tasks", "attention")}>Open in Work</Button>
                </div>
              : <p className="panel px-4 py-4 text-[13px] text-muted-foreground">No approval records. High-impact work stops here for your decision.</p>}
        </section>
        {others === null
          ? <Panel title="Also needs you"><PanelRow title="Attention queue not reported" meta="This API version does not report it. Check incidents and system status directly." lead={<Dot tone="warning" />} /></Panel>
          : others.length > 0 && <Panel title="Also needs you" count={others.length} description="Resolved where each item points; nothing here is decided by opening it">
              {others.map((n, i) => <NeedRow key={`${n.kind}-${n.title}-${i}`} n={n} />)}
            </Panel>}
      </>}
  </>
}
