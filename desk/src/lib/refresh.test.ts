import assert from "node:assert/strict"
import { test } from "node:test"
import { coalesce, due, FAST_MS, SLOW_MS } from "./refresh.ts"

test("a refresh asked for during a running one is not dropped: it runs once right after", async () => {
  let calls = 0, release!: () => void
  const run = coalesce(() => { calls++; return calls === 1 ? new Promise<void>(r => { release = r }) : Promise.resolve() })
  const first = run()          // background poll in flight
  void run(); void run()       // the founder acts twice meanwhile
  assert.equal(calls, 1)
  release(); await first
  assert.equal(calls, 2)       // one follow-up read, never lost, never duplicated
})

test("polls fast just after an action, slow otherwise", () => {
  const now = 1_000_000
  assert.equal(due(now, now - FAST_MS, now + 1), true)
  assert.equal(due(now, now - FAST_MS, now - 1), false)
  assert.equal(due(now, now - SLOW_MS, 0), true)
})
