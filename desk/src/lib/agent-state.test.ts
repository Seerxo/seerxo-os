import assert from "node:assert/strict"
import { test } from "node:test"
import type { DeskState } from "./api.ts"
import { agentState } from "./agent-state.ts"

const NOW = Date.parse("2026-10-20T12:00:00Z")
const d = (worker: unknown) => ({ worker } as unknown as DeskState)

test("RUNNING is executing only while the worker is heard from", () => {
  assert.equal(agentState({ status: "RUNNING" }, d({ status: "HEALTHY", heartbeat_at: "2026-10-20T11:59:30Z" }), NOW).running, true)
  for (const w of [{ status: "HEALTHY", heartbeat_at: "2026-10-20T11:30:00Z" }, { status: "STALE", heartbeat_at: "2026-10-20T11:59:30Z" },
    { status: "NO_DATA" }, undefined]) {
    const s = agentState({ status: "RUNNING" }, d(w), NOW)
    assert.equal(s.running, false); assert.equal(s.label, "Running · not confirmed")
  }
})

test("a disabled agent is disabled whatever its status; other statuses pass through", () => {
  assert.equal(agentState({ status: "RUNNING", enabled: false }, d(undefined), NOW).value, "DISABLED")
  assert.equal(agentState({ status: "BLOCKED" }, d(undefined), NOW).value, "BLOCKED")
})

test("only a calm agent with nothing held, open or failed folds away; unknown and unconfirmed stay visible", async () => {
  const { foldable } = await import("./agent-state.ts")
  const heard = d({ status: "HEALTHY", heartbeat_at: "2026-10-20T11:59:30Z" }), silent = d(undefined)
  const idle = { status: "IDLE", current_task: null, last_error: null }
  assert.equal(foldable(idle, heard, NOW, 0), true)
  assert.equal(foldable({ ...idle, status: "ACTIVE" }, heard, NOW, 0), true)
  assert.equal(foldable(idle, heard, NOW, 1), false)                                       // open work
  assert.equal(foldable({ ...idle, last_error: "quota" }, heard, NOW, 0), false)          // recorded failure
  assert.equal(foldable({ ...idle, current_task: { key: "T", title: "t", status: "READY" } } as never, heard, NOW, 0), false)
  for (const status of ["RUNNING", "BLOCKED", "STALLED", "WAITING", "UNKNOWN", "NO_DATA"])
    assert.equal(foldable({ ...idle, status }, silent, NOW, 0), false, status)            // incl. RUNNING unconfirmed
  assert.equal(foldable({ ...idle, enabled: false }, heard, NOW, 0), false)                // disabled stays visible
})
