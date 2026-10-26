// Runtime checks for API responses the Desk renders. A response that does not have the shape a page
// reads throws a short Error naming the broken field; the caller shows it as a recoverable error state.
// Optional fields (undefined or null) are accepted where the page already handles "none".
import type { AgentDetail, Memory, TaskDetail } from "./api"

type Obj = Record<string, unknown>
const bad = (path: string, want: string): never => { throw new Error(`Malformed response: "${path}" should be ${want}`) }

export const obj = (v: unknown, path: string): Obj => (v && typeof v === "object" && !Array.isArray(v) ? v as Obj : bad(path, "an object"))
export const str = (v: unknown, path: string): string => (typeof v === "string" ? v : bad(path, "a string"))
export const num = (v: unknown, path: string): number => (typeof v === "number" && Number.isFinite(v) ? v : bad(path, "a number"))
export const bool = (v: unknown, path: string): boolean => (typeof v === "boolean" ? v : bad(path, "a boolean"))
export const strOrNull = (v: unknown, path: string): string | null => (v == null ? null : str(v, path))
export const list = <T>(v: unknown, path: string, each: (item: unknown, at: string) => T): T[] =>
  Array.isArray(v) ? v.map((item, i) => each(item, `${path}[${i}]`)) : bad(path, "a list")
const strings = (v: unknown, path: string) => list(v, path, str)

const run = (v: unknown, at: string) => {
  const r = obj(v, at)
  return { ...r, id: num(r.id, `${at}.id`), status: str(r.status, `${at}.status`), started_at: str(r.started_at, `${at}.started_at`),
    task: strOrNull(r.task, `${at}.task`), provider: strOrNull(r.provider, `${at}.provider`), ended_at: strOrNull(r.ended_at, `${at}.ended_at`),
    error: strOrNull(r.error, `${at}.error`), supervisor_verdict: strOrNull(r.supervisor_verdict, `${at}.supervisor_verdict`),
    supervisor_notes: strOrNull(r.supervisor_notes, `${at}.supervisor_notes`) }
}
const keyed = (v: unknown, at: string) => { const o = obj(v, at); return { ...o, key: str(o.key, `${at}.key`), title: str(o.title, `${at}.title`), status: str(o.status, `${at}.status`) } }

export function checkTaskDetail(raw: unknown, key: string): TaskDetail {
  const t = obj(raw, "task")
  if (str(t.key, "task.key") !== key) bad("task.key", `${key}`)
  const origin = t.origin == null ? undefined : (() => { const o = obj(t.origin, "task.origin"); return { ...o, by: str(o.by, "task.origin.by"), automatic: bool(o.automatic, "task.origin.automatic"), trigger: strOrNull(o.trigger, "task.origin.trigger"), reason: strOrNull(o.reason, "task.origin.reason") } })()
  const goal = t.goal == null ? null : (() => { const g = obj(t.goal, "task.goal"); return { id: num(g.id, "task.goal.id"), title: str(g.title, "task.goal.title"), status: str(g.status, "task.goal.status") } })()
  const sup = t.supervisor == null ? null : (() => { const s = obj(t.supervisor, "task.supervisor"); return { verdict: str(s.verdict, "task.supervisor.verdict"), notes: strOrNull(s.notes, "task.supervisor.notes") } })()
  return { ...t, key, title: str(t.title, "task.title"), status: str(t.status, "task.status"), priority: str(t.priority, "task.priority"), risk: str(t.risk, "task.risk"),
    description: strOrNull(t.description, "task.description"), objective: strOrNull(t.objective, "task.objective"), success_criteria: strOrNull(t.success_criteria, "task.success_criteria"),
    assigned_agent: strOrNull(t.assigned_agent, "task.assigned_agent"), project: strOrNull(t.project, "task.project"), business_unit: strOrNull(t.business_unit, "task.business_unit"),
    result: strOrNull(t.result, "task.result"), error: strOrNull(t.error, "task.error"), approval_required: bool(t.approval_required, "task.approval_required"),
    dependencies: list(t.dependencies, "task.dependencies", (d, at) => { const o = obj(d, at); return { key: str(o.key, `${at}.key`), status: str(o.status, `${at}.status`) } }),
    runs: list(t.runs, "task.runs", run), origin, goal, supervisor: sup,
    created_at: str(t.created_at, "task.created_at"), started_at: strOrNull(t.started_at, "task.started_at"), completed_at: strOrNull(t.completed_at, "task.completed_at") } as TaskDetail
}

export function checkAgentDetail(raw: unknown, key: string): AgentDetail {
  const a = obj(raw, "agent")
  if (str(a.key, "agent.key") !== key) bad("agent.key", `${key}`)
  const id = obj(a.identity, "agent.identity")
  const cur = a.current_task == null ? null : keyed(a.current_task, "agent.current_task")
  return { ...a, key, name: str(a.name, "agent.name"), role: str(a.role, "agent.role"), status: str(a.status, "agent.status"), current_task: cur,
    last_run: strOrNull(a.last_run, "agent.last_run"), last_success: strOrNull(a.last_success, "agent.last_success"), last_error: strOrNull(a.last_error, "agent.last_error"),
    identity: { description: str(id.description, "agent.identity.description"), capabilities: strings(id.capabilities, "agent.identity.capabilities"),
      skills: strings(id.skills, "agent.identity.skills"), allowed_tools: strings(id.allowed_tools, "agent.identity.allowed_tools") },
    triggers: list(a.triggers, "agent.triggers", (x, at) => { const o = obj(x, at); return { ...o, type: str(o.type, `${at}.type`) } }),
    recent_tasks: list(a.recent_tasks, "agent.recent_tasks", keyed),
    recent_runs: a.recent_runs == null ? [] : list(a.recent_runs, "agent.recent_runs", run),
    recent_learnings: list(a.recent_learnings, "agent.recent_learnings", (x, at) => { const o = obj(x, at); return { title: str(o.title, `${at}.title`), lesson: str(o.lesson, `${at}.lesson`) } }),
    notebook: list(a.notebook, "agent.notebook", (x, at) => { const o = obj(x, at); return { note: str(o.note, `${at}.note`), refs: num(o.refs, `${at}.refs`), last_seen_at: str(o.last_seen_at, `${at}.last_seen_at`) } }),
    unattended_tasks: a.unattended_tasks == null ? undefined : list(a.unattended_tasks, "agent.unattended_tasks", (x, at) => { const o = obj(x, at)
      return { task: str(o.task, `${at}.task`), status: str(o.status, `${at}.status`), reason: strOrNull(o.reason, `${at}.reason`), approval_required: bool(o.approval_required, `${at}.approval_required`) } }),
  } as AgentDetail
}

export function checkMemory(raw: unknown): Memory {
  const m = obj(raw, "memory")
  const tags = (v: unknown, at: string) => (v == null || typeof v === "string" ? v ?? null : strings(v, at))
  if (m.permanent != null && typeof m.permanent !== "string") bad("memory.permanent", "a string or null")
  return { ...m, permanent: (m.permanent as string | null | undefined) ?? null,
    decisions: list(m.decisions, "memory.decisions", (x, at) => { const o = obj(x, at); return { ...o, key: str(o.key, `${at}.key`), title: str(o.title, `${at}.title`), decision: str(o.decision, `${at}.decision`),
      status: str(o.status, `${at}.status`), reason: strOrNull(o.reason, `${at}.reason`), approved_by: strOrNull(o.approved_by, `${at}.approved_by`), created_at: str(o.created_at, `${at}.created_at`) } }),
    learnings: list(m.learnings, "memory.learnings", (x, at) => { const o = obj(x, at); return { ...o, id: num(o.id, `${at}.id`), title: str(o.title, `${at}.title`), lesson: str(o.lesson, `${at}.lesson`),
      tags: tags(o.tags, `${at}.tags`), created_at: str(o.created_at, `${at}.created_at`) } }),
    notes: list(m.notes, "memory.notes", (x, at) => { const o = obj(x, at); return { ...o, note: str(o.note, `${at}.note`), refs: num(o.refs, `${at}.refs`), last_seen_at: str(o.last_seen_at, `${at}.last_seen_at`),
      promoted: bool(o.promoted, `${at}.promoted`), tags: tags(o.tags, `${at}.tags`) } }),
  } as Memory
}
