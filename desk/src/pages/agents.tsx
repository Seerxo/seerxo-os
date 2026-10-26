// The autonomous workforce. Identity and reporting come from the registry; status, current task and failures from runtime
// evidence; each agent's workload is counted from the task list (task.agent). Nothing here predicts an agent's next task:
// the API does not report one, so a lane shows what is held and what is recorded, never what might come next.
import { ArrowUpRight, ChevronRight } from "lucide-react"
import type { Agent, TaskRow } from "@/lib/api"
import { ago, capacity } from "@/lib/format"
import { GROUPS, groupOf, type Group } from "@/lib/task-story"
import { useChange, useData, useDesk, useNow } from "@/state"
import { agentState, foldable } from "@/lib/agent-state"
import { cn } from "@/lib/utils"
import { Calm, ChangeMark, DOT, Expandable, FigureStrip, Muted, PageHeader, Status, TaskKey } from "@/components/desk"

const short = (name: string) => name.replace(/ Agent$/, "")
const LANE: Group[] = ["attention", "running", "review", "next", "waiting"]   // open work, in the order it needs attention
const LABEL: Partial<Record<Group, string>> = { attention: "stopped", running: "running", review: "in review", next: "queued", waiting: "waiting outside" }

/** One agent's own tasks by group. Failed and blocked are named separately because they need different action. */
function workload(tasks: TaskRow[]) {
  const by = (g: Group) => tasks.filter(t => groupOf(t.status) === g).length
  const failed = tasks.filter(t => t.status === "FAILED").length, blocked = tasks.filter(t => t.status === "BLOCKED").length
  const approval = tasks.filter(t => t.status === "WAITING_APPROVAL").length
  const parts = [failed && `${failed} failed`, blocked && `${blocked} blocked`, approval && `${approval} awaiting approval`,
    ...LANE.filter(g => g !== "attention").map(g => by(g) && `${by(g)} ${LABEL[g]}`), by("closed") && `${by("closed")} done`].filter(Boolean) as string[]
  return { open: LANE.reduce((n, g) => n + by(g), 0), segments: LANE.map(g => ({ g, n: by(g) })).filter(s => s.n), parts }
}

function AgentLane({ agent, tasks, reportsTo }: { agent: Agent; tasks: TaskRow[]; reportsTo: string }) {
  const { open } = useDesk()
  const title = agent.current_task?.title
  const st = agentState(agent, useData(), useNow("minute"))
  const availability = st.value
  const w = workload(tasks)
  const changed = useChange("agent", agent.key)
  return (
    <button type="button" onClick={() => open({ kind: "agent", key: agent.key })}
      aria-label={`${short(agent.name)}, ${availability}${title ? `, holding ${title}` : ""}`}
      className={cn(changed && "desk-flash", "group grid w-full min-w-0 gap-x-6 gap-y-4 px-4 py-4 text-left transition-colors duration-150 first:rounded-t-[inherit] last:rounded-b-[inherit] hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset sm:px-5 lg:grid-cols-[minmax(0,13rem)_minmax(0,1fr)_minmax(0,15rem)_minmax(0,9rem)]")}>
      <span className="grid min-w-0 content-start gap-1">
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[15px] font-semibold tracking-tight">{short(agent.name)}</span>
          <Status value={availability} label={st.label} tone={st.tone} live={st.running} />
          <ChangeMark c={changed} />
        </span>
        <span className="line-clamp-2 text-[13px] leading-5 text-muted-foreground">{agent.role}</span>
        <span className="text-xs text-muted-foreground">{[agent.team, agent.model_class?.toLowerCase()].filter(Boolean).join(" · ")}</span>
      </span>
      {/* current work */}
      <span className="grid min-w-0 content-start gap-1.5">
        <span className="eyebrow">Current work</span>
        {agent.current_task
          ? <span className="min-w-0 text-[13px] leading-5 [overflow-wrap:anywhere]"><TaskKey k={agent.current_task.key} /> <span className="font-medium">{agent.current_task.title}</span>
              <span className="text-muted-foreground"> · task {agent.current_task.status.toLowerCase().replaceAll("_", " ")}</span></span>
          : <span className="text-[13px] text-muted-foreground">No task assigned</span>}
        {agent.last_error && <span className="min-w-0 text-xs [overflow-wrap:anywhere]"><span className="text-muted-foreground">Last failure </span>
          <span className="font-mono text-destructive">{agent.last_error}</span></span>}
      </span>
      {/* workload */}
      <span className="grid min-w-0 content-start gap-1.5">
        <span className="eyebrow">Workload · {w.open} open</span>
        {w.segments.length
          ? <span className="flex h-1.5 overflow-hidden rounded-full bg-border" role="img" aria-label={w.parts.join(", ")}>
              {w.segments.map(s => <i key={s.g} className={DOT[GROUPS.find(x => x.key === s.g)!.tone]} style={{ width: `${(s.n / w.open) * 100}%` }} />)}
            </span>
          : <span className="h-1.5 rounded-full bg-border" aria-hidden />}
        <span className="text-xs leading-snug text-pretty text-muted-foreground">{w.parts.length ? w.parts.join(" · ") : "No tasks in recent work"}</span>
      </span>
      <span className="grid min-w-0 content-start gap-1 text-xs text-muted-foreground">
        <span><span className="text-foreground/80">Last run</span> {agent.last_run ? `${ago(agent.last_run)} ago` : "not recorded"}</span>
        <span><span className="text-foreground/80">Last success</span> {agent.last_success ? `${ago(agent.last_success)} ago` : "not recorded"}</span>
        <span className="truncate"><span className="text-foreground/80">Reports to</span> {reportsTo}</span>
        <span className="mt-1 flex items-center gap-1 text-muted-foreground transition-colors group-hover:text-foreground">Open<ArrowUpRight className="size-3" /></span>
      </span>
    </button>
  )
}

/** Who reports to whom, only from the registry's reports_to. An agent with no recorded manager is listed as such. */
function Organisation({ agents }: { agents: Agent[] }) {
  const name = (k: string) => short(agents.find(a => a.key === k)?.name ?? k)
  const top = agents.filter(a => a.reports_to === "founder")
  const under = (k: string) => agents.filter(a => a.reports_to === k)
  const loose = agents.filter(a => a.reports_to !== "founder" && !agents.some(m => m.key === a.reports_to))
  return (
    <section aria-label="Reporting lines" className="flex flex-wrap items-center gap-x-2 gap-y-1.5 rounded-xl border bg-card px-4 py-2.5 text-[13px]">
      <span className="eyebrow mr-1">Reporting</span>
      <span className="font-medium">Founder</span>
      {top.map(t => <span key={t.key} className="contents">
        <ChevronRight aria-hidden className="size-3.5 text-muted-foreground/60" /><span className="font-medium">{name(t.key)}</span>
        {under(t.key).length > 0 && <><ChevronRight aria-hidden className="size-3.5 text-muted-foreground/60" />
          <span className="text-muted-foreground">{under(t.key).map(a => name(a.key)).join(", ")}</span></>}
      </span>)}
      {loose.length > 0 && <span className="text-muted-foreground">· no recorded manager: {loose.map(a => name(a.key)).join(", ")}</span>}
    </section>
  )
}

/** The model providers every agent runs on, from the worker's own report. Credentials alone are not proof of access. */
function Providers() {
  const d = useData()
  const w = d.worker
  if (!w?.providers || !Object.keys(w.providers).length) return <p className="text-xs text-muted-foreground">Provider capacity is not reported by this API.</p>
  return (
    <section aria-label="Model providers" className="flex flex-wrap items-center gap-x-5 gap-y-1.5 rounded-xl border bg-card px-4 py-2.5 text-[13px]">
      <span className="eyebrow">Providers</span>
      {Object.keys(w.providers).sort().map(name => {
        const c = capacity(name, w)
        // Wraps on narrow phones: a long capacity text ("Credentials configured · …") pushed the page sideways at 320 px.
        return <span key={name} className="flex min-w-0 max-w-full flex-wrap items-center gap-x-2 gap-y-0.5">
          <span className="font-medium capitalize">{name}</span>
          <Status value={c.health ?? "UNKNOWN"} label={c.text} className="min-w-0 shrink whitespace-normal [overflow-wrap:anywhere]" tone={c.health === "AVAILABLE" ? "success" : c.health === "CREDENTIALS_CONFIGURED" ? "neutral" : "warning"} />
        </span>
      })}
    </section>
  )
}


/** Identity comes from the checked-in registry; live status and task attribution come from PostgreSQL. */
export function AgentsPage() {
  const d = useData()
  const now = useNow("minute")
  // RUNNING counts only while the worker is heard from (lib/agent-state.ts); unconfirmed runs are not "running now".
  const n = (s: string) => d.agents.filter(a => (s === "RUNNING" ? agentState(a, d, now).running : a.status === s)).length
  const name = (key: string | null) => key ? short(d.agents.find(a => a.key === key)?.name ?? key) : "—"
  const mine = (a: Agent) => d.tasks.filter(t => t.agent === a.key)
  // An agent with no open task, no held task, no recorded failure and a calm state folds under one line; anything else
  // stays in view. Only a named calm state folds; anything else (unknown, stalled, waiting, unconfirmed) stays visible.
  const calm = (a: Agent) => foldable(a, d, now, workload(mine(a)).open)
  const busy = d.agents.filter(a => !calm(a)), quiet = d.agents.filter(calm)
  const lanes = (list: Agent[], label: string) => (
    <section aria-label={label} className="panel grid min-w-0 grid-cols-1 divide-y divide-border/70">
      {list.map(agent => <AgentLane key={agent.key} agent={agent} tasks={mine(agent)}
        reportsTo={agent.reports_to === "founder" ? "Founder" : name(agent.reports_to)} />)}
    </section>)
  return <>
    <PageHeader title="Agents" description="The autonomous workforce: what each agent holds, its workload and what last failed." />
    {d.agents.length > 0 && <FigureStrip label="Agents at a glance" items={[
      // Zero running is a count, not a verdict: idle agents with READY work can be the problem (lib/operating.ts decides health).
      { label: "Running now", value: String(n("RUNNING")), sub: n("RUNNING") ? "executing a task" : "none executing; not a health signal" },
      { label: "Waiting", value: String(n("WAITING") + n("NEEDS_APPROVAL")), tone: n("NEEDS_APPROVAL") ? "warning" : undefined, sub: "on review, approval or input" },
      { label: "Blocked or failed", value: String(n("BLOCKED") + n("FAILED") + n("STALLED")), tone: n("BLOCKED") + n("FAILED") + n("STALLED") ? "danger" : undefined,
        sub: n("BLOCKED") + n("FAILED") + n("STALLED") ? "cannot continue on its own" : "none stopped" },
      { label: "Idle with work", value: String(d.agents.filter(a => ["SLEEPING", "IDLE"].includes(a.status) && a.current_task).length),
        tone: d.agents.some(a => ["SLEEPING", "IDLE"].includes(a.status) && a.current_task) ? "warning" : undefined, sub: "holds a task but is not running it" },
    ]} />}
    {d.agents.length > 0 && <div className="grid min-w-0 grid-cols-1 gap-2 xl:grid-cols-2"><Organisation agents={d.agents} /><Providers /></div>}
    {!d.agents.length ? <Calm title="No agents synced">The registry has not been synced to the database yet.</Calm> : (
      <div className="grid min-w-0 gap-3">
        {busy.length > 0 ? lanes(busy, "Agents with work") : <p className="text-sm text-muted-foreground">No agent holds open work or has a recorded failure.</p>}
        {quiet.length > 0 && <Expandable summary={<span className="text-[13px]"><span className="font-medium">{quiet.length} {quiet.length === 1 ? "agent" : "agents"} with nothing open</span>
          <Muted> · {quiet.map(a => short(a.name)).join(", ")}</Muted></span>}>{lanes(quiet, "Agents with nothing open")}</Expandable>}
      </div>
    )}
  </>
}
