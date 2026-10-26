// Contract test: reconciler.contract.json holds real outputs of app/reconciler_heartbeat.py:view (the backend
// that builds DeskState.reconciler_execution) for every state, at a fixed clock. If the backend's shape or
// meaning changes, regenerate the fixture and this test shows what the Desk would now display.
//
// Regenerate from the repository root (Python env with requirements.txt): for each case, seed one
// ControlState(key=reconciler_heartbeat.KEY, value=<row>, updated_by="watchdog") into an in-memory SQLite DB
// (models.Base.metadata.create_all) and store reconciler_heartbeat.view(db, now=NOW) as "view", with
// NOW = 2026-10-09T12:00:00Z. The rows used (minutes before NOW):
//   NO_EVIDENCE: none · STARTED: attempt 2, completed 12 · UNFINISHED: STARTED attempt 14, completed 24 ·
//   RAISED_after_completion: attempt 3, completed 13, error OperationalError, goal_errors 0 ·
//   per_goal_FAILED: COMPLETED 3/3, created_task_count 1, goal_errors 2 · COMPLETED_unknown_tasks_0: COMPLETED
//   3/3, created_task_count 0, goal_errors 0 · COMPLETED_goals_OK: same plus per_goal_outcome "OK", goals_total 2 ·
//   STALE: COMPLETED 45/45, created_task_count 0 · MALFORMED_bad_time: attempt
//   "not-a-time" · MALFORMED_future: attempt NOW+5m · MALFORMED_completed_without_time: COMPLETED, attempt 3.
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import { reconcilerView } from "./reconciler.ts"

const fixture = JSON.parse(readFileSync(new URL("./reconciler.contract.json", import.meta.url), "utf8")) as {
  now: string; cases: Record<string, { view: unknown; expected_label: string }>
}
const NOW = Date.parse(fixture.now)

test("every real backend state maps to the label it proves, never to a healthier one", () => {
  const names = Object.keys(fixture.cases)
  for (const required of ["NO_EVIDENCE", "STARTED", "UNFINISHED", "RAISED_after_completion", "per_goal_FAILED",
    "COMPLETED_unknown_tasks_0", "STALE", "MALFORMED_bad_time", "MALFORMED_future", "MALFORMED_completed_without_time"]) {
    assert.ok(names.includes(required), `fixture lacks ${required}`)
  }
  for (const [name, { view, expected_label }] of Object.entries(fixture.cases)) {
    const r = reconcilerView(view, NOW)
    assert.equal(r.label, expected_label, name)
    assert.notEqual(r.tone, "success", name)                       // the backend never proves per-goal success
    assert.doesNotMatch(r.detail, /NaN|undefined|null/, name)
  }
})

test("the real backend's evidence for each state reads correctly", () => {
  const c = fixture.cases
  assert.match(reconcilerView(c.NO_EVIDENCE.view, NOW).detail, /No recorded invocation yet/)
  assert.match(reconcilerView(c.STARTED.view, NOW).detail, /in progress, started 2m ago/)
  assert.match(reconcilerView(c.UNFINISHED.view, NOW).detail, /did not complete · previous completion 24m ago/)
  assert.match(reconcilerView(c.RAISED_after_completion.view, NOW).detail, /raised OperationalError · previous completion 13m ago/)
  assert.match(reconcilerView(c.per_goal_FAILED.view, NOW).detail, /2 goal error\(s\) · 1 task\(s\) created/)
  assert.match(reconcilerView(c.COMPLETED_unknown_tasks_0.view, NOW).detail, /completed 3m ago · 0 task\(s\) created · per-goal outcome unknown/)
  assert.match(reconcilerView(c.COMPLETED_goals_OK.view, NOW).detail, /0 task\(s\) created · 2 active goal\(s\) examined without error/)
  assert.equal(reconcilerView(c.COMPLETED_goals_OK.view, NOW).tone, "info")       // examined is not "on track"
  assert.match(reconcilerView(c.STALE.view, NOW).detail, /stale: last attempt 45m ago, completed 45m ago/)
  for (const m of ["MALFORMED_bad_time", "MALFORMED_future", "MALFORMED_completed_without_time"]) {
    const r = reconcilerView(c[m].view, NOW)
    assert.equal(r.last, null, m)                                  // no fabricated timestamp
    assert.match(r.detail, /Malformed execution evidence/, m)
  }
  assert.equal(reconcilerView(c.STARTED.view, NOW).cadence, "Every 10 min")
})
