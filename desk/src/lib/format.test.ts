import assert from "node:assert/strict"
import { test } from "node:test"
import { capacity, humanizeTokens } from "./format.ts"

test("provider capacity does not label stored credentials as verified model access", () => {
  assert.deepEqual(capacity("codex", { providers: { codex: "CREDENTIALS_CONFIGURED" } }), {
    health: "CREDENTIALS_CONFIGURED",
    text: "Credentials configured · live access unverified",
  })
})

test("status tokens read as words while identifiers keep their form", () => {
  assert.equal(humanizeTokens("TASK-0033 · WAITING_EXTERNAL"), "TASK-0033 · Waiting on external")
  assert.equal(humanizeTokens("INC-0001 · INVESTIGATING"), "INC-0001 · Investigating")
  assert.equal(humanizeTokens("Seerxo · OPEN"), "Seerxo · Open")
})
