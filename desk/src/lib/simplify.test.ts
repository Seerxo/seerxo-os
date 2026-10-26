// The simplified Founder Desk (2026-10-21): one composer on Home, search kept apart from it, every route still
// rendered, and every decision still confirmed. Source-level checks in the style of rows.test.ts. `npm test`.
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import { PAGES } from "./nav.ts"

const read = (p: string) => readFileSync(new URL(p, import.meta.url), "utf8")
const app = read("../App.tsx"), composer = read("../components/command-composer.tsx")
const controls = read("../components/controls.tsx"), overview = read("../pages/overview.tsx")

test("search (⌘K) never submits a command: it only hands the text to the Home composer", () => {
  const group = app.slice(app.indexOf("function InstructGroup"), app.indexOf("\n}\n", app.indexOf("function InstructGroup")))
  assert.ok(group.length > 0)
  assert.doesNotMatch(group, /command\(/)
  assert.match(group, /setDraft\(cur => handOff\(cur, instruction\)\); go\("overview"\) \}\)/)   // behaviour: draft.test.ts
  // Focus moves to the composer only after the palette closed (no timer, no fight with the focus trap).
  assert.match(app, /run=\{fn => \{ handedOff\.current = true; run\(fn\) \}\}/)
  assert.match(app, /onCloseAutoFocus=\{closeFocus\}/)
  // No other palette path posts a command either: App.tsx never calls the command endpoint.
  assert.doesNotMatch(app, /\bcommand\(\{/)
  assert.doesNotMatch(app, /\/desk\/command/)
})

test("the composer sends only on an explicit submit, and says a command is recorded, not executed", () => {
  assert.match(composer, /onSubmit=\{e => \{ e\.preventDefault\(\); void send\(\) \}\}/)
  assert.match(composer, /e\.key === "Enter" && \(e\.metaKey \|\| e\.ctrlKey\)/)         // a bare Enter adds a newline
  assert.match(composer, /disabled=\{busy \|\| text\.length < 3\}/)
  assert.match(composer, /Nothing runs until an agent picks it up/)
  assert.match(composer, /if \(c\) setDraft\(cur => draftAfterSend\(cur, raw\)\)/)   // only after the API answered (behaviour: draft.test.ts)
  assert.match(composer, /commandLifecycle\(status\(c\.key\), true\)/)                    // progress is read from the task
  assert.match(composer, /\{c\.key\}/)                                                    // the real task key is shown
  assert.match(overview, /<CommandComposer \/>/)
})

test("every page still renders: each route has its own case in the page switch", () => {
  for (const p of PAGES.filter(p => p !== "overview")) assert.match(app, new RegExp(`case "${p}": return <`), p)
  assert.match(app, /default: return <Overview \/>/)
})

test("approvals keep their confirmations on Home and everywhere else", () => {
  assert.match(controls, /export const HIGH_RISK = \["HIGH", "CRITICAL"\]/)
  assert.match(controls, /HIGH_RISK\.includes\(a\.risk\) \? <HighRiskApprove/)
  assert.match(controls, /<Confirm title=\{`Reject \$\{a\.key\}\?`\}/)
  assert.match(controls, /<Confirm title=\{`Approve \$\{a\.key\}\?`\}/)
  // Home reuses the same control, never a bespoke unconfirmed button.
  assert.match(overview, /<ApprovalDecision a=\{a\} compact \/>/)
  assert.doesNotMatch(overview, /\/approvals\//)
})

test("lost-session paths hand the error to endSession, which applies draftAfterError (behaviour: draft.test.ts)", () => {
  const state = read("../state.tsx")
  const clear = state.slice(state.indexOf("const clearLiveData"), state.indexOf("const endSession"))
  assert.doesNotMatch(clear, /setDraft/)                                     // onOffline calls clearLiveData
  assert.match(state, /setDraft\(cur => draftAfterError\(e, cur\)\)\n\s+applyDialog\(d => dialogAfterError\(e, d\)\)/)
  assert.equal((state.match(/sessionLost\(e\)\) (\{ )?endSession\(e\)/g) ?? []).length, 3)
})

test("Home, the sidebar and System summarise the same checklist", () => {
  assert.match(overview, /healthSummary\(systemChecks\(d, now\)\)/)
  assert.match(app, /const checks = systemChecks\(data, now\), h = healthSummary\(checks\)/)
  assert.doesNotMatch(app, /vitals\(data, now\)/)
})

test("the full task dialog is read-only while recording, so nothing typed then can be dropped unsent", () => {
  const tasks = read("../pages/tasks.tsx")
  assert.match(tasks, /<fieldset disabled=\{busy\} className="grid min-w-0 gap-5">/)
  assert.match(tasks, /setDraft\(cur => homeAfterCreate\(cur, seed\.text, body\.text\)\)/)
})

test("the dialog cannot be dismissed while its request is in flight, and settles only its own send", () => {
  const tasks = read("../pages/tasks.tsx")
  assert.match(tasks, /showCloseButton=\{!busy\}/)
  assert.match(tasks, /onEscapeKeyDown=\{e => \{ if \(busy\) e\.preventDefault\(\) \}\} onInteractOutside=\{e => \{ if \(busy\) e\.preventDefault\(\) \}\}/)
  assert.match(tasks, /const key = startSend\(\)\n\s+if \(key === null\) return/)
  assert.match(tasks, /settleSend\(key, !!ok\)/)
})
