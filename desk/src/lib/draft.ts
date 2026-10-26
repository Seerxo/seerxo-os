// What happens to the Home composer's unsent text. Pure, so `npm test` exercises the behaviour, not the source.

import { ApiError } from "./api.ts"

/** A real sign-out: ApiError.signIn (401, or Access's sign-in page). A network error (fetch TypeError), a 403, a
 *  broken or malformed 200 body, or any other failure keeps the founder's draft. */
export const signedOut = (e: unknown): boolean => e instanceof ApiError && e.signIn

/** The draft after a failed request: cleared only on a real sign-out. */
export const draftAfterError = (e: unknown, draft: string): string => (signedOut(e) ? "" : draft)

/** The draft after `sent` (the raw text at send time) was accepted: only what was typed after it stays, so a second
 *  send never repeats the task. An edit inside the sent text cannot be separated from it and is kept whole. */
export function draftAfterSend(current: string, sent: string): string {
  if (current.startsWith(sent)) return current.slice(sent.length).replace(/^\s+/, "")
  return current.trim() === sent.trim() ? "" : current
}

/** The Home draft after the full dialog recorded a task. Only a dialog seeded from Home touches it, and only the text
 *  actually sent leaves it (an edited or trimmed seed keeps whatever was not sent). */
export const homeAfterCreate = (home: string, seedText: string, sentText: string): string =>
  seedText ? draftAfterSend(home, sentText) : home

/** Text carried from search (⌘K) into the composer: appended to a draft in progress, never replacing it. */
export const handOff = (current: string, instruction: string): string =>
  current.trim() ? `${current.trimEnd()}\n${instruction}` : instruction

/** The full task dialog: open or not, what it was seeded with, and which open's request is in flight. `key` remounts
 *  the dialog, so each open starts from exactly its own seed (no text or options left over from an earlier open). */
export interface Dialog { composing: boolean; seed: { text: string; key: number }; sending: number | null }
export const NO_DIALOG: Dialog = { composing: false, seed: { text: "", key: 0 }, sending: null }

/** Opening always starts fresh (a new key, the seed only if this open carries one), except while a request is in
 *  flight: then nothing opens, so the same instruction cannot be sent twice from a second dialog. */
export const openDialog = (d: Dialog, text?: string): Dialog =>
  d.sending !== null ? d : { composing: true, seed: { text: text ?? "", key: d.seed.key + 1 }, sending: null }
/** Closing is refused while a request is in flight (Esc, outside click, the close button). */
export const closeDialog = (d: Dialog): Dialog => (d.sending !== null ? d : { ...d, composing: false })

/** A send starts for the open dialog; a second start while one is in flight is refused (null: do not POST). */
export const beginSend = (d: Dialog): Dialog | null => (d.composing && d.sending === null ? { ...d, sending: d.seed.key } : null)
/** A send settles. Only the request this dialog started may close it (on success); a stale result changes nothing. */
export const endSend = (d: Dialog, key: number, ok: boolean): Dialog =>
  d.sending !== key ? d : { ...d, sending: null, composing: ok ? false : d.composing }

/** After a failed request: a real sign-out closes the dialog and drops its seed (the private draft it copied) and any
 *  in-flight claim; anything else (network error, 403, broken body) leaves the dialog exactly as it was. */
export const dialogAfterError = (e: unknown, d: Dialog): Dialog =>
  signedOut(e) ? { composing: false, seed: { text: "", key: d.seed.key + 1 }, sending: null } : d
