// Navigation model: three places (founder decision 2026-10-20, "too complex"). Home answers "is the company fine and
// what needs me" (overview, approvals, business results); Work is what the agents do (tasks, agents, automation,
// activity); System is whether it can be relied on (status, incidents, memory). Every page is still there as a tab of
// its place: nothing was removed to make the Desk shorter. Every #/page route stays a deep link; retired routes
// resolve to their new home (ALIASES, nav.test.ts).

export const PAGES = ["overview", "approvals", "tasks", "agents", "projects", "project", "finance", "growth",
  "health", "incidents", "automation", "activity", "memory"] as const
export type Page = (typeof PAGES)[number]

/** Routes from earlier Desk versions (bookmarks, notification links) and where they live now. */
export const ALIASES: Record<string, Page> = { needs: "approvals", home: "overview", system: "health", work: "tasks" }

export function resolvePage(raw: string): Page | null {
  if ((PAGES as readonly string[]).includes(raw)) return raw as Page
  return ALIASES[raw] ?? null
}

export type SectionKey = "home" | "work" | "system"
/** `shortcut`: the second key of the "g <key>" go-to shortcut. */
export interface NavSection { key: SectionKey; label: string; home: Page; tabs: Page[]; shortcut: string }

export const SECTIONS: NavSection[] = [
  { key: "home", label: "Home", home: "overview", tabs: ["overview", "approvals", "projects", "finance", "growth"], shortcut: "h" },
  { key: "work", label: "Work", home: "tasks", tabs: ["tasks", "agents", "automation", "activity"], shortcut: "w" },
  { key: "system", label: "System", home: "health", tabs: ["health", "incidents", "memory"], shortcut: "s" },
]

export const PAGE_LABEL: Record<Page, string> = {
  overview: "Overview", approvals: "Approvals", tasks: "Tasks", agents: "Agents", projects: "Portfolio", project: "Project",
  finance: "Finance", growth: "Growth", health: "Status", incidents: "Incidents", automation: "Automation",
  activity: "Activity", memory: "Memory",
}

/** A page's name where its section is not around it (palette, page header). It matches the sidebar label, so the
 *  page a link opens carries the name that was clicked; only Status reads longer once out of its Trust section. */
export const pageTitle = (p: Page): string =>
  ({ health: "System status" } as Partial<Record<Page, string>>)[p] ?? PAGE_LABEL[p]

/** The section a route belongs to; a project detail page lives under Home, beside Portfolio. */
export function sectionOf(page: Page): NavSection {
  if (page === "project") return SECTIONS.find(s => s.key === "home")!
  return SECTIONS.find(s => s.home === page || s.tabs.includes(page)) ?? SECTIONS[0]
}

/** Every page a person can navigate to from the sidebar / section tabs (the project page is reached per unit). */
export const reachable = (): Page[] => [...new Set(SECTIONS.flatMap(s => [s.home, ...s.tabs]))]
