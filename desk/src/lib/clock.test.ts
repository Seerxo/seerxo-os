// The Desk context must not carry a per-second clock: it re-rendered every page each second while idle
// (measured before the fix: Overview, BusinessTable, TasksPage, sidebar at 1 render/s; after: 0). Run with `npm test`.
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"

const state = readFileSync(new URL("../state.tsx", import.meta.url), "utf8")
const ctx = /interface Ctx \{([\s\S]*?)\n\}/.exec(state)?.[1] ?? ""

test("the shared Desk context holds no clock and no clock-derived value", () => {
  assert.ok(ctx.includes("data: DeskState | null"), "Ctx interface not found")
  assert.doesNotMatch(ctx, /\bnow\b|\bstale\b/)
  assert.match(state, /<ClockProvider>\{children\}<\/ClockProvider>/)
  assert.match(state, /export function useNow\(resolution: "second" \| "minute"\)/)
})

test("only what shows seconds subscribes to the second clock", () => {
  const app = readFileSync(new URL("../App.tsx", import.meta.url), "utf8")
  assert.equal((app.match(/useStale\(\)/g) ?? []).length, 2)            // freshness pill + stale banner
  for (const page of ["overview", "automation"]) {
    const src = readFileSync(new URL(`../pages/${page}.tsx`, import.meta.url), "utf8")
    assert.doesNotMatch(src, /useNow\("second"\)/, page)
  }
})
