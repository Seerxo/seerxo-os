// Home, the founder's command center. It answers three questions and stops: does anything need my decision (the
// honest headline from lib/operating.ts with one action, then Needs you), what is the company doing (work in
// progress: task, owner, status, next step), and is it progressing safely (one line of health, one of business
// results). Above them, the one place to tell the CEO what to do. Everything else lives on its own page, one click
// away: the health checklist on System (Home's line summarises the same checks), agents and activity under Work, business numbers under Home's tabs.
import { ArrowRight, ChevronRight } from "lucide-react"
import type { Tone } from "@/lib/api"
import { ago, fmtNum, humanize } from "@/lib/format"
import { healthSummary, operating, systemChecks } from "@/lib/operating"
import { needAction, useOpenNeed } from "@/lib/needs"
import { GROUPS, groupOf, nextLine } from "@/lib/task-story"
import { cn } from "@/lib/utils"
import { useChanges, useData, useDesk, useNow, useStale, type Page } from "@/state"
import { ChangeMark, DOT, Dot, Panel, PanelEmpty, PanelRow, Status } from "@/components/desk"
import { Button } from "@/components/ui/button"
import { ApprovalDecision, AgentsSwitch, RetryDelivery } from "@/components/controls"
import { CommandComposer } from "@/components/command-composer"
import { retryEventId } from "@/lib/needs-pure"

const NEED_TONE: Record<string, Tone> = { critical: "danger", warning: "warning", info: "info" }
const GROUP_TONE = Object.fromEntries(GROUPS.map(g => [g.key, g.tone])) as Record<string, Tone>

const LinkButton = ({ children, onClick }: { children: React.ReactNode; onClick: () => void }) => (
  <button type="button" onClick={onClick} className="inline-flex min-h-7 items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground">
    {children}<ArrowRight className="size-3" />
  </button>
)

function Header({ op }: { op: ReturnType<typeof operating> }) {
  const { go, reload } = useDesk()
  const act = () => {
    const t = op.primary.target
    if (t === "brief") document.getElementById("ceo-command")?.focus()   // the composer right below
    else if (t.page === "refresh") void reload()
    else go(t.page as Page, t.key)
  }
  // One sentence, one action, and the kill switch.
  return (
    <header aria-labelledby="operating-state" className="flex min-w-0 flex-wrap items-center justify-between gap-x-8 gap-y-3">
      <h1 id="operating-state" className="flex min-w-0 items-center gap-3 text-[26px] leading-9 font-semibold tracking-[-0.045em] sm:text-[30px]">
        <Dot tone={op.tone} className="size-2.5" />{op.headline}
      </h1>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="lg" className="h-10 px-4" onClick={act}>{op.primary.label}<ArrowRight /></Button>
        <AgentsSwitch className="px-3.5" iconOnPhone />
      </div>
    </header>
  )
}

const SHOW = 3   // decisions shown here; Approvals has every one

function NeedsPreview() {
  const d = useData()
  const { go } = useDesk()
  const openNeed = useOpenNeed()
  const needs = d.needs_you
  if (!Array.isArray(needs) || !needs.length) return null
  return (
    <Panel title="Needs you" count={needs.length} action={<LinkButton onClick={() => go("approvals")}>See all</LinkButton>}>
      {needs.slice(0, SHOW).map((n, i) => {
        // An approval record is decided right here (same confirmation as Approvals); everything else opens where it is resolved.
        const a = n.kind === "APPROVAL" && n.target.page === "approvals" ? d.approvals.find(x => x.key === n.target.key) : undefined
        const retry = retryEventId(n)
        const row = <PanelRow key={`${n.kind}-${i}`} onClick={() => openNeed(n)} lead={<Dot tone={NEED_TONE[n.level] ?? "info"} />} className="items-center py-2.5"
          title={n.title} trail={a || retry !== null ? undefined : <><span className="font-medium text-foreground">{needAction(n)}</span><ChevronRight className="size-4 text-muted-foreground/50 max-sm:hidden" /></>} />
        if (!a && retry === null) return row
        return (
          <div key={`${n.kind}-${i}`} className="flex items-center gap-x-2 pr-3 max-sm:flex-wrap max-sm:pb-2.5 max-sm:pl-7">
            <div className="min-w-0 flex-1 max-sm:-ml-7 max-sm:basis-[calc(100%+1.75rem)]">{row}</div>
            {a ? <ApprovalDecision a={a} compact /> : <RetryDelivery eventId={retry!} compact />}
          </div>
        )
      })}
    </Panel>
  )
}

const ORDER = ["attention", "running", "review", "next", "waiting"]

/** What the company is doing: each active task with its owner, its real status and what happens next. */
function WorkInProgress() {
  const d = useData()
  const { open, go } = useDesk()
  const changes = useChanges()
  const paused = d.control.agents_paused
  const active = d.tasks.filter(t => ORDER.includes(groupOf(t.status)))
    .sort((a, b) => ORDER.indexOf(groupOf(a.status)) - ORDER.indexOf(groupOf(b.status)) || a.priority.localeCompare(b.priority) || b.updated_at.localeCompare(a.updated_at))
  return (
    <Panel title="Work in progress" count={active.length || undefined} action={<LinkButton onClick={() => go("tasks")}>All work</LinkButton>}>
      {active.length ? active.slice(0, 5).map(t => {
        const g = groupOf(t.status)
        const c = changes[`task:${t.key}`]
        return (
          <PanelRow key={t.key} onClick={() => open({ kind: "task", key: t.key })} label={`${t.title}, ${humanize(t.status)}`} className={cn("py-2.5", c && "desk-flash")}
            lead={<Dot tone={GROUP_TONE[g]} live={t.status === "IN_PROGRESS"} />} title={t.title}
            meta={<>{t.agent ? <span className="font-medium text-foreground/80">{t.agent}</span> : "Unassigned"} · {nextLine(t, { paused })}</>}
            trail={<><ChangeMark c={c} className="max-sm:hidden" /><Status value={t.status} tone={GROUP_TONE[g]} /></>} />
        )
      }) : <PanelEmpty title="Nothing in progress">Nothing is queued, running or waiting. Tell the CEO what to do above.</PanelEmpty>}
    </Panel>
  )
}

/** Health and business in two lines. Details stay on System and Finance; what is not reported says so, never zero. */
function AtAGlance() {
  const d = useData()
  const { go } = useDesk()
  const now = useNow("minute")
  const health = healthSummary(systemChecks(d, now))
  const r = d.revenue
  const mrr = r ? (r.mrr ?? r.month) : null
  const line = "flex min-w-0 items-center gap-2.5 rounded-lg px-3 py-2.5 text-left text-[13px] transition-colors hover:bg-muted/50 focus-visible:outline-2 focus-visible:outline-ring"
  return (
    <section aria-label="At a glance" className="grid gap-1 border-t border-border/70 pt-3 sm:grid-cols-2">
      <button type="button" onClick={() => go("health")} className={line} aria-label={`Company health: ${health.text}. Open System status`}>
        <span aria-hidden className={cn("size-1.5 shrink-0 rounded-full", DOT[health.tone])} />
        <span className="font-medium">Company health</span>
        <span className="min-w-0 flex-1 truncate text-muted-foreground">{health.text}</span>
        <ChevronRight className="size-4 shrink-0 text-muted-foreground/50" />
      </button>
      <button type="button" onClick={() => go("finance")} className={line}>
        <span aria-hidden className={cn("size-1.5 shrink-0 rounded-full", DOT[r?.freshness === "STALE" ? "warning" : mrr != null ? "success" : "neutral"])} />
        <span className="font-medium">{r?.mrr != null ? "MRR" : "Revenue this month"}</span>
        <span className="min-w-0 flex-1 truncate text-muted-foreground">{mrr != null
          ? <><b className="num font-semibold text-foreground">${fmtNum(mrr)}</b> · {r!.source} · {ago(r!.observed_at)} ago{r!.freshness === "STALE" ? " · stale" : ""}</>
          : r === undefined ? "Not reported by this API" : "No verified revenue source"}</span>
        <ChevronRight className="size-4 shrink-0 text-muted-foreground/50" />
      </button>
    </section>
  )
}

export function Overview() {
  const d = useData()
  const stale = useStale()
  const now = useNow("minute")
  const op = operating(d, { stale, now })
  const needs = Array.isArray(d.needs_you) && d.needs_you.length > 0
  return (
    <div className="grid min-w-0 grid-cols-1 gap-6 lg:gap-8">
      <Header op={op} />
      <CommandComposer />
      <div className={cn("grid min-w-0 gap-6", needs && "lg:grid-cols-2")}>
        {needs && <NeedsPreview />}
        <WorkInProgress />
      </div>
      <AtAGlance />
    </div>
  )
}
