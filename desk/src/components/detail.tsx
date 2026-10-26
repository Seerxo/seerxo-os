// Task / agent detail in a side sheet, plus the confirmation and result dialogs the controls use.
// Summary and evidence first; raw context and agent configuration only under "Advanced".
import { useEffect, useState, type ReactNode } from "react"
import { Check, ChevronRight, Copy, Play } from "lucide-react"
import { toast } from "sonner"
import { api, type AgentDetail, type Run, type TaskDetail } from "@/lib/api"
import { ago, duration, humanize, usd, when } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useDesk } from "@/state"
import { Facts, Muted, priorityTone, Status, Tag, TaskKey } from "@/components/desk"
import { story } from "@/lib/task-story"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { ErrorBoundary } from "@/components/error-boundary"
import { checkAgentDetail, checkTaskDetail } from "@/lib/validate"
import { canOfferReady, isCodeWrite, readyNote } from "@/lib/ready"
import { AuthorizeStageC, MarkReady } from "@/components/controls"
import { taskPipeline, type StepState } from "@/lib/pipeline"
import { deliverableItems } from "@/lib/deliverable"
import { useReturnFocus } from "@/lib/return-focus"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"

export function Confirm({ title, description, action, destructive, onConfirm, children }: {
  title: string; description: ReactNode; action: string; destructive?: boolean; onConfirm: () => void; children: ReactNode
}) {
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>{children}</AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription asChild><div className="grid gap-2 text-sm">{description}</div></AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction variant={destructive ? "destructive" : "default"} onClick={onConfirm}>{action}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

export function InfoDialog({ title, open, onOpenChange, children, description }: {
  title: string; open: boolean; onOpenChange: (o: boolean) => void; children: ReactNode; description?: string
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>
        {children}
      </DialogContent>
    </Dialog>
  )
}

export const Pre = ({ children }: { children: ReactNode }) => (
  <pre className="max-h-96 overflow-auto rounded-lg bg-muted p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere]">{children}</pre>
)

const Section = ({ title, children }: { title: string; children: ReactNode }) => (
  <section className="grid min-w-0 grid-cols-1 gap-2">
    <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>
    {children}
  </section>
)

const Text = ({ text, none }: { text: string | null | undefined; none: string }) =>
  text ? <p className="text-sm leading-relaxed whitespace-pre-wrap">{text}</p> : <p className="text-sm text-muted-foreground">{none}</p>

const Chips = ({ items }: { items: (string | null | undefined)[] | string | null | undefined }) => {
  const list = (Array.isArray(items) ? items : items ? String(items).split(/[,\s]+/) : []).filter(Boolean) as string[]
  return list.length ? <div className="flex flex-wrap gap-1.5">{list.map(x => <Tag key={x}>{x}</Tag>)}</div> : <Muted>None</Muted>
}
export { Chips }

function Advanced({ children }: { children: ReactNode }) {
  return (
    <Collapsible className="rounded-lg border bg-surface/50">
      <CollapsibleTrigger className="group flex w-full items-center gap-2 px-3 py-2 text-sm text-muted-foreground hover:text-foreground">
        <ChevronRight className="size-4 transition-transform group-data-[state=open]:rotate-90" />Advanced · raw data
      </CollapsibleTrigger>
      <CollapsibleContent className="grid gap-4 border-t p-3">{children}</CollapsibleContent>
    </Collapsible>
  )
}

// Recorded fields only: no invented steps, durations or tokens.
export function Runs({ runs }: { runs: Run[] }) {
  const { open } = useDesk()
  if (!runs.length) return <Muted>No runs recorded. The worker has not executed this yet.</Muted>
  return (
    <div className="grid gap-2">
      {runs.map(r => (
        <div key={r.id} className="grid gap-1 rounded-lg border p-2.5 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <Status value={r.status} />
            <span className="font-mono text-xs">{r.provider ?? "—"}</span>
            <Muted>{when(r.started_at)}</Muted>
            <span className="font-mono text-xs text-muted-foreground">{duration(r.started_at, r.ended_at)}</span>
            {r.task && <button className="ml-auto" onClick={() => open({ kind: "task", key: r.task! })}><TaskKey k={r.task} /></button>}
          </div>
          {r.supervisor_verdict && <div className="text-xs text-muted-foreground">Supervisor <b className="text-foreground">{r.supervisor_verdict}</b>{r.supervisor_notes && ` · ${r.supervisor_notes}`}</div>}
          {r.error && <div className="text-xs text-destructive">{r.error}</div>}
        </div>
      ))}
    </div>
  )
}

/** Where the task stands, why it stopped (recorded reason only), what happens next, and what the founder can do from
 *  the Desk. Actions are limited to the Desk's authorized calls: assign, the approval inbox, and a new task. */
function StandsNow({ t }: { t: TaskDetail }) {
  const { data, act, open, go, compose } = useDesk()
  const s = story(t, { paused: data?.control.agents_paused })
  const [agent, setAgent] = useState(t.assigned_agent ?? "")
  const agents = (data?.agents ?? []).filter(a => a.enabled !== false)
  return (
    <section aria-label="Where this task stands" className="grid gap-4 rounded-xl border bg-surface/60 p-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <Status value={t.status} pill tone={s.tone} />
        <span className="text-sm font-medium">{s.now}</span>
      </div>
      {s.blocker && (
        <div className={cn("grid gap-1 rounded-lg border-l-2 py-1.5 pl-3 text-sm", s.tone === "danger" ? "border-destructive" : "border-warning")}>
          <span className="text-xs font-medium text-muted-foreground">{["BLOCKED", "FAILED"].includes(t.status) ? "Why it stopped" : "Waiting on"}</span>
          <span className="[overflow-wrap:anywhere]">{s.blocker === "unrecorded" ? <Muted>No reason was recorded. The run history below is the only evidence.</Muted> : s.blocker}</span>
          {s.waitingOn.length > 0 && <span className="flex flex-wrap gap-1.5 pt-0.5">{s.waitingOn.map(d => (
            <button key={d.key} onClick={() => open({ kind: "task", key: d.key })} className="rounded-md border bg-background px-1.5 py-0.5 font-mono text-[11px] hover:bg-muted">{d.key} · {humanize(d.status).toLowerCase()}</button>))}</span>}
        </div>
      )}
      <p className="text-[13px] text-muted-foreground"><span className="font-medium text-foreground">Next: </span>{s.next}</p>
      {/* Where the task is, step by step, from recorded facts only (lib/pipeline.ts); updates live while open. */}
      <Pipeline t={t} />
      {(s.founder.length > 0 || canOfferReady(t) || isCodeWrite(t)) && (
        <div className="grid gap-2 border-t border-border/70 pt-3">
          <span className="text-xs font-medium text-muted-foreground">What you can do</span>
          <div className="flex flex-wrap items-center gap-2">
            {s.founder.includes("approve") && <Button size="sm" className="h-9" onClick={() => { open(null); go("approvals") }}>Decide in Approvals</Button>}
            {s.founder.includes("assign") && <>
              <Select value={agent} onValueChange={setAgent}>
                <SelectTrigger className="h-9 w-48" aria-label="Agent to assign"><SelectValue placeholder="Choose agent" /></SelectTrigger>
                <SelectContent>{agents.map(a => <SelectItem key={a.key} value={a.key}>{a.name}</SelectItem>)}</SelectContent>
              </Select>
              <Button size="sm" variant="outline" className="h-9" disabled={!agent || agent === t.assigned_agent}
                onClick={async () => { if (await act(`/tasks/${encodeURIComponent(t.key)}/assign`, { agent, actor: "founder" }, `${t.key} assigned to ${agent}`)) open(null) }}>
                {t.assigned_agent ? "Reassign" : "Assign"}
              </Button>
            </>}
            {canOfferReady(t) && <MarkReady taskKey={t.key} status={t.status} codeWrite={isCodeWrite(t)} note={readyNote(t)} />}
            {s.founder.includes("follow-up") && <Button size="sm" variant="outline" className="h-9" onClick={() => { open(null); compose(true) }}>New follow-up task</Button>}
            {(t.context as { write?: unknown } | null)?.write === true && !["DONE", "CANCELLED"].includes(t.status) && <AuthorizeStageC taskKey={t.key} />}
          </div>
          <p className="text-[11px] text-muted-foreground">Closing or cancelling a task is not available in the Desk yet.</p>
        </div>
      )}
    </section>
  )
}

function TaskBody({ t }: { t: TaskDetail }) {
  const { data, go, open } = useDesk()
  const deliverable = deliverableItems(t.artifacts)
  const unit = data?.portfolio?.find(u => u.key === t.business_unit)
  return (
    <div className="grid min-w-0 grid-cols-1 gap-6">
      <StandsNow t={t} />
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px] text-muted-foreground">
        <Tag tone={priorityTone(t.priority)}>{t.priority}</Tag>
        <span>{t.risk.toLowerCase()} risk</span>
        {t.business_unit && (unit
          ? <button type="button" className="inline-flex min-h-7 items-center gap-1 font-medium text-foreground underline-offset-2 hover:underline"
              onClick={() => { open(null); go("project", unit.key) }}>· {unit.name}{t.project && ` / ${t.project}`}<ChevronRight className="size-3.5" /></button>
          : <span>· {t.business_unit}{t.project && ` / ${t.project}`}</span>)}
        <span>· {t.assigned_agent ? <>owned by <b className="font-medium text-foreground">{t.assigned_agent}</b></> : "unassigned"}</span>
        {t.approval_required && <Status value="NEEDS_APPROVAL" label="Founder approval required" />}
      </div>
      {t.origin && (
        <Section title="Why this task exists">
          <p className="text-sm">{t.origin.automatic ? <>Opened automatically by <b className="font-medium">{t.origin.by}</b></> : <>Requested by <b className="font-medium">{t.origin.by}</b></>}
            {t.origin.trigger && <Muted> · {t.origin.trigger}</Muted>}</p>
          {t.origin.reason && t.origin.reason !== t.origin.trigger && <Muted className="text-xs">{t.origin.reason}</Muted>}
          {t.goal && <p className="text-xs"><Muted>Serves goal </Muted>{t.goal.title}{t.goal.status && <Muted> · {t.goal.status.toLowerCase()}</Muted>}</p>}
        </Section>
      )}
      <Section title="Objective"><Text text={t.objective} none="No objective recorded." /></Section>
      <Section title="Done when"><Text text={t.success_criteria} none="No success criteria recorded." /></Section>
      {/* An automatic task's description only restates its origin, shown above. */}
      {!t.origin?.automatic && t.description && t.description !== t.title && t.description !== t.objective && <Section title="Request"><Text text={t.description} none="" /></Section>}
      {deliverable.length > 0 && (
        <Section title="Deliverable">
          <ol className="grid list-decimal gap-2 pl-5 text-sm leading-relaxed [overflow-wrap:anywhere]">
            {deliverable.map((x, i) => <li key={i} className="whitespace-pre-wrap">{x}</li>)}
          </ol>
        </Section>
      )}
      <Section title="Result">
        {/* A stopped task's error is shown as "Why it stopped" above; here only what the agent produced. */}
        {t.error && !["BLOCKED", "FAILED"].includes(t.status) ? <p className="text-sm text-destructive [overflow-wrap:anywhere]">{t.error}</p> : <Text text={t.result} none="No result recorded yet." />}
        {t.supervisor && <p className="mt-1 text-sm"><Muted>Supervisor verdict: </Muted><Status value={t.supervisor.verdict} />{t.supervisor.notes && <Muted> · {t.supervisor.notes}</Muted>}</p>}
      </Section>
      {t.dependencies.length > 0 && <Section title="Depends on"><Chips items={t.dependencies.map(d => `${d.key} · ${d.status}`)} /></Section>}
      <Section title="Runs"><Runs runs={t.runs} /></Section>
      <Advanced>
        <Section title="Context">{t.context ? <Pre>{JSON.stringify(t.context, null, 2)}</Pre> : <Muted>None</Muted>}</Section>
        <Section title="Artifacts">{t.artifacts ? <Pre>{JSON.stringify(t.artifacts, null, 2)}</Pre> : <Muted>None</Muted>}</Section>
      </Advanced>
    </div>
  )
}

function waitReason(reason: string | null) {
  if (reason === null) return "Eligible for unattended work"
  const labels: Record<string, string> = {
    not_opted_in: "Not enabled for unattended work", needs_human_gate: "Requires human review / approval",
    waiting_external: "Waiting for external evidence", awaiting_review: "Waiting for result review",
    awaiting_ready: "Queued; waiting for READY admission",
    approval_required: "Waiting for approval", agents_paused: "Automation is paused",
    agent_disabled: "Agent is disabled", blocked: "Task is blocked", running: "Task is running",
    daily_run_budget: "Daily run budget reached", code_write_disabled: "Repository writes are disabled",
    stage_c_not_implemented: "Stage C execution is unavailable",
  }
  return labels[reason] ?? reason.replaceAll("_", " ")
}

function AgentBody({ a }: { a: AgentDetail }) {
  const { open } = useDesk()
  const [work, setWork] = useState<string[] | null | "paused">(null)
  const runOnce = async () => {
    const out = await api<{ paused: boolean; work: { agent: string; next_command: string }[] }>("/dispatch/run", {})
    setWork(out.paused ? "paused" : out.work.filter(w => w.agent === a.key).map(w => w.next_command))
  }
  return (
    <div className="grid min-w-0 grid-cols-1 gap-5">
      <div className="flex flex-wrap items-center gap-2"><Status value={a.status} /><Muted>{a.role}</Muted></div>
      <Section title="Unattended task eligibility">
        {a.unattended_tasks === undefined ? <Muted>No verified eligibility data from this API.</Muted>
          : a.unattended_tasks.length ? <div className="grid grid-cols-1 gap-2">{a.unattended_tasks.map(t => (
            <button key={t.task} onClick={() => open({ kind: "task", key: t.task })}
              className="rounded-lg border px-3 py-2 text-left text-sm hover:bg-muted">
              <TaskKey k={t.task} /> · {waitReason(t.reason)}
              {t.approval_required && <p className="text-xs text-muted-foreground">Founder approval required.</p>}
            </button>))}</div> : <Muted>No open tasks in the recent task window.</Muted>}
        <p className="mt-2 text-xs text-muted-foreground">Task eligibility is separate from provider capacity. No model is called by this view.</p>
      </Section>
      <div><Button size="sm" onClick={runOnce}><Play />Check attended task plans</Button></div>
      {work !== null && (work === "paused" ? <Muted>Agent execution is paused. No models were called.</Muted>
        : work.length ? <><p className="text-sm"><b>{work.length}</b> attended task plan(s). Run on the laptop; approval gates still apply:</p><Pre>{work.join("\n")}</Pre></>
        : <Muted>No work for {a.key}. It stays asleep; no model was called.</Muted>)}
      <Section title="Now">
        <Facts items={[["Current task", a.current_task && `${a.current_task.key} · ${a.current_task.title}`],
          ["Last run", a.last_run && when(a.last_run)], ["Last success", a.last_success && when(a.last_success)], ["Cost today", usd(a.cost_today_usd)]]} />
      </Section>
      {a.last_error && <Section title="Last recorded failure"><p className="text-sm">{a.last_error}</p><Muted>Historical information; current task eligibility is shown above.</Muted></Section>}
      <Section title="Recent work">
        {a.recent_tasks.length ? <div className="grid grid-cols-1 gap-1.5">{a.recent_tasks.map(t => (
          <button key={t.key} onClick={() => open({ kind: "task", key: t.key })}
            className="flex items-center gap-2 rounded-lg border px-2.5 py-2 text-left text-sm hover:bg-muted">
            <TaskKey k={t.key} /><span className="min-w-0 flex-1 truncate">{t.title}</span><Status value={t.status} />
          </button>))}</div> : <Muted>No task history.</Muted>}
      </Section>
      <Section title="Run history"><Runs runs={a.recent_runs ?? []} /></Section>
      <Section title="Learnings">
        {a.recent_learnings.length ? a.recent_learnings.map(l => <p key={l.title} className="text-sm"><b>{l.title}</b><br /><Muted>{l.lesson}</Muted></p>) : <Muted>No related learnings.</Muted>}
      </Section>
      <Section title="Notebook">
        {a.notebook.length ? a.notebook.map(n => <div key={n.note} className="flex gap-2 text-sm"><Tag>×{n.refs}</Tag><span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{n.note}</span><Muted className="shrink-0">{ago(n.last_seen_at)}</Muted></div>) : <Muted>No notebook entries.</Muted>}
      </Section>
      <Advanced>
        <Section title="Identity"><p className="text-sm">{a.identity.description}</p><Chips items={a.identity.capabilities} /></Section>
        <Section title="Skills"><Chips items={a.identity.skills} /></Section>
        <Section title="Allowed tools"><Chips items={a.identity.allowed_tools} /></Section>
        <Section title="Triggers"><Chips items={a.triggers.map(x => x.type + (x.event_type ? `: ${x.event_type}` : ""))} /></Section>
        <Section title="Schedule">{a.schedule ? <Pre>{JSON.stringify(a.schedule, null, 2)}</Pre> : <Muted>No automatic schedule</Muted>}</Section>
        <Section title="Memory policy"><Pre>{JSON.stringify(a.memory_policy, null, 2)}</Pre></Section>
      </Advanced>
    </div>
  )
}

function CopyKey({ value }: { value: string }) {
  const [done, setDone] = useState(false)
  return (
    <button aria-label={`Copy ${value}`} title="Copy key" className="grid size-5 place-items-center rounded text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      onClick={() => navigator.clipboard.writeText(value).then(() => { setDone(true); setTimeout(() => setDone(false), 1200) }, () => toast.error("Copy failed"))}>
      {done ? <Check className="size-3" /> : <Copy className="size-3" />}
    </button>
  )
}

/** Phones get the detail as a bottom sheet (thumb reach, full width); wider screens keep the side sheet. */
function useNarrow() {
  const query = "(max-width: 639px)"
  const [narrow, setNarrow] = useState(() => typeof matchMedia === "function" && matchMedia(query).matches)
  useEffect(() => {
    const m = matchMedia(query)
    const on = () => setNarrow(m.matches)
    m.addEventListener("change", on)
    return () => m.removeEventListener("change", on)
  }, [])
  return narrow
}

export function DetailSheet() {
  const { detail, open, lastOk } = useDesk()
  const narrow = useNarrow()
  // The body keeps its own kind: while the sheet animates closed `detail` is already null.
  const [body, setBody] = useState<{ kind: "task"; v: TaskDetail } | { kind: "agent"; v: AgentDetail } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const returnFocus = useReturnFocus()
  useEffect(() => {
    if (!detail) return
    let live = true
    setBody(null); setError(null)
    const { kind, key } = detail
    api<unknown>(`/${kind === "task" ? "tasks" : "agents"}/${encodeURIComponent(key)}`)
      .then(v => live && setBody(kind === "task" ? { kind, v: checkTaskDetail(v, key) } : { kind, v: checkAgentDetail(v, key) }))
      .catch(e => live && setError((e as Error).message))
    return () => { live = false }
  }, [detail, attempt])
  // Watch it live: every new state read (each one follows a change notice from the stream) re-reads the open item in
  // place, so a run starting, a verdict or a PR appears without closing the sheet. A failed re-read keeps what is shown.
  useEffect(() => {
    if (!detail || !lastOk) return
    let live = true
    const { kind, key } = detail
    api<unknown>(`/${kind === "task" ? "tasks" : "agents"}/${encodeURIComponent(key)}`)
      .then(v => live && setBody(b => (b && b.v.key === key ? (kind === "task" ? { kind, v: checkTaskDetail(v, key) } : { kind, v: checkAgentDetail(v, key) }) : b)))
      .catch(() => {})
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-read on each new state read only
  }, [lastOk])
  const key = body?.kind === "task" ? body.v.key : body ? body.v.key : detail?.key
  const title = !body ? "Loading…" : body.kind === "task" ? body.v.title : body.v.name
  return (
    <Sheet open={!!detail} onOpenChange={o => !o && open(null)}>
      <SheetContent side={narrow ? "bottom" : "right"}
        className={narrow ? "max-h-[88dvh] overflow-y-auto rounded-t-2xl" : "overflow-y-auto data-[side=right]:w-full data-[side=right]:sm:max-w-xl"}
        onCloseAutoFocus={returnFocus}>
        <SheetHeader className="sticky top-0 z-10 gap-1 border-b bg-popover/95 px-5 pt-5 pb-4 backdrop-blur-xl">
          <SheetDescription className="flex items-center gap-1.5 font-mono text-xs">{(body?.kind ?? detail?.kind) === "agent" ? "Agent · " : ""}{key}
            {key && <CopyKey value={key} />}</SheetDescription>
          <SheetTitle className="pr-6 text-[17px] leading-snug font-semibold tracking-tight">{title}</SheetTitle>
        </SheetHeader>
        <div className="px-5 pt-1 pb-8">
          {error ? <div role="alert" className="grid justify-items-start gap-2 text-sm"><p className="text-destructive [overflow-wrap:anywhere]">{error}</p>
              <Button size="sm" variant="outline" onClick={() => setAttempt(n => n + 1)}>Try again</Button></div>
            : !body ? <div className="grid gap-3"><Skeleton className="h-6 w-40" /><Skeleton className="h-24" /><Skeleton className="h-40" /></div>
            : <ErrorBoundary label="These details" onRetry={() => setAttempt(n => n + 1)} resetKey={`${detail?.kind}:${detail?.key}:${attempt}`}>
                {body.kind === "task" ? <TaskBody key={body.v.key} t={body.v} /> : <AgentBody a={body.v} />}</ErrorBoundary>}
        </div>
      </SheetContent>
    </Sheet>
  )
}

const STEP: Record<StepState, { dot: string; text: string; word: string }> = {
  done: { dot: "bg-success", text: "", word: "Done" },
  current: { dot: "bg-info desk-pulse", text: "text-info", word: "Now" },
  waiting: { dot: "bg-warning", text: "text-warning", word: "Waiting" },
  failed: { dot: "bg-destructive", text: "text-destructive", word: "Failed" },
  blocked: { dot: "bg-destructive", text: "text-destructive", word: "Blocked" },
  cancelled: { dot: "bg-muted-foreground/50", text: "text-muted-foreground", word: "Cancelled" },
  pending: { dot: "bg-border", text: "text-muted-foreground", word: "Not reached" },
}

/** The task's own pipeline (lib/pipeline.ts): recorded facts only; a step not reached is drawn as not reached. */
function Pipeline({ t }: { t: TaskDetail }) {
  const steps = taskPipeline(t)
  return (
    <ol className="grid">
      {steps.map((s, i) => {
        const v = STEP[s.state], took = s.at && s.until ? duration(s.at, s.until) : null
        return (
          <li key={s.key} className="grid grid-cols-[14px_minmax(0,1fr)] gap-x-3">
            <span className="relative flex justify-center" aria-hidden>
              <span className={cn("relative z-10 mt-[5px] size-2.5 rounded-full ring-4 ring-background", v.dot)} />
              {i < steps.length - 1 && <span className="absolute top-4 bottom-0 w-px bg-border" />}
            </span>
            <div className={cn("grid min-w-0 gap-0.5", i < steps.length - 1 && "pb-3.5")}>
              <span className="flex min-w-0 flex-wrap items-baseline gap-x-2 text-sm">
                <span className={cn("font-medium", s.state === "pending" && "text-muted-foreground")}>{s.label}</span>
                <span className={cn("text-xs", v.text || "text-muted-foreground")}>{v.word}</span>
                {s.at && <span className="num text-xs text-muted-foreground" title={when(s.at)}>{ago(s.at)} ago{took && ` · took ${took}`}</span>}
              </span>
              {s.detail && <span className={cn("text-xs [overflow-wrap:anywhere]", s.state === "failed" || s.state === "blocked" ? "text-destructive" : "text-muted-foreground")}>{s.detail}</span>}
              {s.href && <a href={s.href} target="_blank" rel="noopener noreferrer" className="w-fit text-xs font-medium text-foreground underline underline-offset-2 hover:text-info">Open pull request on GitHub</a>}
            </div>
          </li>
        )
      })}
    </ol>
  )
}
