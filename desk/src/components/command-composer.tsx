// The Home command composer: say what should happen, send it explicitly, then follow it. POST /desk/command records a
// READY task for the CEO and nothing more; each later step is read from that task's own status (lib/lifecycle.ts), so
// "sent" is never shown as "done". Search lives in ⌘K and never submits from here.
import { useEffect, useRef, useState } from "react"
import { ArrowUp, Loader2, SlidersHorizontal } from "lucide-react"
import { commandLifecycle } from "@/lib/lifecycle"
import { draftAfterSend } from "@/lib/draft"
import { useData, useDesk, useDraft } from "@/state"
import { CommandTrack } from "@/components/desk"
import { Button } from "@/components/ui/button"

const EXAMPLE = "Investigate why Seerxo activation dropped and recommend two fixes"

export function CommandComposer() {
  const d = useData()
  const { command, sent, compose, open } = useDesk()
  const { draft, setDraft, takeFocus, focusTick } = useDraft()
  const [busy, setBusy] = useState(false)
  const area = useRef<HTMLTextAreaElement>(null)
  // A hand-off from search focuses the composer once it is on screen (here, not on a timer), caret at the end.
  useEffect(() => {
    const el = area.current
    if (!el || !takeFocus()) return
    const id = requestAnimationFrame(() => { el.focus(); el.setSelectionRange(el.value.length, el.value.length) })
    return () => cancelAnimationFrame(id)
  }, [focusTick, takeFocus])
  const text = draft.trim()
  const send = async () => {
    if (busy || text.length < 3) return
    const raw = draft
    setBusy(true)
    const c = await command({ text, agent: "ceo", priority: "P2" })
    setBusy(false)
    if (c) setDraft(cur => draftAfterSend(cur, raw))   // only text typed after the sent part stays
    area.current?.focus()
  }
  const status = (key: string) => d.tasks.find(t => t.key === key)?.status
  return (
    <section aria-label="Instruct the CEO" className="grid gap-3">
      <form className="panel grid gap-2 p-3 focus-within:border-ring focus-within:ring-2 focus-within:ring-ring/30 sm:p-4" onSubmit={e => { e.preventDefault(); void send() }}>
        <label htmlFor="ceo-command" className="sr-only">What should the company do?</label>
        <textarea id="ceo-command" ref={area} value={draft} onChange={e => setDraft(e.target.value)} rows={2} maxLength={2000}
          placeholder={`Tell the CEO what you want done, e.g. “${EXAMPLE}”`}
          onKeyDown={e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void send() } }}
          className="min-h-[4.5rem] w-full resize-none sm:min-h-14 bg-transparent text-[15px] leading-6 outline-none placeholder:text-muted-foreground/80" />
        <div className="flex flex-wrap items-center justify-between gap-2">
          {/* POST /desk/command always records READY for the chosen agent (app/main.py); a pause is said, not hidden. */}
          <span className="text-xs text-muted-foreground">{d.control.agents_paused
            ? <>Recorded as a READY task for the CEO. <span className="text-warning">Agents are paused: it waits until you resume them.</span></>
            : "Recorded as a READY task for the CEO. Nothing runs until an agent picks it up."}</span>
          <span className="flex items-center gap-1.5">
            <Button type="button" variant="ghost" size="sm" className="h-9 text-muted-foreground" disabled={busy} onClick={() => compose(true, draft)}>
              <SlidersHorizontal />More options</Button>
            <Button type="submit" size="sm" className="h-9" disabled={busy || text.length < 3} aria-label="Send to the CEO">
              {busy ? <Loader2 className="animate-spin" /> : <ArrowUp />}Send<kbd className="ml-1 hidden text-[10px] opacity-70 sm:inline">⌘↵</kbd></Button>
          </span>
        </div>
      </form>
      {sent.length > 0 && (
        <ul aria-label="What you asked for" className="grid gap-1">
          {sent.slice(0, 3).map(c => (
            <li key={c.key}>
              <button type="button" onClick={() => open({ kind: "task", key: c.key })}
                className="flex w-full min-w-0 items-center gap-3 rounded-lg px-2 py-1.5 text-left text-[13px] transition-colors hover:bg-muted/50 focus-visible:outline-2 focus-visible:outline-ring">
                <span className="min-w-0 flex-1 truncate">{c.text}</span>
                <span className="shrink-0 font-mono text-[11px] text-muted-foreground">{c.key}</span>
                <CommandTrack l={commandLifecycle(status(c.key), true)} compact />
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
