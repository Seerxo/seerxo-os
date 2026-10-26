// Desk state: one read model, re-read when the server says committed state changed (GET /desk/stream, lib/live.ts).
// Timed reads are only the fallback while the stream is down: every 30s, every 3s for a minute after a founder action
// (pure reads, no model calls), paused while the tab is hidden. Server errors keep the last real values marked STALE; lost connectivity or auth clears them.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { toast } from "sonner"
import { api, ApiError, type DeskState } from "@/lib/api"
import { BURST_MS, coalesce, due } from "@/lib/refresh"
import { describe, FRESH_MS, prune, statusChanges, type Change, type Changes } from "@/lib/changes"
import { humanize } from "@/lib/format"
import { connectLive, INITIAL, linkState, type Link, type LinkSnap } from "@/lib/live"

import { PAGES, resolvePage, type Page } from "@/lib/nav"
import { beginSend, closeDialog, dialogAfterError, draftAfterError, endSend, NO_DIALOG, openDialog, type Dialog } from "@/lib/draft"
export { PAGES, type Page }
export interface Route { page: Page; key?: string }

const parse = (): Route => {
  const [raw, key] = location.hash.replace(/^#\/?/, "").split("/")
  const page = resolvePage(raw)
  return page ? { page, key: key && decodeURIComponent(key) } : { page: "overview" }
}

export type Detail = { kind: "task" | "agent"; key: string } | null

/** A founder command sent this session: the API's answer (task key, status at creation) and when. Its later
 *  progress is read from the task list (lib/lifecycle.ts), never assumed. */
export interface SentCommand { text: string; at: number; key: string; agent: string }
export interface CommandBody { text: string; agent: string; priority: string; [k: string]: unknown }

interface Ctx {
  data: DeskState | null
  /** The live link's raw state; read it through useLink(). */
  link: LinkSnap
  lastOk: number | null
  loading: boolean
  error: string | null
  online: boolean
  reload: () => Promise<void>
  route: Route
  go: (page: Page, key?: string) => void
  detail: Detail
  open: (d: Detail) => void
  /** POST and refresh; errors surface as a toast and return false. */
  act: (path: string, body: unknown, done: string) => Promise<boolean>
  /** POST /desk/command: on 201 the command is accepted and a task recorded, nothing more. */
  command: (body: CommandBody) => Promise<SentCommand | null>
  sent: SentCommand[]
  /** The one create flow (brief the CEO, or a full task contract): open it from anywhere. */
  composing: boolean
  /** Open or close the full task dialog. `seed` (the Home composer's draft) pre-fills it; nothing is submitted.
   *  Both are refused while the dialog's request is in flight. */
  compose: (open: boolean, seed?: string) => void
  /** What the open dialog was seeded with ("" when opened empty) and a key that changes with each open. */
  seed: { text: string; key: number }
  /** True while the dialog's create request is in flight. */
  sending: boolean
  /** Claim the send for the open dialog: its key, or null if one is already in flight (then do not POST). */
  startSend: () => number | null
  /** Settle the send started with `key`; an outdated key changes nothing. */
  settleSend: (key: number, ok: boolean) => void
}

/** The Home command composer's text, in its own context so a keystroke re-renders only the composer and the palette,
 *  not every useDesk() consumer. Search (⌘K) may hand text over to it; only the composer submits. */
interface DraftCtx {
  draft: string
  setDraft: (text: string | ((current: string) => string)) => void
  /** Ask the composer to take focus (caret at the end) the next time it is mounted or already shown. */
  focusDraft: () => void
  /** Consumed by the composer: true once after focusDraft. */
  takeFocus: () => boolean
  focusTick: number
}

const DeskContext = createContext<Ctx | null>(null)
const DraftContext = createContext<DraftCtx | null>(null)
const sessionLost = (e: unknown) => !(e instanceof ApiError) || [0, 401, 403].includes(e.status)

export function DeskProvider({ children }: { children: ReactNode }) {
  const [data, setData] = useState<DeskState | null>(null)
  const [lastOk, setLastOk] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [online, setOnline] = useState(navigator.onLine)
  const [route, setRoute] = useState<Route>(parse)
  const [detail, open] = useState<Detail>(null)
  const [sent, setSent] = useState<SentCommand[]>([])
  const [dialog, setDialog] = useState<Dialog>(NO_DIALOG)
  const { composing, seed } = dialog
  const sending = dialog.sending !== null
  // Every transition reads and writes the ref synchronously, so two events in one frame (a double click) see each
  // other: the second submit is refused even before React re-renders.
  const dialogRef = useRef(dialog)
  const applyDialog = useCallback((fn: (d: Dialog) => Dialog) => { const next = fn(dialogRef.current); dialogRef.current = next; setDialog(next) }, [])
  const compose = useCallback((o: boolean, text?: string) => applyDialog(d => (o ? openDialog(d, text) : closeDialog(d))), [applyDialog])
  const startSend = useCallback(() => {
    const next = beginSend(dialogRef.current)
    if (!next) return null
    applyDialog(() => next)
    return next.sending
  }, [applyDialog])
  const settleSend = useCallback((key: number, ok: boolean) => applyDialog(d => endSend(d, key, ok)), [applyDialog])
  const [draft, setDraft] = useState("")
  const pendingFocus = useRef(false)
  const [focusTick, setFocusTick] = useState(0)
  const focusDraft = useCallback(() => { pendingFocus.current = true; setFocusTick(n => n + 1) }, [])
  const takeFocus = useCallback(() => { const p = pendingFocus.current; pendingFocus.current = false; return p }, [])
  const requestId = useRef(0)
  const hadData = useRef(false)
  const [changes, setChanges] = useState<Changes>({})
  const prev = useRef<DeskState | null>(null)
  const clearLiveData = useCallback(() => {
    setData(null)
    setLastOk(null)
    open(null)
    setSent([])
    prev.current = null
    setChanges({})
    document.title = "Founder Desk"
  }, [])
  // Live data goes on any lost-session signal; the unsent instruction only on a real sign-out (lib/draft.ts), so a
  // network error, a 403 or going offline never erases the founder's text.
  const endSession = useCallback((e: unknown) => {
    clearLiveData()
    setDraft(cur => draftAfterError(e, cur))
    applyDialog(d => dialogAfterError(e, d))   // a real sign-out also drops the dialog's copy of the draft
  }, [clearLiveData, applyDialog])

  const lastRead = useRef(0)
  const fastUntil = useRef(0)
  const read = useCallback(async () => {
    if (!navigator.onLine) return
    const id = ++requestId.current
    lastRead.current = Date.now()
    setLoading(true)
    try {
      const state = await api<DeskState>("/desk/state")
      if (id !== requestId.current || !navigator.onLine) return
      const at = Date.now()
      const p = prev.current
      const fresh = p ? {
        ...statusChanges("task", p.tasks, state.tasks, t => t.key, t => t.status, at),
        ...statusChanges("agent", p.agents, state.agents, a => a.key, a => a.status, at),
        ...statusChanges("event", p.activity, state.activity, e => String(e.id), () => "NEW", at),
      } : {}
      prev.current = state
      if (Object.keys(fresh).length) {
        setChanges(c => ({ ...prune(c, at), ...fresh }))
        setTimeout(() => setChanges(c => prune(c, Date.now())), FRESH_MS + 50)
      }
      setData(state)
      setLastOk(at)
      setError(null)
      hadData.current = true
      const n = Array.isArray(state.needs_you) ? state.needs_you.length : 0
      document.title = n ? `(${n}) Founder Desk` : "Founder Desk"
    } catch (e) {
      if (id !== requestId.current) return
      const message = (e as Error).message
      setError(message)
      if (sessionLost(e)) endSession(e)
      if (!hadData.current) toast.error(`Load failed: ${message}`)
    } finally {
      if (id === requestId.current) {
        setLoading(false)
      }
    }
  }, [endSession])
  const reload = useMemo(() => coalesce(read), [read])
  const [link, setLink] = useState<LinkSnap>(INITIAL)
  const streaming = useRef(false)  // open and accepted: timed reads stand down
  /** After the founder acts: re-read now, then fast for a minute so what the worker does next shows as it happens. */
  const kick = useCallback(() => { fastUntil.current = Date.now() + BURST_MS; void reload() }, [reload])

  useEffect(() => {
    reload()
    // One stream for the whole app. A notice while the tab is hidden is not read: the visibility handler re-reads
    // on return anyway, so a backgrounded phone costs nothing but the open connection.
    const stopLive = typeof EventSource === "undefined" ? () => {} : connectLive({
      url: "/desk/stream", Source: EventSource,
      onChange: () => { if (!document.hidden) void reload() },
      onSnap: s => { streaming.current = s.open && !s.refused; setLink(s) },
    })
    const poll = setInterval(() => {
      if (!document.hidden && !streaming.current && due(Date.now(), lastRead.current, fastUntil.current)) reload()
    }, 1_000)
    const onVisible = () => { if (!document.hidden) reload() }
    const onOffline = () => {
      requestId.current++
      setOnline(false)
      setLoading(false)
      clearLiveData()
      setError(null)
    }
    const onOnline = () => { setOnline(true); reload() }
    const onHash = () => { setRoute(parse()); scrollTo(0, 0) }
    document.addEventListener("visibilitychange", onVisible)
    addEventListener("offline", onOffline)
    addEventListener("online", onOnline)
    addEventListener("hashchange", onHash)
    return () => {
      stopLive()
      clearInterval(poll)
      document.removeEventListener("visibilitychange", onVisible)
      removeEventListener("offline", onOffline)
      removeEventListener("online", onOnline)
      removeEventListener("hashchange", onHash)
    }
  }, [reload, clearLiveData])

  const go = useCallback((page: Page, key?: string) => {
    location.hash = `/${page}${key ? `/${encodeURIComponent(key)}` : ""}`
  }, [])

  const act = useCallback(async (path: string, body: unknown, done: string) => {
    if (!navigator.onLine) { toast.error("Reconnect before making changes."); return false }
    try {
      await api(path, body)
      toast.success(done)
      kick()
      return true
    } catch (e) {
      // A refused action (403) may be a missing permission, not a lost session: re-read once. If the session is gone
      // that read fails too and the Desk stays cleared; a live stream would otherwise never bring the data back.
      if (sessionLost(e)) { endSession(e); void reload() }
      toast.error((e as Error).message)
      return false
    }
  }, [kick, reload, endSession])

  const command = useCallback(async (body: CommandBody) => {
    if (!navigator.onLine) { toast.error("Reconnect before sending a command."); return null }
    try {
      const out = await api<{ key: string; status: string; agent: string }>("/desk/command", body)
      const c: SentCommand = { text: body.text, at: Date.now(), key: out.key, agent: out.agent }
      setSent(xs => [c, ...xs.filter(x => x.key !== c.key)].slice(0, 8))
      // Accepted and recorded; not started. The palette's "Recent commands" follows the task from here.
      toast(`Command accepted · ${out.key} recorded as ${out.status} for ${out.agent}`, {
        description: "Not started yet. Its progress follows the task on Home and under ⌘K.",
        action: { label: "Open", onClick: () => open({ kind: "task", key: out.key }) },
      })
      kick()
      return c
    } catch (e) {
      // A refused action (403) may be a missing permission, not a lost session: re-read once. If the session is gone
      // that read fails too and the Desk stays cleared; a live stream would otherwise never bring the data back.
      if (sessionLost(e)) { endSession(e); void reload() }
      toast.error(`Command not accepted: ${(e as Error).message}`)
      return null
    }
  }, [kick, reload, endSession])

  const value = useMemo(() => ({ data, link, lastOk, loading, error, online, reload, route, go, detail, open, act, command, sent, composing, compose, seed, sending, startSend, settleSend }),
    [data, link, lastOk, loading, error, online, reload, route, go, detail, act, command, sent, composing, compose, seed, sending, startSend, settleSend])
  const draftValue = useMemo(() => ({ draft, setDraft, focusDraft, takeFocus, focusTick }), [draft, focusDraft, takeFocus, focusTick])
  return (
    <DeskContext.Provider value={value}>
      <DraftContext.Provider value={draftValue}>
        <ChangesContext.Provider value={changes}>
          <ClockProvider>{children}</ClockProvider>
          <LiveAnnouncer changes={changes} />
        </ChangesContext.Provider>
      </DraftContext.Provider>
    </DeskContext.Provider>
  )
}

// Recent changes live in their own context: only rows that show a change re-render when one arrives.
const ChangesContext = createContext<Changes>({})

/** Fresh change marks, keyed `task:KEY`, `agent:KEY`, `event:ID`. */
export const useChanges = () => useContext(ChangesContext)
/** The change mark for one row while it is fresh. */
export const useChange = (kind: "task" | "agent" | "event", key: string | number): Change | undefined => useChanges()[`${kind}:${key}`]

/** Screen readers hear status changes; events are left out (Activity already lists them). */
function LiveAnnouncer({ changes }: { changes: Changes }) {
  const latest = Object.entries(changes).filter(([k]) => !k.startsWith("event:")).sort((a, b) => b[1].at - a[1].at).slice(0, 3)
  return <div role="status" aria-live="polite" className="sr-only">{latest.map(([k, c]) => describe(k, c, humanize)).join(". ")}</div>
}

// The clock lives outside the Desk context: a 1s tick in the shared context re-rendered every page each second
// while idle (measured: Overview, BusinessTable, TasksPage, sidebar all at 1 render/s). Only what shows seconds reads
// the second clock; what needs a date or a cadence reads the minute clock. Everything else renders on data change.
const SecondClock = createContext(Date.now())
const MinuteClock = createContext(Date.now())

function ClockProvider({ children }: { children: ReactNode }) {
  const [second, setSecond] = useState(() => Date.now())
  const [minute, setMinute] = useState(() => Date.now())
  useEffect(() => {
    const tick = setInterval(() => {
      const t = Date.now()
      setSecond(t)
      setMinute(m => (Math.floor(t / 60_000) === Math.floor(m / 60_000) ? m : t))
    }, 1_000)
    return () => clearInterval(tick)
  }, [])
  return <MinuteClock.Provider value={minute}><SecondClock.Provider value={second}>{children}</SecondClock.Provider></MinuteClock.Provider>
}

/** Current time, re-rendering the caller every second ("second") or once a minute ("minute"). */
export function useNow(resolution: "second" | "minute"): number {
  return useContext(resolution === "second" ? SecondClock : MinuteClock)
}

/** Shown data is stale once a refresh failed or none succeeded for 90s. Ticks with the second clock. */
/** The live link as the Desk shows it, re-evaluated every second (a silent stream turns stale on its own). */
export function useLink(): Link {
  const { link, online } = useDesk()
  return linkState(link, online, useNow("second"))
}

/** Shown data may be outdated: a read failed, or no read succeeded for 90s while no live stream vouches for it (a
 *  quiet company sends no change notices, so an old read is still current while the stream is live). */
export function useStale(): boolean {
  const { lastOk, error } = useDesk()
  const now = useNow("second")
  const live = useLink() === "live"
  return !!lastOk && (!!error || (!live && now - lastOk > 90_000))
}

export function useDraft() {
  const ctx = useContext(DraftContext)
  if (!ctx) throw new Error("useDraft outside DeskProvider")
  return ctx
}

export function useDesk() {
  const ctx = useContext(DeskContext)
  if (!ctx) throw new Error("useDesk outside DeskProvider")
  return ctx
}

/** Pages render only once data exists; the shell shows the loading/unavailable state. */
export function useData(): DeskState {
  const { data } = useDesk()
  if (!data) throw new Error("useData before first load")
  return data
}
