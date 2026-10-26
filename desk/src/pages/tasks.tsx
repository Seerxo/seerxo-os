// Work: every task grouped by what it means for the founder (lib/task-story.ts), each row saying what happens next and,
// for stopped work, the recorded reason. Filters are routes (#/tasks/attention) so Overview links land on the right list.
import { useRef, useState, type ComponentProps } from "react"
import { ChevronRight, Search, Sparkles } from "lucide-react"
import { deliveryView } from "@/lib/delivery"
import { pageTitle } from "@/lib/nav"
import { DeliveryTrack } from "@/components/delivery"
import { ago } from "@/lib/format"
import type { TaskRow } from "@/lib/api"
import { attentionBreakdown, GROUPS, groupOf, nextLine, oldestUpdate, type Group } from "@/lib/task-story"
import { arrived, isOpen, pinned, signals } from "@/lib/work-groups"
import { useReasons } from "@/lib/use-reasons"
import { useChange, useChanges, useData, useDesk, useDraft } from "@/state"
import { homeAfterCreate } from "@/lib/draft"
import { ChangeMark, Dot, PageHeader, PanelEmpty, PanelRow, Status, Tag, priorityTone } from "@/components/desk"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { useReturnFocus } from "@/lib/return-focus"

const NONE = "none"  // Radix Select cannot hold "": the sentinel means "not attached"

function Pick({ label, value, onChange, options, none }: {
  label: string; value: string; onChange: (v: string) => void; options: [string, string][]; none?: string
}) {
  return (
    <label className="grid gap-1.5 text-[13px] font-medium">{label}
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger className="w-full font-normal"><SelectValue /></SelectTrigger>
        <SelectContent>
          {none && <SelectItem value={NONE}>{none}</SelectItem>}
          {options.map(([v, l]) => <SelectItem key={v} value={v}>{l}</SelectItem>)}
        </SelectContent>
      </Select>
    </label>
  )
}

/** The one create flow. By default a brief for the CEO (one field); "More options" makes it a full work contract (§35).
 *  Either way it is POST /desk/command: 201 means a READY task was recorded, nothing ran. The backend validates every
 *  reference; autonomy and repository writes are never offered here (Stage C admission decides them). */
export function CreateTask({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const returnFocus = useReturnFocus()
  const d = useData()
  const { command, seed, sending: busy, startSend, settleSend } = useDesk()
  const { setDraft } = useDraft()
  const blank = { title: "", text: "", objective: "", success: "", unit: NONE, project: NONE, goal: NONE,
                  agent: "ceo", priority: "P2", risk: "LOW" }
  // Opened from Home's "More options", the dialog starts from the composer's draft (App remounts it per seed).
  const [f, setF] = useState(() => ({ ...blank, text: seed.text }))
  const set = (k: keyof typeof blank) => (v: string) => setF(x => ({
    ...x, [k]: v,
    // Changing the unit or project clears what no longer belongs to it.
    ...(k === "unit" ? { project: NONE, goal: NONE } : k === "project" ? { goal: NONE } : {}),
  }))
  const projects = (d.catalog?.projects ?? []).filter(p => f.unit === NONE || p.business_unit === f.unit)
  const goals = (d.catalog?.goals ?? []).filter(g => f.project === NONE ? f.unit === NONE || projects.some(p => p.key === g.project) : g.project === f.project)
  const opt = (v: string) => (v === NONE ? undefined : v)
  const submit: ComponentProps<"form">["onSubmit"] = async e => {
    e.preventDefault()
    // One request per open dialog: while it is in flight the dialog can neither close nor reopen (lib/draft.ts), and
    // its result settles only the open that started it.
    const key = startSend()
    if (key === null) return
    const body = {
      text: f.text, agent: f.agent, priority: f.priority, risk_level: f.risk,
      title: f.title.trim() || undefined, objective: f.objective.trim() || undefined,
      success_criteria: f.success.trim() || undefined, business_unit: opt(f.unit), project: opt(f.project),
      goal: f.goal === NONE ? undefined : Number(f.goal),
    }
    const ok = await command(body)
    settleSend(key, !!ok)   // closes this dialog on success; a stale key changes nothing
    if (ok) {
      // The fields were read-only while recording, so `f` is exactly what was sent: clearing it loses nothing unsent.
      setF(blank)
      setDraft(cur => homeAfterCreate(cur, seed.text, body.text))   // only the part actually sent leaves Home
    }
  }
  const agentName = d.agents.find(a => a.key === f.agent)?.name ?? f.agent
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-xl" onCloseAutoFocus={returnFocus} showCloseButton={!busy}
        onEscapeKeyDown={e => { if (busy) e.preventDefault() }} onInteractOutside={e => { if (busy) e.preventDefault() }}>
        <form onSubmit={submit} className="grid gap-5">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2"><Sparkles className="size-4 text-brand" />New task</DialogTitle>
            <DialogDescription>Describe the outcome you want. It is recorded as a READY task for {agentName}; nothing runs until an agent picks it up.</DialogDescription>
          </DialogHeader>
          {/* Read-only while the request is in flight: nothing can be typed that the request will not carry. */}
          <fieldset disabled={busy} className="grid min-w-0 gap-5">
          <label className="grid gap-1.5 text-[13px] font-medium">What should happen?
            <Textarea value={f.text} onChange={e => set("text")(e.target.value)} required minLength={3} maxLength={2000} rows={4} autoFocus
              placeholder="e.g. Find why trial users abandon Seerxo checkout and propose two fixes" className="font-normal" />
          </label>
          <Collapsible>
            <CollapsibleTrigger className="group flex min-h-9 items-center gap-1.5 text-[13px] text-muted-foreground hover:text-foreground">
              <ChevronRight className="size-4 shrink-0 transition-transform group-data-[state=open]:rotate-90" /><span className="shrink-0">More options</span>
              <span className="min-w-0 text-left text-xs">· owner, project, priority, success criteria</span>
            </CollapsibleTrigger>
            <CollapsibleContent className="grid gap-4 pt-3">
              <label className="grid gap-1.5 text-[13px] font-medium">Title
                <Input value={f.title} onChange={e => set("title")(e.target.value)} minLength={3} maxLength={200} placeholder="Defaults to the first line of the request" className="font-normal" />
              </label>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="grid gap-1.5 text-[13px] font-medium">Objective
                  <Textarea value={f.objective} onChange={e => set("objective")(e.target.value)} maxLength={2000} rows={2} placeholder="The outcome, not the steps" className="font-normal" />
                </label>
                <label className="grid gap-1.5 text-[13px] font-medium">Success criteria
                  <Textarea value={f.success} onChange={e => set("success")(e.target.value)} minLength={3} maxLength={2000} rows={2} placeholder="How we will know it is done" className="font-normal" />
                </label>
              </div>
              <div className="grid gap-3 sm:grid-cols-3">
                <Pick label="Business unit" value={f.unit} onChange={set("unit")} none="Company-wide" options={(d.portfolio ?? []).map(u => [u.key, u.name])} />
                <Pick label="Project" value={f.project} onChange={set("project")} none="None" options={projects.map(p => [p.key, p.name])} />
                <Pick label="Goal" value={f.goal} onChange={set("goal")} none="None" options={goals.map(g => [String(g.id), g.title])} />
              </div>
              <div className="grid gap-3 sm:grid-cols-3">
                <Pick label="Agent" value={f.agent} onChange={set("agent")} options={d.agents.filter(a => a.enabled !== false || a.key === "ceo").map(a => [a.key, a.name])} />
                <Pick label="Priority" value={f.priority} onChange={set("priority")} options={["P0", "P1", "P2", "P3", "P4"].map(p => [p, p])} />
                <Pick label="Risk" value={f.risk} onChange={set("risk")} options={["LOW", "MEDIUM", "HIGH"].map(r => [r, r])} />
              </div>
            </CollapsibleContent>
          </Collapsible>
          </fieldset>
          <DialogFooter>
            <Button type="submit" disabled={busy || f.text.trim().length < 3} className="h-10 px-4">
              {busy ? "Recording…" : f.agent === "ceo" ? "Send to the CEO" : "Create READY task"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

type Filter = "active" | Group | "all"
const ACTIVE: Group[] = ["attention", "running", "review", "next", "waiting"]
const FILTERS: { key: Filter; label: string }[] = [
  { key: "active", label: "Active" }, ...GROUPS.map(g => ({ key: g.key as Filter, label: g.label })), { key: "all", label: "All" },
]
const asFilter = (k: string | undefined): Filter => (FILTERS.some(f => f.key === k) ? k as Filter : "active")

function TaskLine({ t, reason, paused }: { t: TaskRow; reason?: string; paused: boolean }) {
  const { open } = useDesk()
  // The task's own code mission, if it has one: the link from work to a technical result (PR, CI, merge).
  const mission = useData().improvements?.find(i => i.task === t.key)
  const mv = mission && deliveryView(mission)
  const g = groupOf(t.status)
  const tone = GROUPS.find(x => x.key === g)!.tone
  const changed = useChange("task", t.key)
  return (
    <PanelRow onClick={() => open({ kind: "task", key: t.key })} label={`${t.title}: ${t.status}`} className={changed && "desk-flash"}
      lead={<Dot tone={tone} live={t.status === "IN_PROGRESS"} />}
      title={<span className="flex min-w-0 items-center gap-2">
        <span className="truncate">{t.title}</span>
        <ChangeMark c={changed} />
        {t.priority !== "P2" && <Tag tone={priorityTone(t.priority)}>{t.priority}</Tag>}
        {t.risk !== "LOW" && <Tag tone={t.risk === "HIGH" || t.risk === "CRITICAL" ? "warning" : "neutral"} className="max-sm:hidden">{t.risk} risk</Tag>}
      </span>}
      meta={<><span className="font-mono text-xs">{t.key}</span> · {nextLine(t, { reason, paused })}
        {mv && <span className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <span className="font-medium text-foreground/80">Code mission</span><Status value={mission!.stage} label={mv.label} tone={mv.tone} />
          {mv.evidence.length > 0 && <span>{mv.evidence.join(" · ")}</span>}<DeliveryTrack v={mv} className="w-28 max-sm:hidden" /></span>}</>}
      tone={g === "attention" ? "danger" : undefined}
      trail={<>
        <span className="max-lg:hidden">{[t.agent ?? "unassigned", t.business_unit].filter(Boolean).join(" · ")}</span>
        {/* Where the task came from: who opened it, and whether automatically. Absent on older APIs. */}
        {t.origin && <span className="max-w-[9rem] truncate max-xl:hidden" title={t.origin.reason ?? t.origin.trigger ?? undefined}>
          {t.origin.automatic ? "auto" : "from"} {t.origin.by}</span>}
        <span className="num max-sm:hidden">{ago(t.updated_at)}</span>
        <Status value={t.status} tone={tone} />
        <ChevronRight className="size-4 text-muted-foreground/40 max-sm:hidden" />
      </>} />
  )
}

export function TasksPage() {
  const d = useData()
  const { route, go, compose } = useDesk()
  const filter = asFilter(route.key)
  const [query, setQuery] = useState("")
  const [unit, setUnit] = useState("all")
  const [manual, setManual] = useState<Partial<Record<Group, boolean>>>({})
  // Keys present when the page first had data: a task that arrives later is announced as "+N new" on its group.
  const seen = useRef<Set<string> | null>(null)
  if (seen.current === null && d.tasks.length) seen.current = new Set(d.tasks.map(t => t.key))
  const reasons = useReasons(d)
  const paused = d.control.agents_paused
  const q = query.trim().toLocaleLowerCase()
  const matches = (t: TaskRow) => (unit === "all" || t.business_unit === unit) &&
    (!q || [t.key, t.title, t.agent, t.business_unit, t.origin?.trigger, t.origin?.by].filter(Boolean).join(" ").toLocaleLowerCase().includes(q))
  const inFilter = (t: TaskRow, f: Filter) => f === "all" || (f === "active" ? ACTIVE.includes(groupOf(t.status)) : groupOf(t.status) === f)
  const count = (f: Filter) => d.tasks.filter(t => inFilter(t, f)).length
  const tasks = d.tasks.filter(t => inFilter(t, filter) && matches(t))
    .sort((a, b) => a.priority.localeCompare(b.priority) || b.updated_at.localeCompare(a.updated_at))
  const groups = GROUPS.filter(g => filter === "all" || filter === "active" ? (filter === "all" || ACTIVE.includes(g.key)) : g.key === filter)
    .map(g => ({ ...g, tasks: tasks.filter(t => groupOf(t.status) === g.key) })).filter(g => g.tasks.length)
  // Overview filters show at most CAP rows per group so a long queue (live: 27 in review) does not bury the groups below it.
  // Needs attention is never capped, a search shows every match, and a group's own filter shows all of it.
  // Overview filters (Active, All) collapse every group but Needs attention; a search or a business unit narrows and opens.
  const overview = filter === "active" || filter === "all"
  const searching = q !== "" || unit !== "all"
  const reviewOldest = oldestUpdate(d.tasks, "REVIEW")
  const lead = d.task_lead_time
  const changes = useChanges()
  return <>
    <PageHeader title={pageTitle("tasks")} description="Every task, what it is doing and what happens next."
      >
      <Button variant="outline" className="h-10" onClick={() => compose(true)}><Sparkles />New task</Button>
    </PageHeader>
    <div className="grid gap-3">
      <nav aria-label="Filter work" className="task-filters-scroll -mx-1 flex flex-nowrap gap-1 px-1 pb-1">
        {FILTERS.map(f => {
          const n = count(f.key)
          const on = filter === f.key
          // An empty group's chip hides (its link still works); Active and All always show.
          if (!n && !on && f.key !== "active" && f.key !== "all") return null
          return (
            <button key={f.key} aria-current={on ? "true" : undefined} onClick={() => go("tasks", f.key === "active" ? undefined : f.key)}
              className={cn("inline-flex h-9 shrink-0 items-center gap-1.5 rounded-full border px-3.5 text-[13px] transition-colors duration-150",
                on ? "border-foreground/15 bg-foreground text-background" : "border-border bg-card text-muted-foreground hover:text-foreground")}>
              {f.label}<span className={cn("num text-xs", on ? "text-background/70" : "text-muted-foreground")}>{n}</span>
            </button>
          )
        })}
      </nav>
      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_220px]">
        <div className="relative"><Search className="pointer-events-none absolute top-1/2 left-3 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input type="search" value={query} onChange={e => setQuery(e.target.value)} placeholder="Find a task, agent or trigger…" aria-label="Find a task" className="h-10 pl-9" /></div>
        <Select value={unit} onValueChange={setUnit}>
          <SelectTrigger className="h-10 w-full" aria-label="Filter by business unit"><SelectValue /></SelectTrigger>
          <SelectContent><SelectItem value="all">All business units</SelectItem>
            {[...new Set(d.tasks.map(t => t.business_unit).filter(Boolean))].sort().map(u => <SelectItem key={u} value={u!}>{u}</SelectItem>)}</SelectContent>
        </Select>
      </div>
      {lead && <p className="text-xs text-muted-foreground">
        Median lead time, created → done, last 30 days: <b className="num font-medium text-foreground">{lead.median_hours === null ? "no completions" : `${lead.median_hours} h`}</b>
        {` · ${lead.sample_size} completed · ${lead.review_pending} awaiting review`}</p>}
    </div>
    {groups.length ? groups.map(g => {
      const open = isOpen(g.key, g.tasks, { overview, searching, manual: manual[g.key] })
      const fresh = seen.current ? arrived(g.tasks, seen.current) : 0
      const sig = signals(g.tasks)
      // A closed group hides its rows, so a change inside it is marked on the header instead (lib/changes.ts).
      const moved = open ? 0 : g.tasks.filter(t => changes[`task:${t.key}`] && !pinned([t]).length).length
      return (
        <section key={g.key} aria-label={g.label} className="panel min-w-0 divide-y divide-border/70 overflow-hidden">
          <button type="button" aria-expanded={open} disabled={g.key === "attention"} onClick={() => setManual(m => ({ ...m, [g.key]: !open }))}
            className={cn(moved > 0 && "desk-flash", "flex min-h-11 w-full min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1 px-4 py-2.5 text-left transition-colors enabled:hover:bg-muted/40 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring disabled:cursor-default")}>
            {g.key !== "attention" && <ChevronRight aria-hidden className={cn("size-4 shrink-0 text-muted-foreground transition-transform duration-150", open && "rotate-90")} />}
            <Dot tone={g.tone} />
            <span className="text-[13px] font-semibold">{g.label}</span>
            <span className="num rounded-full bg-muted px-1.5 text-[11px] leading-[18px] font-medium text-muted-foreground">{g.tasks.length}</span>
            {sig.map(x => <Tag key={x} tone={x.endsWith("P0") || x.endsWith("high risk") ? "danger" : "warning"}>{x}</Tag>)}
            {/* What the counts are made of, from statuses and timestamps only (the facts the old figure strip carried). */}
            {g.key === "attention" && attentionBreakdown(g.tasks) && <span className="text-xs text-muted-foreground">{attentionBreakdown(g.tasks)}</span>}
            {g.key === "review" && reviewOldest && <span className="text-xs text-muted-foreground">oldest untouched {ago(reviewOldest)}</span>}
            {fresh > 0 && <span className="rounded-full bg-info/10 px-1.5 text-[11px] leading-[18px] font-medium text-info">+{fresh} new</span>}
            {moved > 0 && <span className="rounded-full bg-info/10 px-1.5 text-[11px] leading-[18px] font-medium text-info">{moved} just changed</span>}
            {g.key !== "attention" && <span className="ml-auto text-xs text-muted-foreground">{open ? "Hide" : `Show all ${g.tasks.length}`}</span>}
          </button>
          {(open ? g.tasks : pinned(g.tasks)).map(t => <TaskLine key={t.key} t={t} reason={reasons[t.key]} paused={paused} />)}
        </section>
      )
    }) : (
      <div className="panel"><PanelEmpty title={q || unit !== "all" ? "No task matches" : filter === "active" ? "No active work" : "Nothing here"}
        action={q || unit !== "all" ? <Button size="sm" variant="outline" onClick={() => { setQuery(""); setUnit("all") }}>Clear search</Button>
          : <Button size="sm" variant="outline" onClick={() => compose(true)}><Sparkles />New task</Button>}>
        {q || unit !== "all" ? "Try another word or business unit." : "A task is recorded when you or the CEO ask for work. Recording is not execution."}
      </PanelEmpty></div>
    )}
  </>
}
