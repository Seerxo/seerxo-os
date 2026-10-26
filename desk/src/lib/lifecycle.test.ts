// A command's feedback never claims more than the task's status shows. Run with `npm test`.
import assert from "node:assert/strict"
import { test } from "node:test"
import { TRACK, commandLifecycle, trackIndex } from "./lifecycle.ts"
import { PAGES, PAGE_LABEL, SECTIONS, sectionOf } from "./nav.ts"

test("accepted is not executed: a READY task reads queued, not running or done", () => {
  const r = commandLifecycle("READY", true)
  assert.equal(r.step, "queued")
  assert.match(r.detail, /no agent has started/)
  assert.notEqual(r.tone, "success")
})

test("each step is claimed only by its own status, and nothing reads as success", () => {
  assert.equal(commandLifecycle("IN_PROGRESS", true).step, "running")
  assert.equal(commandLifecycle("REVIEW", true).step, "finished")
  assert.match(commandLifecycle("REVIEW", true).detail, /not accepted until reviewed/)
  assert.equal(commandLifecycle("DONE", true).step, "done")
  for (const s of ["READY", "IN_PROGRESS", "REVIEW", "DONE", "FAILED", "BLOCKED", "CANCELLED", "X"])
    assert.notEqual(commandLifecycle(s, true).tone, "success", s)          // the list holds no verified outcome
})

test("the command track never places a stopped or unknown command on it, and never rounds up", () => {
  assert.equal(trackIndex(commandLifecycle("READY", true).step), 1)          // queued, not executing
  assert.equal(trackIndex(commandLifecycle("IN_PROGRESS", true).step), 2)
  assert.equal(trackIndex(commandLifecycle("REVIEW", true).step), 3)         // finished is review, not done
  assert.equal(trackIndex(commandLifecycle("DONE", true).step), 4)
  assert.equal(trackIndex(commandLifecycle(undefined, false).step), 0)
  for (const s of [undefined, "FAILED", "BLOCKED", "CANCELLED", "WAITING_APPROVAL", "X"])
    assert.equal(trackIndex(commandLifecycle(s, true).step), null, String(s))
  assert.ok(!TRACK.some(t => /verif/i.test(t)))                              // verification is never claimed from the list
})

test("a task missing from the list is unknown, never assumed queued or done", () => {
  assert.equal(commandLifecycle(undefined, true).step, "unknown")
  assert.equal(commandLifecycle("SOMETHING_NEW", true).step, "unknown")
  assert.equal(commandLifecycle("FAILED", true).tone, "danger")
})

test("every route belongs to a section and every section tab is a real route", () => {
  for (const p of PAGES) assert.ok(sectionOf(p), p)
  for (const s of SECTIONS) for (const p of [s.home, ...s.tabs]) assert.ok(PAGES.includes(p), `${s.key}:${p}`)
  // Every page except the project detail page is reachable from a section, so nothing is orphaned.
  const reachable = new Set(SECTIONS.flatMap(s => [s.home, ...s.tabs]))
  assert.deepEqual(PAGES.filter(p => !reachable.has(p)), ["project"])
  assert.equal(sectionOf("project").key, "home")
  assert.equal(SECTIONS.length, 3)
  for (const p of PAGES) assert.ok(PAGE_LABEL[p], p)
})
