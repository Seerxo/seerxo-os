import { useEffect, useRef } from "react"

/** Dialogs and sheets opened from state (command palette, a table row) have no Radix trigger, so Radix
 *  cannot give focus back on close. This remembers the last control focused outside any dialog and
 *  returns an `onCloseAutoFocus` that restores it; if it is gone, Radix's default applies. */
export function useReturnFocus() {
  const last = useRef<HTMLElement | null>(null)
  useEffect(() => {
    const track = (e: FocusEvent) => { const t = e.target as HTMLElement; if (!t.closest?.("[role=dialog],[role=alertdialog]")) last.current = t }
    document.addEventListener("focusin", track)
    return () => document.removeEventListener("focusin", track)
  }, [])
  return (e: Event) => { if (last.current?.isConnected) { e.preventDefault(); last.current.focus() } }
}
