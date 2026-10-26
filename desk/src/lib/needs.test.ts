import assert from "node:assert/strict"
import { test } from "node:test"
import type { Need } from "./api.ts"
import { retryEventId } from "./needs-pure.ts"

const need = (over: Partial<Need>): Need => ({ kind: "SECURITY", rank: 1, level: "critical", title: "t", why: "w", affected: "watchdog",
  since: null, evidence: "event 42 · uncertain", action: "", target: { page: "activity" }, ...over })

test("only an unconfirmed delivery offers a retry, for exactly its event", () => {
  assert.equal(retryEventId(need({})), 42)
  assert.equal(retryEventId(need({ evidence: "event 7 · recovery delivery pending" })), 7)
  assert.equal(retryEventId(need({ kind: "STOPPED", evidence: "event 9 · route_failed" })), null)   // routing failure: not here
  assert.equal(retryEventId(need({ evidence: "TASK-0001 · BLOCKED" })), null)
  assert.equal(retryEventId(need({ target: { page: "incidents" } })), null)
})
