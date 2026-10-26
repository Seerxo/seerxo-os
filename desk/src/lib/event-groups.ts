// Repeated events as one row. Live data (2026-10-16): 40 recent events were 4 watchdog conditions re-recorded every
// 10 minutes, interleaved, so a timeline was mostly repetition. A group keeps every fact the rows had: how many times,
// first and last occurrence, and the newest row's own fields. Nothing is dropped; the timeline view is still there.
import type { Event } from "./api"

export interface EventGroup { key: string; latest: Event; count: number; first: string; last: string }

const SEV_RANK: Record<string, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2 }

export function groupEvents(events: Event[]): EventGroup[] {
  const groups = new Map<string, EventGroup>()
  for (const e of events) {
    const key = [e.type, e.source, e.severity, e.title, e.task ?? ""].join("\u0000")
    const g = groups.get(key)
    if (!g) { groups.set(key, { key, latest: e, count: 1, first: e.at, last: e.at }); continue }
    g.count++
    if (e.at > g.last) { g.last = e.at; g.latest = e }
    if (e.at < g.first) g.first = e.at
  }
  // Most severe first, then most recently seen: the order a founder triages in.
  return [...groups.values()].sort((a, b) => (SEV_RANK[a.latest.severity] ?? 3) - (SEV_RANK[b.latest.severity] ?? 3) || b.last.localeCompare(a.last))
}

/** True when grouping removes rows; otherwise the timeline already is the summary. */
export const hasRepeats = (events: Event[]) => groupEvents(events).length < events.length
