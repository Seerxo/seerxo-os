// What changed between two reads of /desk/state, so the Desk can show the company moving: a row that changed status
// (or appeared) is marked for a few seconds. Only real differences between two reads; the first read marks nothing.

export interface Change { from: string | null; to: string; at: number }
export type Changes = Record<string, Change>

export const FRESH_MS = 12_000   // how long a changed row stays marked

/** `prefix:key` → change, for rows whose status differs from the previous read or that were not in it. */
export function statusChanges<T>(prefix: string, prev: T[] | undefined, next: T[], key: (t: T) => string, status: (t: T) => string, at: number): Changes {
  if (!prev) return {}
  const before = new Map(prev.map(t => [key(t), status(t)]))
  const out: Changes = {}
  for (const t of next) {
    const k = key(t), was = before.get(k), now = status(t)
    if (was !== now) out[`${prefix}:${k}`] = { from: was ?? null, to: now, at }
  }
  return out
}

export const prune = (c: Changes, now: number): Changes =>
  Object.fromEntries(Object.entries(c).filter(([, v]) => now - v.at < FRESH_MS))

/** One sentence for the screen-reader live region. */
export const describe = (k: string, c: Change, humanize: (s: string) => string) =>
  `${k.slice(k.indexOf(":") + 1)} ${c.from ? `moved from ${humanize(c.from)} to ${humanize(c.to)}` : `appeared: ${humanize(c.to)}`}`
