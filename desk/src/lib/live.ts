// The Desk's live link: one EventSource on GET /desk/stream (app/desk_stream.py) for the whole app. A `change` notice
// means committed state changed; the Desk re-reads /desk/state, the only source of truth. Notices carry no data, so a
// lost or duplicated one can only cause one extra read, never a wrong screen. Pure apart from the injected
// EventSource and clock, so `npm test` runs it without a browser.

/** connecting: first attempt · live: stream open and recently heard · reconnecting: lost, the browser is retrying ·
 *  stale: open but silent past STALE_AFTER_MS, or the server said it cannot read state · polling: the API refused
 *  the stream (older API), the Desk falls back to timed reads · offline: the device has no network. */
export type Link = "connecting" | "live" | "reconnecting" | "stale" | "polling" | "offline"

export interface LinkSnap { open: boolean; everOpen: boolean; refused: boolean; degraded: boolean; heardAt: number }

export const STALE_AFTER_MS = 45_000   // three missed heartbeats (HEARTBEAT_S = 15 on the server)
export const RETRY_REFUSED_MS = 60_000 // a refused stream (403/404/503) is tried again this often

export const INITIAL: LinkSnap = { open: false, everOpen: false, refused: false, degraded: false, heardAt: 0 }

export function linkState(s: LinkSnap, online: boolean, now: number): Link {
  if (!online) return "offline"
  if (s.refused) return "polling"
  if (!s.open) return s.everOpen ? "reconnecting" : "connecting"
  return s.degraded || now - s.heardAt > STALE_AFTER_MS ? "stale" : "live"
}

interface Source {
  readyState: number
  onopen: ((e: Event) => void) | null
  onerror: ((e: Event) => void) | null
  addEventListener(type: string, fn: (e: Event) => void): void
  close(): void
}
type SourceCtor = new (url: string) => Source

/** Open the stream and keep it open. Returns the cleanup. `onChange` fires once per notice; the caller coalesces. */
export function connectLive({ url, onChange, onSnap, Source, now = Date.now, later = (fn, ms) => setTimeout(fn, ms), cancel = t => clearTimeout(t as number) }: {
  url: string; onChange: () => void; onSnap: (s: LinkSnap) => void; Source: SourceCtor
  now?: () => number; later?: (fn: () => void, ms: number) => unknown; cancel?: (t: unknown) => void
}): () => void {
  let snap = INITIAL, es: Source | null = null, retry: unknown = null, stopped = false
  const set = (patch: Partial<LinkSnap>) => { snap = { ...snap, ...patch }; onSnap(snap) }
  const heard = (patch: Partial<LinkSnap> = {}) => set({ heardAt: now(), degraded: false, ...patch })

  const open = () => {
    if (stopped) return
    es = new Source(url)
    es.onopen = () => heard({ open: true, everOpen: true, refused: false })
    es.addEventListener("change", () => { heard(); onChange() })
    es.addEventListener("heartbeat", () => heard())
    es.addEventListener("unavailable", () => set({ degraded: true }))
    es.onerror = () => {
      // CLOSED (2) after an error means the server answered with an error status: the browser will not retry, so the
      // Desk polls and tries the stream again later. Otherwise the browser is already reconnecting (retry: 3000).
      if (es?.readyState === 2) {
        es.close()
        set({ open: false, refused: true })
        retry = later(open, RETRY_REFUSED_MS)
      } else set({ open: false })
    }
  }
  open()
  return () => { stopped = true; if (retry) cancel(retry); es?.close() }
}
