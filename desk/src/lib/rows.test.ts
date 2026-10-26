// Pins the mobile contract of dense tables: nothing is lost on a phone, and the pages cannot slide
// back to raw sideways-scrolling tables without a test noticing. Run with `npm test`.
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import { checkMemory } from "./validate.ts"
import { layout, order, rightAligned } from "./rows.ts"

test("every column the cards omit must say so; every other column reaches the cards", () => {
  const cols = [{ head: "Status" }, { head: "Revenue" }, { head: "Internal id", mobile: "hide" as const }]
  assert.deepEqual(layout(cols), { table: ["Status", "Revenue", "Internal id"], card: ["Status", "Revenue"] })
})

test("the card view is always a subset of the table view, in the same order", () => {
  const { table, card } = layout([{ head: "A" }, { head: "B", mobile: "hide" }, { head: "C" }])
  assert.deepEqual(card, table.filter(h => card.includes(h)))
})

test("no columns means no fields, not a crash", () => {
  assert.deepEqual(layout([]), { table: [], card: [] })
})

// The point of the change: Finance and Automation render dense data through DataRows (table on
// desktop, cards below `sm`), never through a bare <Table> that scrolls sideways on a phone.
for (const page of ["finance", "automation", "projects"]) {
  test(`${page} renders its tables through DataRows`, () => {
    const src = readFileSync(new URL(`../pages/${page}.tsx`, import.meta.url), "utf8")
    assert.match(src, /<DataRows\b/)
    assert.doesNotMatch(src, /<Table\b/, `${page}.tsx must not render a bare <Table>`)
  })
}

test("the card list is labelled, and a clickable card is a real button (keyboard reachable)", () => {
  const src = readFileSync(new URL("../components/data-rows.tsx", import.meta.url), "utf8")
  assert.match(src, /<ul aria-label=\{label\}/)
  assert.match(src, /<button type="button"/)
  assert.match(src, /tabIndex=\{act \? 0 : undefined\}/)   // table rows are keyboard reachable too
  assert.match(src, /onKeyDown/)
})

test("a right-aligned column gets a right-aligned header; look-alike classes do not", () => {
  assert.equal(rightAligned({ className: "num text-right" }), true)
  assert.equal(rightAligned({ className: "text-right text-xs text-muted-foreground" }), true)
  assert.equal(rightAligned({ className: "text-sm" }), false)
  assert.equal(rightAligned({ className: "context-right" }), false)
  assert.equal(rightAligned({}), false)
  assert.equal(rightAligned({}, true), true)   // the aside (state) column is always right-aligned
})

// Regression (project page at 320 px, long goal titles): the row list is a CSS grid whose implicit
// column is `auto`, so a title's max-content width inflated the whole list to 1053 px even though the
// title had `min-w-0 truncate`. Measured fix: an explicit `minmax(0, 1fr)` column (Tailwind grid-cols-1).
test("goal and project lists give their rows a shrinkable column, and let long titles wrap", () => {
  const src = readFileSync(new URL("../pages/projects.tsx", import.meta.url), "utf8")
  const unitGoals = src.slice(src.indexOf("function UnitGoals"))
  for (const label of ["Goals", "Projects"]) {
    assert.match(unitGoals, new RegExp(`<ul className="grid grid-cols-1[^"]*" aria-label="${label}">`), `${label} list needs grid-cols-1`)
  }
  assert.doesNotMatch(unitGoals, /grid divide-y/, "a bare `grid divide-y` list is the overflow bug")
  assert.match(unitGoals, /min-w-0 break-words">\{g\.title\}/)      // title wraps, the status badge keeps its place
})

test("aside is last by default and right after the title with asideFirst; never duplicated or dropped", () => {
  const cols = ["Current work", "Reports to", "Last run", "Last success"]
  assert.deepEqual(order(cols, "Status"), [...cols, "Status"])
  assert.deepEqual(order(cols, "Status", true), ["Status", ...cols])      // Agent, Status, Current work, ...
  assert.deepEqual(order(cols, undefined, true), cols)
  for (const first of [false, true]) assert.equal(order(cols, "Status", first).filter(c => c === "Status").length, 1)
})

test("agents use responsive, keyboard-reachable lanes with verified runtime facts", () => {
  const src = readFileSync(new URL("../pages/agents.tsx", import.meta.url), "utf8")
  // One lane per agent: stacked on phones, four columns (identity, current work, workload, timing) from lg.
  assert.match(src, /lg:grid-cols-\[minmax\(0,13rem\)_minmax\(0,1fr\)_minmax\(0,15rem\)_minmax\(0,9rem\)\]/)
  assert.match(src, /<button type="button" onClick=\{\(\) => open\(\{ kind: "agent", key: agent\.key \}\)\}/)
  for (const fact of ["Current work", "Reports to", "Last run", "Last success"]) assert.match(src, new RegExp(fact))
  // Disabled and unconfirmed-running are decided in lib/agent-state.ts (agent-state.test.ts), never from status alone.
  assert.match(src, /agentState\(agent, useData\(\), useNow\("minute"\)\)/)
  assert.match(src, /focus-visible:ring-2 focus-visible:ring-ring/)
  // Agents holding work or a failure stay in view; calm agents fold under one line, still one click away.
  assert.match(src, /<section aria-label=\{label\}/)
  assert.match(src, /lanes\(busy, "Agents with work"\)/)
  assert.match(src, /lanes\(quiet, "Agents with nothing open"\)/)
  // Blocked, failed and stalled are counted together and shown as danger; zero running is never a health claim.
  assert.match(src, /value: String\(n\("BLOCKED"\) \+ n\("FAILED"\) \+ n\("STALLED"\)\)/)
  assert.match(src, /none executing; not a health signal/)
})

// `break-words` (overflow-wrap:break-word) does NOT reduce min-content, so one unbroken 70+ character
// name still widened the page (Agents, 627 px at 320). `anywhere` does. Pinned on the shared component.
test("DataRows wraps unbroken strings with overflow-wrap:anywhere, not break-words", () => {
  const rows = readFileSync(new URL("../components/data-rows.tsx", import.meta.url), "utf8")
  assert.doesNotMatch(rows, /break-words/)
  assert.match(rows, /\[overflow-wrap:anywhere\]/)
  assert.match(rows, /<ul aria-label=\{label\} className="grid grid-cols-1[^"]*lg:hidden">/)
  assert.match(rows, /className="surface-panel grid gap-2\.5 p-3\.5 sm:p-4"/)
  // TableCell is whitespace-nowrap by default, which defeats overflow-wrap: data cells must opt out.
  assert.match(rows, /whitespace-normal \[overflow-wrap:anywhere\]/)
})

// Health and Incidents (measured with long unbroken strings: Health 630 px, Incidents 1095 px at 320).
// The same two causes as the other pages: implicit `auto` grid columns and non-shrinking text.
test("Health, Incidents and Facts wrap unbroken strings and give grids a shrinkable column", () => {
  const read = (p: string) => readFileSync(new URL(p, import.meta.url), "utf8")
  const system = read("../pages/system.tsx"), control = read("../pages/control.tsx"), desk = read("../components/desk.tsx")
  assert.match(system, /min-w-0 flex-1 \[overflow-wrap:anywhere\]/)
  assert.doesNotMatch(system, /truncate text-xs text-muted-foreground">\{sub\}/, "service evidence must not be truncated")
  assert.match(system, /grid grid-cols-1 gap-10 lg:grid-cols-/)
  assert.match(control, /grid grid-cols-1 gap-4", d\.incidents\.length > 1 \? "xl:grid-cols-2"/)
  assert.match(control, /<article [^>]*min-w-0[^>]*\[overflow-wrap:anywhere\]/)
  assert.match(desk, /minmax\(0,1fr\)\] gap-x-6/)
  assert.doesNotMatch(desk, /break-words/)
})

// Measured with real Tab key events (CDP): agent card buttons and list rows showed only an underline
// or a faint background on focus. A ring is the visible indicator (WCAG 2.4.7).
test("keyboard focus is a visible ring on card buttons and list rows, not only an underline or tint", () => {
  const read = (p: string) => readFileSync(new URL(p, import.meta.url), "utf8")
  assert.match(read("../components/data-rows.tsx"), /focus-visible:ring-2 focus-visible:ring-ring\/60/)
  assert.match(read("../components/desk.tsx"), /focus-visible:bg-muted\/60 focus-visible:ring-2/)
})

test("primary navigation targets are at least 44 px tall on touch screens and the section action wraps", () => {
  const read = (p: string) => readFileSync(new URL(p, import.meta.url), "utf8")
  const app = read("../App.tsx"), css = read("../index.css")
  assert.match(app, /relative flex h-11 items-center gap-1\.5 text-\[13px\]/)          // section tabs: 44 px
  assert.match(app, /<a href=\{`#\/\$\{sec\.home\}`\} onClick=\{\(\) => setOpenMobile\(false\)\}/)    // sidebar places are real links
  assert.match(app, /relative flex h-\[58px\] w-full flex-col/)                         // bottom nav targets
  assert.match(app, /fixed inset-x-0 bottom-0 z-30[^"]*lg:hidden/)                        // bottom nav remains through tablet
  // Dense desktop rows (40 px) grow to 44 px wherever the pointer is coarse, including the sidebar's sub-page links.
  const coarse = css.slice(css.indexOf("@media (pointer: coarse)"))
  assert.match(coarse, /button, \[role="tab"\][^{]*\[data-sidebar="menu-button"\][^{]*\{ min-height: 2\.75rem; \}/)
  assert.match(read("../components/desk.tsx"), /max-w-\[65%\] min-w-0 flex-wrap/)
})

test("Growth shows Seerxo funnel readings beside Polar subscriptions without inventing conversion", () => {
  const growth = readFileSync(new URL("../pages/growth.tsx", import.meta.url), "utf8")
  assert.match(growth, /function SeerxoFunnel/)
  assert.match(growth, /"seerxo-backend"/)
  assert.match(growth, /label="Quota hits · 7d"/)
  assert.match(growth, /label="Checkout started · 7d"/)
  assert.match(growth, /label="New signups · not tried yet"/)   // derived only from the same signups population          // a started checkout, never presented as a purchase
  assert.match(growth, /<SeerxoFunnel u=\{u\} \/><Subscriptions u=\{u\} \/>/)
  assert.match(growth, /Free → paid conversion is not derived until matching cohort data exists/)
  assert.doesNotMatch(growth, /Signups, activation and free → paid conversion are not connected/)
})

test("a tab panel that takes keyboard focus shows it (Memory: focus landed on the panel with no indicator)", () => {
  const tabs = readFileSync(new URL("../components/ui/tabs.tsx", import.meta.url), "utf8")
  assert.match(tabs, /text-sm outline-none focus-visible:ring-2/)
})

test("a malformed /desk/memory response is rejected, never rendered as empty memory", () => {
  const ok = { decisions: [], learnings: [], notes: [], permanent: null }
  assert.deepEqual(checkMemory(ok), ok)
  assert.equal(checkMemory({ ...ok, permanent: undefined }).permanent, null)
  for (const bad of [null, [], "x", {}, { ...ok, notes: undefined }, { ...ok, decisions: {} }, { ...ok, permanent: 3 }]) {
    assert.throws(() => checkMemory(bad), /Malformed response/)
  }
})

test("Work never scrolls sideways: only the filter rail scrolls, and it is the marked phone scroller", () => {
  const tasks = readFileSync(new URL("../pages/tasks.tsx", import.meta.url), "utf8")
  assert.match(tasks, /className="task-filters-scroll /)
  assert.doesNotMatch(tasks, /auto-cols-|grid-flow-col|overflow-x-auto/)
})

test("a long current task wraps in full on agent cards; it is never truncated", () => {
  const agents = readFileSync(new URL("../pages/agents.tsx", import.meta.url), "utf8")
  const task = agents.slice(agents.indexOf("{/* current work */}"), agents.indexOf("{/* workload */}"))
  assert.ok(task.length > 50)
  assert.match(task, /\[overflow-wrap:anywhere\]/)
  assert.doesNotMatch(task, /truncate|line-clamp/)
})

test("the Overview headline comes from lib/operating, never from the API's optimistic system_health", () => {
  // overview.system_health is HEALTHY unless a SEV0/1 or failing service exists; it ignores a stopped worker and backups.
  const overview = readFileSync(new URL("../pages/overview.tsx", import.meta.url), "utf8")
  assert.match(overview, /operating\(d, \{ stale, now \}\)/)
  assert.doesNotMatch(overview, /system_health/)
})

test("work rows say what happens next and, for stopped work, the recorded reason", () => {
  const tasks = readFileSync(new URL("../pages/tasks.tsx", import.meta.url), "utf8")
  assert.match(tasks, /const reasons = useReasons\(d\)/)
  assert.match(tasks, /nextLine\(t, \{ reason, paused \}\)/)
  assert.match(tasks, /flex min-w-0 items-center gap-2/)   // title and its priority/risk tags stay together
})

test("portfolio cards show reported metrics and freshness without substituting zero", () => {
  const projects = readFileSync(new URL("../pages/projects.tsx", import.meta.url), "utf8")
  assert.match(projects, /grid grid-cols-1 gap-3 sm:grid-cols-2 lg:hidden/)
  assert.match(projects, /No verified metrics reported/)
  assert.match(projects, /u\.latest_at \? `\$\{ago\(u\.latest_at\)\} ago` : "No observation"/)
  assert.match(projects, /nums\.map\(n => <span key=\{n\.text\}/)
  assert.match(projects, /dataSummary\(u\)/)
  assert.doesNotMatch(projects, /nums\[0\]\.text/)
  assert.doesNotMatch(projects, /value=\{0\}/)
})

test("PageHeader typography has one source of truth in the responsive design tokens", () => {
  const component = readFileSync(new URL("../components/desk.tsx", import.meta.url), "utf8")
  const css = readFileSync(new URL("../index.css", import.meta.url), "utf8")
  assert.match(component, /<h1>\{title\}<\/h1>/)
  assert.match(component, /<p className="text-muted-foreground">\{description\}<\/p>/)
  assert.match(css, /\.page-heading h1 \{ font-size: clamp\(1\.75rem, 2\.4vw, 2rem\);/)
  assert.match(css, /\.page-heading p \{ max-width: 46rem; font-size: \.875rem; line-height: 1\.55; \}/)
})

test("state-opened dialogs (palette, create task, detail sheet) hand focus back through useReturnFocus", () => {
  for (const f of ["../App.tsx", "../pages/tasks.tsx", "../components/detail.tsx"]) {
    const src = readFileSync(new URL(f, import.meta.url), "utf8")
    assert.match(src, /useReturnFocus\(\)/, f)
    // The palette routes through closeFocus, which falls back to returnFocus unless it handed text to the composer.
    if (f === "../App.tsx") {
      assert.match(src, /onCloseAutoFocus=\{closeFocus\}/)
      assert.match(src, /e\.preventDefault\(\); focusDraft\(\) \} else returnFocus\(e\)/)
    } else assert.match(src, /onCloseAutoFocus=\{returnFocus\}/, f)
  }
})

// Live data (2026-10-16): long unbroken task text widened the Overview past a 375 px phone. The page clipped it,
// so scrollWidth checks passed while the figures and rows were cut off. Grid items need an explicit shrinkable column.
test("Overview and metric trends cannot be widened by long unbroken text", () => {
  const overview = readFileSync(new URL("../pages/overview.tsx", import.meta.url), "utf8")
  const trend = readFileSync(new URL("../components/trend.tsx", import.meta.url), "utf8")
  assert.match(overview, /<div className="grid min-w-0 grid-cols-1 gap-6 lg:gap-8">/)
  assert.match(overview, /<header aria-labelledby="operating-state" className="flex min-w-0 /)
  assert.doesNotMatch(overview, /shrink-0 text-warning">· no open work serves it/)
  assert.match(trend, /<figcaption className="flex min-w-0 /)
})

// Live data (2026-10-16): the agent sheet's content grew to 810 px inside a 375 px phone and was clipped.
test("task and agent sheets give their content an explicit shrinkable column", () => {
  const detail = readFileSync(new URL("../components/detail.tsx", import.meta.url), "utf8")
  assert.equal(detail.match(/<div className="grid min-w-0 grid-cols-1 gap-[56]">/g)?.length, 2)
  assert.match(detail, /whitespace-pre-wrap \[overflow-wrap:anywhere\]">\{children\}<\/pre>/)
  assert.match(detail, /<section className="grid min-w-0 grid-cols-1 gap-2">/)
})

// Live data (2026-10-16): 27 tasks in review made the Work page 3,700 px and buried Up next and Waiting outside.
test("Work collapses groups in overview filters, but never Needs attention, and a search opens what it finds", () => {
  const tasks = readFileSync(new URL("../pages/tasks.tsx", import.meta.url), "utf8")
  assert.match(tasks, /const open = isOpen\(g\.key, g\.tasks, \{ overview, searching, manual: manual\[g\.key\] \}\)/)
  assert.match(tasks, /const searching = q !== "" \|\| unit !== "all"/)
  assert.match(tasks, /aria-expanded=\{open\} disabled=\{g\.key === "attention"\}/)
  assert.doesNotMatch(tasks, /slice\(0, CAP\)/)                      // nothing is cut: a closed group opens in full
})
