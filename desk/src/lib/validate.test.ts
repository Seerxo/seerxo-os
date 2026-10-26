// Malformed API responses are rejected with a named field (so the UI can show a recoverable error),
// and a good response after a bad one is accepted again. Run with `npm test`.
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import { checkAgentDetail, checkMemory, checkTaskDetail } from "./validate.ts"

const task = () => ({ key: "TASK-1", title: "t", description: null, objective: null, status: "READY", priority: "P2", risk: "LOW",
  assigned_agent: null, project: null, business_unit: null, success_criteria: null, context: null, approval_required: false,
  dependencies: [{ key: "TASK-0", status: "DONE" }], runs: [{ id: 1, task: "TASK-1", status: "OK", provider: null, started_at: "2026-01-01T00:00:00Z", ended_at: null, error: null, supervisor_verdict: null, supervisor_notes: null }],
  artifacts: null, result: null, error: null, supervisor: null, created_at: "2026-01-01T00:00:00Z", started_at: null, completed_at: null })
const agent = () => ({ key: "ceo", name: "CEO", role: "r", status: "IDLE", current_task: null, last_run: null, last_success: null, last_error: null,
  identity: { description: "d", capabilities: ["c"], skills: [], allowed_tools: [] }, schedule: null, triggers: [{ type: "event", event_type: "x" }], memory_policy: null,
  recent_tasks: [{ key: "TASK-1", title: "t", status: "READY" }], recent_runs: [], recent_learnings: [{ title: "a", lesson: "b" }],
  notebook: [{ note: "n", refs: 1, last_seen_at: "2026-01-01T00:00:00Z" }] })
const memory = () => ({ decisions: [{ key: "D1", title: "t", decision: "d", reason: null, status: "ACTIVE", approved_by: null, created_at: "2026-01-01" }],
  learnings: [{ id: 1, title: "t", lesson: "l", tags: ["a"], created_at: "2026-01-01" }],
  notes: [{ note: "n", refs: 2, last_seen_at: "2026-01-01", promoted: false }], permanent: null })

test("well-formed responses pass unchanged in meaning", () => {
  assert.equal(checkTaskDetail(task(), "TASK-1").runs.length, 1)
  assert.equal(checkAgentDetail(agent(), "ceo").identity.capabilities[0], "c")
  assert.equal(checkMemory(memory()).notes[0].refs, 2)
  assert.equal(checkTaskDetail({ ...task(), origin: { by: "ceo", automatic: true, trigger: null }, supervisor: { verdict: "OK", notes: null } }, "TASK-1").supervisor?.verdict, "OK")
})

test("task detail: null, wrong type, wrong key and nested defects are rejected with the field name", () => {
  for (const [name, raw, at] of [
    ["null", null, /task/], ["list", [], /task/], ["empty", {}, /task\.key/],
    ["other key", { ...task(), key: "TASK-9" }, /task\.key/],
    ["no title", { ...task(), title: undefined }, /task\.title/], ["numeric status", { ...task(), status: 3 }, /task\.status/],
    ["deps not list", { ...task(), dependencies: null }, /task\.dependencies/], ["dep without key", { ...task(), dependencies: [{ status: "x" }] }, /dependencies\[0\]\.key/],
    ["runs not list", { ...task(), runs: {} }, /task\.runs/], ["run without id", { ...task(), runs: [{ status: "OK", started_at: "x" }] }, /runs\[0\]\.id/],
    ["approval string", { ...task(), approval_required: "no" }, /approval_required/], ["origin wrong", { ...task(), origin: { by: 1 } }, /origin\.by/],
  ] as const) assert.throws(() => checkTaskDetail(raw, "TASK-1"), at, name)
})

test("agent detail: null, missing identity and nested defects are rejected", () => {
  for (const [name, raw, at] of [
    ["null", null, /agent/], ["empty", {}, /agent\.key/], ["other key", { ...agent(), key: "x" }, /agent\.key/],
    ["no identity", { ...agent(), identity: undefined }, /agent\.identity/], ["skills not list", { ...agent(), identity: { ...agent().identity, skills: "a" } }, /skills/],
    ["triggers null", { ...agent(), triggers: null }, /agent\.triggers/], ["trigger no type", { ...agent(), triggers: [{}] }, /triggers\[0\]\.type/],
    ["recent_tasks missing", { ...agent(), recent_tasks: undefined }, /recent_tasks/], ["learning bad", { ...agent(), recent_learnings: [{ title: "a" }] }, /lesson/],
    ["notebook refs", { ...agent(), notebook: [{ note: "n", refs: "1", last_seen_at: "x" }] }, /refs/], ["current_task bad", { ...agent(), current_task: { key: "k" } }, /current_task\.title/],
  ] as const) assert.throws(() => checkAgentDetail(raw, "ceo"), at, name)
})

test("memory: nested records are validated, not only the top-level lists", () => {
  for (const [name, raw, at] of [
    ["decision without title", { ...memory(), decisions: [{ key: "D1" }] }, /decisions\[0\]\.title/],
    ["learning id string", { ...memory(), learnings: [{ ...memory().learnings[0], id: "1" }] }, /learnings\[0\]\.id/],
    ["learning null item", { ...memory(), learnings: [null] }, /learnings\[0\]/],
    ["tags numbers", { ...memory(), learnings: [{ ...memory().learnings[0], tags: [1] }] }, /tags\[0\]/],
    ["note refs missing", { ...memory(), notes: [{ note: "n", last_seen_at: "x", promoted: false }] }, /notes\[0\]\.refs/],
    ["note promoted string", { ...memory(), notes: [{ ...memory().notes[0], promoted: "no" }] }, /promoted/],
    ["permanent number", { ...memory(), permanent: 3 }, /permanent/],
  ] as const) assert.throws(() => checkMemory(raw), at, name)
  assert.equal(checkMemory({ ...memory(), learnings: [{ ...memory().learnings[0], tags: null }] }).learnings[0].tags, null)

test("memory: note tags are accepted as the stored string the API returns, or as a list", () => {
  const withTags = (tags: unknown) => ({ ...memory(), notes: [{ ...memory().notes[0], tags }] })
  assert.equal(checkMemory(withTags("a, b")).notes[0].tags, "a, b")
  assert.deepEqual(checkMemory(withTags(["a"])).notes[0].tags, ["a"])
  assert.equal(checkMemory(withTags(null)).notes[0].tags, null)
  assert.throws(() => checkMemory(withTags(3)), /tags/)
})
})

test("recovery: a good response is accepted after any number of bad ones (the checks keep no state)", () => {
  for (let i = 0; i < 3; i++) {
    assert.throws(() => checkTaskDetail({}, "TASK-1"))
    assert.equal(checkTaskDetail(task(), "TASK-1").key, "TASK-1")
    assert.throws(() => checkAgentDetail(null, "ceo"))
    assert.equal(checkAgentDetail(agent(), "ceo").key, "ceo")
  }
})

test("the UI validates before rendering and keeps an error boundary as the last layer", () => {
  const read = (f: string) => readFileSync(new URL(f, import.meta.url), "utf8")
  const detail = read("../components/detail.tsx")
  assert.match(detail, /checkTaskDetail\(v, key\)/)
  assert.match(detail, /checkAgentDetail\(v, key\)/)
  assert.match(detail, /setAttempt\(n => n \+ 1\)/)  // Try again re-fetches
  assert.match(detail, /<ErrorBoundary label="These details" onRetry=\{\(\) => setAttempt/)
  const app = read("../App.tsx")
  assert.match(app, /<ErrorBoundary label="This page" resetKey=\{JSON\.stringify\(route\)\}>/)
  assert.match(app, /<ErrorBoundary label="The Founder Desk">/)
  assert.match(read("../components/error-boundary.tsx"), /getDerivedStateFromError/)
  assert.match(read("../pages/memory.tsx"), /checkMemory/)
})
