// Which Work groups are open, and what a closed group's header must say so nothing urgent hides behind it.
// Rule: Needs attention is always open; a search or a single-group filter opens every group it shows; otherwise the
// founder's own choice (manual) wins and the default is closed. A closed group still shows its P0 tasks (pinned), so
// critical work is never hidden, without opening a long group (live: 1 P0 among 27 in review).
import type { TaskRow } from "./api.ts"
import type { Group } from "./task-story.ts"

export interface OpenContext { overview: boolean; searching: boolean; manual?: boolean }

export function isOpen(key: Group, tasks: Pick<TaskRow, "priority">[], ctx: OpenContext): boolean {
  if (key === "attention") return true                    // never collapsed: acute work is never one click away
  if (!ctx.overview || ctx.searching) return true          // a group's own filter, or a search, shows everything it found
  void tasks
  return ctx.manual ?? false
}

/** Rows a closed group still shows: its P0 work. */
export const pinned = <T extends Pick<TaskRow, "priority">>(tasks: T[]): T[] => tasks.filter(t => t.priority === "P0")

/** What a collapsed header shows: priority and risk that would otherwise be out of sight. Zero parts are dropped. */
export function signals(tasks: Pick<TaskRow, "priority" | "risk">[]): string[] {
  const n = (f: (t: Pick<TaskRow, "priority" | "risk">) => boolean) => tasks.filter(f).length
  const p0 = n(t => t.priority === "P0"), p1 = n(t => t.priority === "P1"), high = n(t => t.risk === "HIGH" || t.risk === "CRITICAL")
  return [p0 && `${p0} P0`, p1 && `${p1} P1`, high && `${high} high risk`].filter(Boolean) as string[]
}

/** Tasks in a group that were not there when the page was opened (keys seen at first render). */
export const arrived = (tasks: Pick<TaskRow, "key">[], seen: Set<string>) => tasks.filter(t => !seen.has(t.key)).length
