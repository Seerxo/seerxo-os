import { useEffect, useRef, useState } from "react"
import {
  Activity, AlertOctagon, Bot, Brain, Briefcase, CheckSquare, ChevronRight, CornerDownLeft, FileText, Gauge, HeartPulse, House,
  KanbanSquare, Keyboard, Layers, ListPlus, Monitor, Moon, Plus, Repeat, RefreshCw, Search, Send, ShieldCheck, Sparkles, Sun, TrendingUp,
  Wallet, WifiOff,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { ago } from "@/lib/format"
import { healthSummary, systemChecks } from "@/lib/operating"
import { useTheme, type Theme } from "@/lib/theme"
import { PAGE_LABEL, SECTIONS, pageTitle, sectionOf, type SectionKey } from "@/lib/nav"
import { commandLifecycle } from "@/lib/lifecycle"
import { ErrorBoundary } from "@/components/error-boundary"
import { useReturnFocus } from "@/lib/return-focus"
import { DeskProvider, useDesk, useDraft, useLink, useNow, useStale, type Page } from "@/state"
import { handOff } from "@/lib/draft"
import { CommandTrack, DOT, Dot, Status } from "@/components/desk"
import { DetailSheet } from "@/components/detail"
import { CreateTask } from "@/pages/tasks"
import { Button } from "@/components/ui/button"
import { Kbd } from "@/components/ui/kbd"
import { Skeleton } from "@/components/ui/skeleton"
import { Toaster } from "@/components/ui/sonner"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import { Command, CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList, CommandShortcut } from "@/components/ui/command"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import {
  Sidebar, SidebarContent, SidebarFooter, SidebarHeader, SidebarInset, SidebarMenu, SidebarMenuBadge, SidebarMenuButton, SidebarMenuItem,
  SidebarProvider, SidebarTrigger, useSidebar,
} from "@/components/ui/sidebar"
import { Overview } from "@/pages/overview"
import { ApprovalsPage } from "@/pages/approvals"
import { ProjectDetail, ProjectsPage } from "@/pages/projects"
import { TasksPage } from "@/pages/tasks"
import { FinancePage } from "@/pages/finance"
import { GrowthPage } from "@/pages/growth"
import { AutomationPage } from "@/pages/automation"
import { AgentsPage } from "@/pages/agents"
import { IncidentsPage } from "@/pages/control"
import { ActivityPage, HealthPage } from "@/pages/system"
import { MemoryPage } from "@/pages/memory"

const SECTION_ICON: Record<SectionKey, typeof Gauge> = { home: House, work: Layers, system: Gauge }
const PAGE_ICON: Record<Page, typeof Gauge> = {
  overview: House, approvals: CheckSquare, projects: Briefcase, project: Briefcase, growth: TrendingUp, finance: Wallet,
  tasks: KanbanSquare, agents: Bot, automation: Repeat, incidents: AlertOctagon, activity: Activity, health: HeartPulse, memory: Brain,
}
const isMac = /Mac|iPhone|iPad/.test(navigator.platform)
const MOD = isMac ? "⌘" : "Ctrl"

/** Badges from the read model: Approvals counts everything that needs the founder (critical in red), Work counts stuck
 *  tasks. A count the API did not report stays absent; an unreported attention queue shows "?". */
function useCounts() {
  const { data } = useDesk()
  const needs = Array.isArray(data?.needs_you) ? data!.needs_you!.length : undefined
  const critical = !!data?.needs_you?.some(n => n.level === "critical")
  const stuck = data?.tasks.filter(t => ["BLOCKED", "FAILED"].includes(t.status)).length
  const tab: Partial<Record<Page, number | undefined>> = { incidents: data?.incidents.length }
  const section: Partial<Record<SectionKey, [number | string | undefined, boolean, string]>> = {
    home: [needs ?? (data ? "?" : undefined), critical, "need you"],
    work: [stuck || undefined, false, "stuck"],
    system: [data?.incidents.length || undefined, false, "open incidents"],
  }
  // Inside a place, the tab that holds them carries the same count.
  if (needs) tab.approvals = needs
  return { tab, section }
}

/** The mark: an ink square with one Iris point, the accent's only decorative use. */
function Mark({ className }: { className?: string }) {
  return (
    <span aria-hidden className={cn("relative grid size-7 shrink-0 place-items-center rounded-[8px] bg-foreground text-[12px] font-bold tracking-tight text-background", className)}>
      S<span className="absolute -top-0.5 -right-0.5 size-2 rounded-full bg-brand ring-2 ring-sidebar" />
    </span>
  )
}

function AppSidebar({ onCreate }: { onCreate: () => void }) {
  const { data, route, go } = useDesk()
  const { setOpenMobile } = useSidebar()
  const { section: count } = useCounts()
  const now = useNow("minute")
  const nav = (page: Page) => { go(page); setOpenMobile(false) }
  return (
    <Sidebar collapsible="offcanvas">
      <SidebarHeader className="gap-4 px-3 pt-4 pb-2">
        <div className="flex items-center gap-2.5 px-1.5">
          <Mark />
          <div className="min-w-0 leading-tight">
            <div className="truncate text-[13px] font-semibold tracking-tight">Seerxo</div>
            <div className="truncate text-[11px] text-muted-foreground">Company OS</div>
          </div>
        </div>
        {data && (
          <Button variant="outline" className="h-10 justify-start gap-2 bg-background px-3 text-[13px] font-medium shadow-xs" onClick={() => { setOpenMobile(false); onCreate() }}>
            <Plus className="size-4" />New task<Kbd className="ml-auto h-5 text-[10px]">C</Kbd>
          </Button>
        )}
      </SidebarHeader>
      <SidebarContent className="gap-0 px-2 pt-1">
        <nav aria-label="Sections" className="grid gap-0.5">
          <SidebarMenu className="gap-px">
            {/* Three places; each page inside one is a tab at the top of the page. */}
            {SECTIONS.map(sec => {
              const Icon = SECTION_ICON[sec.key]
              const here = sectionOf(route.page).key === sec.key
              const [n, hot, what] = count[sec.key] ?? []
              return (
                <SidebarMenuItem key={sec.key}>
                  <SidebarMenuButton asChild isActive={here}
                    className={cn("h-10 gap-2.5 px-2.5 text-[14px] text-sidebar-foreground/70 transition-colors duration-150 hover:bg-sidebar-accent/70 hover:text-sidebar-foreground",
                      "data-active:bg-sidebar-accent data-active:font-medium data-active:text-sidebar-foreground")}>
                    {/* Real links: open in a new tab, copy the address. The hash route does the navigation. */}
                    <a href={`#/${sec.home}`} onClick={() => setOpenMobile(false)} aria-current={here ? "page" : undefined}>
                      <Icon className={cn("size-4", here ? "text-brand" : "text-muted-foreground")} /><span>{sec.label}</span>
                      {!!n && <span className="sr-only">, {n} {what}</span>}
                    </a>
                  </SidebarMenuButton>
                  {!!n && <SidebarMenuBadge aria-hidden className={cn("num top-2.5 rounded-full px-1.5 text-[11px] font-medium text-muted-foreground", hot && "bg-destructive/10 text-destructive")}>{n}</SidebarMenuBadge>}
                </SidebarMenuItem>
              )
            })}
          </SidebarMenu>
        </nav>
      </SidebarContent>
      <SidebarFooter className="gap-1 border-t border-sidebar-border px-2 py-3">
        {/* One line, not four: the sidebar must keep every page visible on a 720 px laptop. Detail is on System status. */}
        {data ? (() => {
          // The same checklist System shows (lib/operating.ts healthSummary): only verified checks count as fine.
          const checks = systemChecks(data, now), h = healthSummary(checks)
          return <button type="button" onClick={() => nav("health")} aria-label={`Company health: ${h.text}. Open System status`}
            className="flex h-9 w-full items-center gap-2 rounded-md px-2.5 text-left text-[13px] transition-colors hover:bg-sidebar-accent/70">
            <Dot tone={h.tone} />
            <span className="min-w-0 truncate">{h.open.length ? `${h.open.length} of ${checks.length} checks not verified` : h.text}</span>
            <span className="ml-auto text-xs text-muted-foreground">Status</span>
          </button>
        })() : <Skeleton className="h-9 w-full" />}
      </SidebarFooter>
    </Sidebar>
  )
}

/** Phones: the three places as a bottom bar; their pages are tabs at the top of each page. */
function MobileNav() {
  const { route, go } = useDesk()
  const { section: count } = useCounts()
  const active = sectionOf(route.page).key
  return (
    <nav aria-label="Sections" className="fixed inset-x-0 bottom-0 z-30 border-t bg-background/95 pb-[env(safe-area-inset-bottom)] backdrop-blur-xl lg:hidden">
      <ul className="grid grid-cols-3">
        {SECTIONS.map(s => {
          const Icon = SECTION_ICON[s.key]
          const on = active === s.key
          const [n, hot, what] = count[s.key] ?? []
          return (
            <li key={s.key}>
              <button onClick={() => go(s.home)} aria-current={on ? "page" : undefined}
                className={cn("relative flex h-[58px] w-full flex-col items-center justify-center gap-1 text-[11px] font-medium transition-colors duration-150 focus-visible:z-10 focus-visible:outline-2 focus-visible:outline-offset-[-3px] focus-visible:outline-ring", on ? "text-foreground" : "text-muted-foreground")}>
                <Icon className={cn("size-[19px]", on && "text-brand")} strokeWidth={on ? 2.2 : 1.8} />{s.label}
                {!!n && <span className={cn("num absolute top-1.5 left-[calc(50%+6px)] min-w-4 rounded-full bg-muted px-1 text-[10px] leading-4 font-semibold", hot && "bg-destructive text-white")}>
                  {n}<span className="sr-only"> {what}</span></span>}
              </button>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}

/** ⌘K: find a page, project, agent or task; create a task; send an instruction to the CEO. Sent commands are followed by
 *  their task's real status (lib/lifecycle.ts): accepted is never shown as executed, and nothing here claims verification. */
function CommandPalette({ open, setOpen, onCreate, onShortcuts, t }: {
  open: boolean; setOpen: (o: boolean) => void; onCreate: () => void; onShortcuts: () => void; t: ThemeApi
}) {
  const { data, go, open: openDetail, reload, sent } = useDesk()
  const [text, setText] = useState("")
  const returnFocus = useReturnFocus()
  const { focusDraft } = useDraft()
  // A hand-off to the composer takes focus only after the palette has closed: inside the dialog's focus trap the
  // composer could not hold it, and the default return-focus would take it back.
  const handedOff = useRef(false)
  const closeFocus = (e: Event) => { if (handedOff.current) { handedOff.current = false; e.preventDefault(); focusDraft() } else returnFocus(e) }
  const run = (fn: () => void) => { setOpen(false); setText(""); fn() }
  const instruction = text.trim()
  // Own filtering (cmdk's is off): matches first, "Send to CEO" always last, so Enter on a
  // typed page name navigates and only sends an instruction when nothing else matches.
  const q = instruction.toLowerCase()
  const hit = (...xs: (string | null | undefined)[]) => !q || xs.some(x => x?.toLowerCase().includes(q))
  const pages = SECTIONS.flatMap(s => (s.tabs.length ? s.tabs : [s.home]).map(p => ({ page: p, section: s })))
    .filter(i => hit(PAGE_LABEL[i.page], pageTitle(i.page), i.page, i.section.label))
  const units = (data?.portfolio ?? []).filter(u => q && hit(u.name, u.key))
  const agents = (data?.agents ?? []).filter(a => q && hit(a.name, a.key, a.role))
  const actions = [
    ...(data ? [{ key: "create", label: "New task", icon: ListPlus, hint: "C", fn: onCreate }] : []),
    { key: "refresh", label: "Refresh data", icon: RefreshCw, hint: "", fn: reload },
    { key: "keys", label: "Keyboard shortcuts", icon: Keyboard, hint: "?", fn: onShortcuts },
    ...THEMES.filter(x => x.value !== t.theme).map(x => ({ key: `theme-${x.value}`, label: `Theme: ${x.label}`, icon: x.icon, hint: "", fn: () => t.setTheme(x.value) })),
  ].filter(a => hit(a.label, "theme mode dark light"))
  const tasks = (data?.tasks ?? []).filter(t => hit(t.key, t.title)).slice(0, q ? 20 : 5)
  const status = (key: string) => data?.tasks.find(t => t.key === key)?.status
  const matches = pages.length + units.length + agents.length + tasks.length
  return (
    <CommandDialog onCloseAutoFocus={closeFocus} open={open} onOpenChange={o => { setOpen(o); if (!o) setText("") }}
      title="Command" description="Find anything, create a task or instruct the CEO" className="top-[14dvh] sm:max-w-[620px]">
      <Command loop shouldFilter={false} className="p-0">
        <CommandInput value={text} onValueChange={setText} placeholder="Search, or write an instruction for the CEO…" />
        <CommandList className="max-h-[min(440px,58dvh)] px-1.5 pb-1.5">
          <CommandEmpty className="px-4 py-8 text-[13px] text-muted-foreground">
            {instruction.length < 3 ? "No matches. Keep typing to write an instruction for the CEO." : "No matches."}
          </CommandEmpty>
          {data && instruction.length >= 3 && !matches && <InstructGroup instruction={instruction} run={fn => { handedOff.current = true; run(fn) }} />}
          {!q && !!sent.length && <CommandGroup heading="Your recent commands">
            {sent.map(c => (
              <CommandItem key={c.key} value={`sent-${c.key}`} onSelect={() => run(() => openDetail({ kind: "task", key: c.key }))}>
                <Send /><span className="min-w-0 flex-1 truncate">{c.text}</span>
                <span className="font-mono text-[11px] text-muted-foreground max-sm:hidden">{c.key}</span>
                <CommandTrack l={commandLifecycle(status(c.key), true)} compact />
              </CommandItem>
            ))}
          </CommandGroup>}
          {!!pages.length && <CommandGroup heading="Go to">
            {pages.map(({ page, section }) => {
              const Icon = PAGE_ICON[page]
              return <CommandItem key={page} value={`go-${page}`} onSelect={() => run(() => go(page))}>
                <Icon />{pageTitle(page)}{section.label !== pageTitle(page) && <span className="text-xs text-muted-foreground">in {section.label}</span>}
                {page === section.home && <CommandShortcut>G {section.shortcut.toUpperCase()}</CommandShortcut>}
              </CommandItem>
            })}
          </CommandGroup>}
          {!!units.length && <CommandGroup heading="Business units">
            {units.map(u => <CommandItem key={u.key} value={`unit-${u.key}`} onSelect={() => run(() => go("project", u.key))}>
              <Briefcase />{u.name}<span className="flex flex-1 justify-end"><Status value={u.status} /></span></CommandItem>)}
          </CommandGroup>}
          {!!agents.length && <CommandGroup heading="Agents">
            {agents.map(a => <CommandItem key={a.key} value={`agent-${a.key}`} onSelect={() => run(() => openDetail({ kind: "agent", key: a.key }))}>
              <Bot />{a.name}<span className="flex flex-1 justify-end"><Status value={a.status} /></span></CommandItem>)}
          </CommandGroup>}
          {!!actions.length && <CommandGroup heading="Actions">
            {actions.map(({ key, label, icon: Icon, hint, fn }) => <CommandItem key={key} value={`act-${key}`} onSelect={() => run(fn)}>
              <Icon />{label}{hint && <CommandShortcut>{hint}</CommandShortcut>}</CommandItem>)}
          </CommandGroup>}
          {!!tasks.length && <CommandGroup heading={q ? "Tasks" : "Recent tasks"}>
            {tasks.map(t => (
              <CommandItem key={t.key} value={`task-${t.key}`} onSelect={() => run(() => openDetail({ kind: "task", key: t.key }))}>
                <FileText /><span className="w-[72px] shrink-0 font-mono text-[11px] text-muted-foreground">{t.key}</span><span className="min-w-0 flex-1 truncate">{t.title}</span><Status value={t.status} />
              </CommandItem>))}
          </CommandGroup>}
          {data && instruction.length >= 3 && matches > 0 && <InstructGroup instruction={instruction} run={fn => { handedOff.current = true; run(fn) }} />}
        </CommandList>
        <div className="flex items-center gap-3 border-t px-3.5 py-2 text-[11px] text-muted-foreground max-sm:hidden">
          <span className="whitespace-nowrap"><Kbd>↵</Kbd> select</span><span className="whitespace-nowrap"><Kbd>↑</Kbd> <Kbd>↓</Kbd> move</span><span className="whitespace-nowrap"><Kbd>esc</Kbd> close</span>
          <span className="ml-auto min-w-0 truncate text-right">Instructions become a READY task for the CEO; nothing runs immediately.</span>
        </div>
      </Command>
    </CommandDialog>
  )
}

/** Search never submits a command: an unmatched query can be carried to the Home composer, where sending is its
 *  own explicit step. (Pressing Enter on "no matches" used to create a READY task.) */
function InstructGroup({ instruction, run }: { instruction: string; run: (fn: () => void) => void }) {
  const { go } = useDesk()
  const { setDraft } = useDraft()
  return (
    <CommandGroup heading="Instruct the CEO">
      {/* Never replaces a draft in progress (lib/draft.ts handOff); the composer takes focus once it is shown. */}
      <CommandItem value="instruct" onSelect={() => run(() => { setDraft(cur => handOff(cur, instruction)); go("overview") })}>
        <Sparkles className="text-brand" /><span className="min-w-0 flex-1 truncate">Write “{instruction}” as an instruction</span>
        <span className="text-xs text-muted-foreground max-sm:hidden">review, then send from Home</span><CornerDownLeft className="size-3.5! text-muted-foreground" />
      </CommandItem>
    </CommandGroup>
  )
}

const THEMES: { value: Theme; label: string; icon: typeof Sun }[] = [
  { value: "system", label: "System", icon: Monitor }, { value: "light", label: "Light", icon: Sun }, { value: "dark", label: "Dark", icon: Moon },
]
type ThemeApi = ReturnType<typeof useTheme>

/** One quiet button that cycles System → Light → Dark; its tooltip says what it is and what comes next. */
function ThemeToggle({ t }: { t: ThemeApi }) {
  const i = THEMES.findIndex(x => x.value === t.theme)
  const Current = THEMES[i].icon
  const next = THEMES[(i + 1) % 3]
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button className="grid size-10 place-items-center rounded-lg text-muted-foreground transition-colors duration-150 hover:bg-muted hover:text-foreground"
          aria-label={`Theme: ${THEMES[i].label}. Switch to ${next.label}`} onClick={() => t.setTheme(next.value)}>
          <Current className="size-4" />
        </button>
      </TooltipTrigger>
      <TooltipContent>Theme: {THEMES[i].label} · switch to {next.label}</TooltipContent>
    </Tooltip>
  )
}

/** The connection, honestly: Live only while the server stream is open and recently heard; otherwise what is known. */
function Freshness() {
  const { lastOk, loading, error, reload } = useDesk()
  const stale = useStale()
  const link = useLink()
  // Offline first: going offline clears the shown data (state.tsx), which must not read as "Loading".
  const label = link === "offline" ? "Offline"
    : !lastOk ? (error ? "Unavailable" : "Loading")
    : stale ? `Stale · ${ago(lastOk)}`
    : link === "live" ? "Live"
    : link === "reconnecting" || link === "connecting" ? "Reconnecting"
    : `Updated ${ago(lastOk)} ago`
  const tone = link === "offline" ? "danger" : !lastOk ? (error ? "danger" : "neutral") : stale ? "warning"
    : link === "live" ? "success" : "neutral"
  const tip = link === "offline" ? "No network: nothing shown is current." : !lastOk ? "Waiting for the first read"
    : link === "live" ? `Live: the server announces every committed change. Last read ${new Date(lastOk).toLocaleTimeString()}.`
    : link === "polling" ? "The live stream is not available from this API; the Desk re-reads every 30s."
    : link === "reconnecting" || link === "connecting" ? "The live stream dropped; reconnecting. Shown data is from the last read."
    : link === "stale" ? "The live stream is silent or the server cannot read state; shown data may be outdated."
    : "No network: nothing shown is current."
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button onClick={reload} disabled={loading} aria-live="polite" aria-label={`Data ${label}. Read again now`}
          className={cn("num flex h-10 items-center gap-2 rounded-lg px-2 text-xs text-muted-foreground transition-colors duration-150 hover:bg-muted hover:text-foreground",
            tone === "warning" && "text-warning", tone === "danger" && "text-destructive")}>
          {loading && link !== "live" ? <RefreshCw className="size-3 animate-spin" />
            : <span className={cn("size-1.5 rounded-full", DOT[tone], link === "live" && "desk-pulse")} aria-hidden />}
          <span className="max-sm:hidden">{label}</span>
        </button>
      </TooltipTrigger>
      <TooltipContent>{tip}</TooltipContent>
    </Tooltip>
  )
}

/** True once the deployed Desk differs from this tab's build. The data refreshes every 30s but
 *  the page code does not: a tab left open would otherwise show an old Desk indefinitely. */
function useNewVersion() {
  const [fresh, setFresh] = useState(false)
  useEffect(() => {
    const mine = document.querySelector<HTMLMetaElement>('meta[name="desk-build"]')?.content
    if (!mine) return  // dev server: no build id
    const check = async () => {
      if (document.hidden) return
      try {
        const html = await (await fetch(location.pathname, { cache: "no-store" })).text()
        const live = /<meta name="desk-build" content="([0-9a-f]+)"/.exec(html)?.[1]
        if (live && live !== mine) setFresh(true)
      } catch { /* offline or an access redirect: the next check retries */ }
    }
    const id = setInterval(check, 5 * 60_000)
    document.addEventListener("visibilitychange", check)
    void check()
    return () => { clearInterval(id); document.removeEventListener("visibilitychange", check) }
  }, [])
  return fresh
}

const Banner = ({ tone = "neutral", icon, children, action }: { tone?: "neutral" | "warning"; icon: React.ReactNode; children: React.ReactNode; action?: React.ReactNode }) => (
  <div role="status" className={cn("flex flex-wrap items-center gap-x-2.5 gap-y-1 rounded-xl border px-3.5 py-2.5 text-[13px] [&>svg]:size-4 [&>svg]:shrink-0",
    tone === "warning" ? "border-warning/25 bg-warning/[0.06] [&>svg]:text-warning" : "bg-surface [&>svg]:text-muted-foreground")}>
    {icon}<span className="min-w-0 flex-1">{children}</span>{action}
  </div>
)

function Banners() {
  const { data, lastOk, error, online, go } = useDesk()
  const stale = useStale()
  const fresh = useNewVersion()
  return <>
    {fresh && <Banner icon={<RefreshCw />} action={<Button size="sm" variant="outline" className="h-8" onClick={() => location.reload()}>Reload</Button>}>
      <b className="font-medium">A new version of the Desk is available.</b></Banner>}
    {!online && <Banner tone="warning" icon={<WifiOff />}>
      <b className="font-medium">You are offline.</b> <span className="text-muted-foreground">Company data is hidden until the connection and sign-in return.</span></Banner>}
    {data?.control.agents_paused && <Banner tone="warning" icon={<ShieldCheck />} action={<Button size="sm" variant="outline" className="h-8" onClick={() => go("automation")}>Automation</Button>}>
      <b className="font-medium">Agent execution paused.</b> <span className="text-muted-foreground">Queued work will not start. Serving infrastructure is unchanged.</span></Banner>}
    {online && stale && lastOk && <Banner tone="warning" icon={<RefreshCw />}>
      <b className="font-medium text-warning">Showing last known data</b> <span className="text-muted-foreground">from {new Date(lastOk).toLocaleTimeString()}.{error && ` Refresh failed: ${error}`}</span></Banner>}
  </>
}

/** Tabs between the pages of one section, where the sidebar is not on screen (below 1024px). Each tab is its own route. */
function SectionTabs() {
  const { route, go } = useDesk()
  const { tab: count } = useCounts()
  const tabs = sectionOf(route.page).tabs
  if (!tabs.includes(route.page)) return null
  return (
    <nav aria-label="Section pages" className="-mx-4 -mt-1 overflow-x-auto border-b px-4 [scrollbar-width:none] md:-mx-8 md:px-8">
      <ul className="flex gap-5">
        {tabs.map(p => (
          <li key={p}>
            <button onClick={() => go(p)} aria-current={route.page === p ? "page" : undefined}
              className={cn("relative flex h-11 items-center gap-1.5 text-[13px] whitespace-nowrap transition-colors duration-150",
                route.page === p ? "font-medium text-foreground after:absolute after:inset-x-0 after:-bottom-px after:h-0.5 after:rounded-full after:bg-foreground" : "text-muted-foreground hover:text-foreground")}>
              {PAGE_LABEL[p]}{count[p] ? <span className="num text-[11px] text-muted-foreground">{count[p]}</span> : null}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  )
}

function Main() {
  const { data, error, route, reload, online } = useDesk()
  if (!online) return <div role="status" className="grid max-w-md gap-3 py-16">
    <WifiOff className="size-6 text-warning" /><h1 className="text-lg font-semibold">Connection unavailable</h1>
    <p className="text-sm text-muted-foreground">Company data and actions are unavailable offline. Reconnect and sign in to refresh.</p>
  </div>
  if (!data) return error
    ? <div role="alert" className="grid max-w-md gap-3 py-16">
        <span className="grid size-9 place-items-center rounded-lg bg-destructive/10 text-destructive"><AlertOctagon className="size-4" /></span>
        <h1 className="text-lg font-semibold tracking-tight">The Founder Desk API is not answering</h1>
        <p className="text-[13px] text-muted-foreground [overflow-wrap:anywhere]">{error}. Nothing is shown until the API answers; no value is guessed.</p>
        <div><Button size="sm" variant="outline" onClick={reload}><RefreshCw />Try again</Button></div>
      </div>
    : <LoadingPage />
  switch (route.page) {
    case "approvals": return <ApprovalsPage />
    case "projects": return <ProjectsPage />
    case "project": return <ProjectDetail unitKey={route.key ?? ""} />
    case "finance": return <FinancePage />
    case "growth": return <GrowthPage />
    case "tasks": return <TasksPage />
    case "automation": return <AutomationPage />
    case "agents": return <AgentsPage />
    case "incidents": return <IncidentsPage />
    case "activity": return <ActivityPage />
    case "health": return <HealthPage />
    case "memory": return <MemoryPage />
    default: return <Overview />
  }
}

const SHORTCUTS: [string[], string][] = [
  [[MOD, "K"], "Search, navigate or instruct the CEO"], [["/"], "Search"], [["C"], "New task"],
  ...SECTIONS.map((s): [string[], string] => [["G", s.shortcut.toUpperCase()], `Go to ${s.label}`]),
  [[MOD, "B"], "Show or hide the sidebar"], [["?"], "This list"], [["Esc"], "Close a dialog or panel"],
]

function ShortcutsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>Keyboard shortcuts</DialogTitle><DialogDescription>Single-key shortcuts work anywhere outside a text field.</DialogDescription></DialogHeader>
        <dl className="grid gap-px text-[13px]">
          {SHORTCUTS.map(([keys, what]) => (
            <div key={what} className="flex items-center justify-between gap-4 border-b border-border/60 py-2 last:border-0">
              <dt className="text-muted-foreground">{what}</dt>
              <dd className="flex items-center gap-1">{keys.map((k, i) => <span key={k} className="contents">{i > 0 && keys[0] === "G" && <span className="text-[11px] text-muted-foreground">then</span>}<Kbd>{k}</Kbd></span>)}</dd>
            </div>
          ))}
        </dl>
      </DialogContent>
    </Dialog>
  )
}

/** Global keys: ⌘K / "/" palette, "c" new task, "g" then a section letter, "?" help. Ignored while typing or in a dialog. */
function useShortcuts({ palette, create, help }: { palette: () => void; create: () => void; help: () => void }) {
  const { go } = useDesk()
  const leader = useRef(0)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === "k" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); palette(); return }
      if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return
      const el = e.target as HTMLElement
      if (el.closest?.("input, textarea, select, [contenteditable=true], [role=dialog], [role=alertdialog], [role=menu], [role=listbox]")) return
      const k = e.key.toLowerCase()
      if (Date.now() - leader.current < 1200) {
        leader.current = 0
        const s = SECTIONS.find(x => x.shortcut === k)
        if (s) { e.preventDefault(); go(s.home) }
        return
      }
      if (k === "g") { leader.current = Date.now(); return }
      if (k === "/") { e.preventDefault(); palette() }
      else if (k === "c") { e.preventDefault(); create() }
      else if (e.key === "?") { e.preventDefault(); help() }
    }
    addEventListener("keydown", onKey)
    return () => removeEventListener("keydown", onKey)
  }, [go, palette, create, help])
}

function TopBar({ t, onSearch }: { t: ThemeApi; onSearch: () => void }) {
  const { route, data, go } = useDesk()
  const section = sectionOf(route.page)
  const project = route.page === "project" ? (data?.portfolio?.find(u => u.key === route.key)?.name ?? route.key) : null
  const here = project ?? (route.page === section.home ? section.label : PAGE_LABEL[route.page])
  const crumb = section.key !== "home" && (route.page !== section.home || project)
  return (
    <header className="sticky top-0 z-20 flex min-h-14 items-center gap-2 border-b bg-background/85 px-3 pt-[env(safe-area-inset-top)] backdrop-blur-xl md:px-6">
      <SidebarTrigger className="size-10 rounded-lg text-muted-foreground lg:hidden" />
      <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1.5 text-[13px]">
        {crumb && <>
          <button className="inline-flex min-h-7 items-center text-muted-foreground transition-colors hover:text-foreground max-sm:hidden" onClick={() => go(section.home)}>{section.label}</button>
          <ChevronRight className="size-3.5 text-muted-foreground/50 max-sm:hidden" />
        </>}
        <span className="truncate font-medium" aria-current="page">{here}</span>
      </nav>
      <div className="ml-auto flex items-center gap-0.5">
        <Freshness />
        <span className="mx-1 h-4 w-px bg-border max-sm:hidden" aria-hidden />
        <button onClick={onSearch} aria-label="Search or instruct the CEO" className="hidden h-10 min-w-[220px] items-center gap-2 rounded-lg border bg-card px-3 text-[13px] text-muted-foreground shadow-xs transition-colors hover:border-foreground/20 hover:text-foreground lg:flex">
          <Search className="size-3.5" /><span className="flex-1 text-left">Search or command</span><Kbd className="h-5 text-[10px]">{MOD} K</Kbd>
        </button>
        <button onClick={onSearch} aria-label="Search or instruct the CEO" className="grid size-10 place-items-center rounded-lg text-muted-foreground transition-colors duration-150 hover:bg-muted hover:text-foreground lg:hidden">
          <Search className="size-4" />
        </button>
        <ThemeToggle t={t} />
      </div>
    </header>
  )
}

function Shell({ t }: { t: ThemeApi }) {
  const { route, data, composing, compose, seed } = useDesk()
  const [palette, setPalette] = useState(false)
  const [help, setHelp] = useState(false)
  const togglePalette = () => setPalette(o => !o)
  const create = () => { if (data) compose(true) }
  useShortcuts({ palette: togglePalette, create, help: () => setHelp(true) })
  return (
    <SidebarProvider>
      <AppSidebar onCreate={create} />
      <SidebarInset className="min-w-0">
        <TopBar t={t} onSearch={() => setPalette(true)} />
        {/* SidebarInset is the page's one <main> landmark; this is its content column. */}
        <div className="desk-main mx-auto grid w-full gap-6 px-4 pt-5 pb-[calc(5.5rem+env(safe-area-inset-bottom))] sm:px-6 md:px-8 md:pt-8 lg:pb-14">
          <SectionTabs />
          <Banners />
          <ErrorBoundary label="This page" resetKey={JSON.stringify(route)}>
            <div key={`${route.page}/${route.key ?? ""}`} className="desk-enter grid min-w-0 gap-8"><Main /></div>
          </ErrorBoundary>
        </div>
      </SidebarInset>
      <MobileNav />
      <DetailSheet />
      <CommandPalette open={palette} setOpen={setPalette} onCreate={create} onShortcuts={() => setHelp(true)} t={t} />
      {data && <CreateTask key={seed.key} open={composing} onOpenChange={o => compose(o)} />}
      <ShortcutsDialog open={help} onOpenChange={setHelp} />
    </SidebarProvider>
  )
}

/** First load: the shape every page now has (heading, figure strip, list), so nothing jumps when data arrives.
 *  No number is drawn: a placeholder is never mistaken for a 0. */
function LoadingPage() {
  const rows = (n: number) => Array.from({ length: n }, (_, i) => (
    <div key={i} className="flex items-start gap-3 px-4 py-3.5"><Skeleton className="mt-1 size-2 rounded-full" />
      <div className="grid flex-1 gap-2"><Skeleton className="h-3.5 w-3/5" /><Skeleton className="h-3 w-2/5" /></div></div>))
  return (
    <div className="grid min-w-0 grid-cols-1 gap-6 lg:gap-8" aria-busy="true" aria-label="Loading company data">
      <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
        <div className="grid min-w-0 gap-2.5"><Skeleton className="h-3 w-32" /><Skeleton className="h-9 w-80 max-w-[85vw]" />
          <Skeleton className="h-4 w-[28rem] max-w-[85vw]" /><Skeleton className="h-4 w-60 sm:hidden" /><Skeleton className="h-3.5 w-72 max-w-[75vw]" /></div>
        <div className="flex gap-2"><Skeleton className="h-10 w-32 rounded-md" /><Skeleton className="h-10 w-28 rounded-md" /></div>
      </div>
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border bg-border lg:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => <div key={i} className="grid gap-2 bg-card px-5 py-4"><Skeleton className="h-2.5 w-20" /><Skeleton className="h-7 w-12" /><Skeleton className="h-2.5 w-24" /></div>)}
      </div>
      <div className="grid min-w-0 gap-8 xl:grid-cols-[minmax(0,1fr)_minmax(320px,380px)]">
        <div className="grid content-start gap-2.5"><Skeleton className="h-3.5 w-28" /><div className="panel divide-y divide-border/70">{rows(5)}</div></div>
        <div className="grid content-start gap-2.5 max-xl:hidden"><Skeleton className="h-3.5 w-20" /><div className="panel divide-y divide-border/70">{rows(4)}</div></div>
      </div>
    </div>
  )
}

export default function App() {
  const t = useTheme()
  return (
    <TooltipProvider delayDuration={300}>
      <ErrorBoundary label="The Founder Desk"><DeskProvider><Shell t={t} /></DeskProvider></ErrorBoundary>
      <Toaster position="bottom-right" theme={t.resolved} mobileOffset={{ bottom: 72 }} />
    </TooltipProvider>
  )
}
