import assert from "node:assert/strict"
import { test } from "node:test"
import type { Event } from "./api.ts"
import { groupEvents, hasRepeats } from "./event-groups.ts"

const ev = (id: number, at: string, title: string, severity = "HIGH", task: string | null = null): Event =>
  ({ id, type: "T", source: "watchdog", severity, at, title, detail: null, task, business_unit: null })

test("interleaved repeats collapse into one row each, with count, first and last occurrence", () => {
  const events = [ev(6, "2026-10-16T20:20:00Z", "A"), ev(5, "2026-10-16T20:20:00Z", "B", "MEDIUM"),
    ev(4, "2026-10-16T20:10:00Z", "A"), ev(3, "2026-10-16T20:10:00Z", "B", "MEDIUM"), ev(2, "2026-10-16T20:00:00Z", "A")]
  const g = groupEvents(events)
  assert.equal(g.length, 2)
  assert.deepEqual([g[0].latest.title, g[0].count, g[0].first, g[0].last], ["A", 3, "2026-10-16T20:00:00Z", "2026-10-16T20:20:00Z"])
  assert.equal(g[0].latest.id, 6)
  assert.equal(g[1].count, 2)
  assert.equal(hasRepeats(events), true)
})

test("different severity or task stays a different row; nothing is merged across them", () => {
  const g = groupEvents([ev(1, "2026-10-16T20:00:00Z", "A", "HIGH", "TASK-1"), ev(2, "2026-10-16T20:10:00Z", "A", "HIGH", "TASK-2"), ev(3, "2026-10-16T20:20:00Z", "A", "MEDIUM", "TASK-1")])
  assert.equal(g.length, 3)
  assert.equal(hasRepeats([ev(1, "2026-10-16T20:00:00Z", "A")]), false)
})

test("most severe first, then most recently seen", () => {
  const g = groupEvents([ev(1, "2026-10-16T21:00:00Z", "low", "LOW"), ev(2, "2026-10-16T19:00:00Z", "high", "HIGH"), ev(3, "2026-10-16T20:00:00Z", "high2", "HIGH")])
  assert.deepEqual(g.map(x => x.latest.title), ["high2", "high", "low"])
})
