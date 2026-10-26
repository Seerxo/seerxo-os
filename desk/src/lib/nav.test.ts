// Three places (Home, Work, System; founder 2026-10-20); no destination is lost and old links still resolve. `npm test`.
import assert from "node:assert/strict"
import { test } from "node:test"
import { ALIASES, PAGES, SECTIONS, pageTitle, reachable, resolvePage, sectionOf } from "./nav.ts"

test("three places in the founder's order, each page reachable from exactly one", () => {
  assert.deepEqual(SECTIONS.map(s => s.label), ["Home", "Work", "System"])
  for (const p of PAGES.filter(p => p !== "project"))
    assert.equal(SECTIONS.filter(s => s.home === p || s.tabs.includes(p)).length, 1, p)
  assert.equal(reachable().length + 1, PAGES.length)   // all but the per-unit project page
})

test("retired routes resolve to their new home; unknown routes do not", () => {
  assert.equal(resolvePage("needs"), "approvals")
  assert.equal(resolvePage("tasks"), "tasks")
  assert.equal(resolvePage("nope"), null)
  for (const target of Object.values(ALIASES)) assert.ok(PAGES.includes(target))
})

test("go-to shortcuts are unique letters and titles stand alone", () => {
  assert.equal(new Set(SECTIONS.map(s => s.shortcut)).size, SECTIONS.length)
  for (const s of SECTIONS) assert.match(s.shortcut, /^[a-z]$/)
  for (const p of PAGES) assert.ok(pageTitle(p))
})

test("every page that used to be a sidebar link is still a tab of exactly one place", () => {
  for (const p of ["overview", "approvals", "tasks", "agents", "automation", "activity", "projects", "finance", "growth", "health", "incidents", "memory"] as const)
    assert.equal(SECTIONS.filter(s => s.tabs.includes(p)).length, 1, p)
  assert.equal(sectionOf("project").key, "home")      // a unit's page lives beside Portfolio
  assert.equal(sectionOf("approvals").key, "home")    // decisions stay on Home
})
