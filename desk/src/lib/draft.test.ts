// The founder's unsent instruction survives everything but a real sign-out, and a send never repeats itself. `npm test`.
import assert from "node:assert/strict"
import { test } from "node:test"
import { api, ApiError } from "./api.ts"
import { beginSend, closeDialog, dialogAfterError, endSend, draftAfterError, draftAfterSend, handOff, homeAfterCreate, NO_DIALOG, openDialog, signedOut } from "./draft.ts"

const DRAFT = "Investigate why activation dropped"

test("a network error, a 403 or any non-sign-out failure keeps the draft", () => {
  assert.equal(draftAfterError(new TypeError("Failed to fetch"), DRAFT), DRAFT)       // connection drop mid-send
  assert.equal(draftAfterError(new ApiError(403, "forbidden"), DRAFT), DRAFT)
  assert.equal(draftAfterError(new ApiError(500, "boom"), DRAFT), DRAFT)
  assert.equal(draftAfterError(new Error("anything"), DRAFT), DRAFT)
  assert.equal(draftAfterError("not an error", DRAFT), DRAFT)
})

test("a real sign-out clears it", () => {
  assert.equal(draftAfterError(new ApiError(401, "sign in"), DRAFT), "")
  assert.equal(draftAfterError(new ApiError(0, "Sign in again, then reload", true), DRAFT), "")
  assert.equal(draftAfterError(new ApiError(0, "Invalid API response"), DRAFT), DRAFT)  // 0 alone is not a sign-out
  assert.equal(signedOut(new TypeError("Failed to fetch")), false)
})

test("after a send only the text typed after it stays, so ⌘↵ again never duplicates the task", () => {
  assert.equal(draftAfterSend(DRAFT, DRAFT), "")
  assert.equal(draftAfterSend(`${DRAFT}\nand check the pricing page`, DRAFT), "and check the pricing page")
  assert.equal(draftAfterSend(`  ${DRAFT}  `, `  ${DRAFT}  `), "")
  assert.equal(draftAfterSend(DRAFT.trim(), `${DRAFT}  `), "")                    // trailing spaces trimmed meanwhile
  const edited = "Investigate why SIGNUPS dropped"                                 // an edit inside the sent text
  assert.equal(draftAfterSend(edited, DRAFT), edited)
})

/** What api() throws for one real Response (fetch replaced for the call). */
async function failure(response: () => Response): Promise<unknown> {
  const real = globalThis.fetch
  globalThis.fetch = (async () => response()) as typeof fetch
  try { await api("/desk/command", { text: "x" }); return null } catch (e) { return e } finally { globalThis.fetch = real }
}
const json = { "content-type": "application/json" }

test("a broken HTTP 200 body never counts as a sign-out: the draft survives", async () => {
  const terminated = await failure(() => new Response(new ReadableStream({
    start(c) { c.enqueue(new TextEncoder().encode('{"key": "TASK-')); c.error(new TypeError("terminated")) },
  }), { status: 200, headers: json }))
  assert.ok(terminated instanceof ApiError && terminated.status === 0)          // live data still treats it as lost
  assert.equal(draftAfterError(terminated, DRAFT), DRAFT)
  const malformed = await failure(() => new Response('{"key": "TASK-0143",', { status: 200, headers: json }))
  assert.ok(malformed instanceof ApiError)
  assert.equal(draftAfterError(malformed, DRAFT), DRAFT)
  const dropped = await failure(() => { throw new TypeError("Failed to fetch") })
  assert.equal(draftAfterError(dropped, DRAFT), DRAFT)
  const forbidden = await failure(() => new Response('{"detail":"forbidden"}', { status: 403, headers: json }))
  assert.equal(draftAfterError(forbidden, DRAFT), DRAFT)
  for (const status of [500, 502, 503, 504]) {                                          // transient server errors
    const e = await failure(() => new Response("<html>Bad gateway</html>", { status, headers: { "content-type": "text/html" } }))
    assert.ok(e instanceof ApiError && e.status === status && !e.signIn)
    assert.equal(draftAfterError(e, DRAFT), DRAFT, String(status))
  }
  const empty = await failure(() => new Response("", { status: 200, headers: json }))           // unexpected body
  assert.equal(draftAfterError(empty, DRAFT), DRAFT)
})

/** A real Response as fetch returns it after following redirects to `url` (redirected/url are read-only otherwise). */
const landed = (url: string, body: string, type = "text/html") => {
  const r = new Response(body, { status: 200, headers: { "content-type": type } })
  Object.defineProperties(r, { redirected: { value: true }, url: { value: url } })
  return r
}
const ACCESS = "https://example.cloudflareaccess.com/cdn-cgi/access/login/desk.example.invalid?redirect_url=%2Fdesk%2Fstate"

test("a genuine sign-out clears it: a redirect to the Access login page, or 401", async () => {
  const page = await failure(() => landed(ACCESS, "<html>Sign in · Cloudflare Access</html>"))
  assert.ok(page instanceof ApiError && page.signIn)
  const bare = await failure(() => landed("https://team.cloudflareaccess.com/cdn-cgi/access/login", "<html>Sign in</html>"))
  assert.ok(bare instanceof ApiError && bare.signIn)                                       // the exact path counts too
  assert.equal(draftAfterError(page, DRAFT), "")
  const unauthorized = await failure(() => new Response('{"detail":"sign in"}', { status: 401, headers: json }))
  assert.equal(draftAfterError(unauthorized, DRAFT), "")
})

test("other HTML on 200 is unreadable, not a sign-out: the draft survives", async () => {
  const cases = [
    () => new Response("<html>Sign in</html>", { status: 200, headers: { "content-type": "text/html" } }),   // no evidence
    () => landed("https://desk.example.invalid/cdn-cgi/challenge-platform/h/b/orchestrate", "<html>Just a moment...</html>"),
    () => landed("https://proxy.example.invalid/login", "<html>Captive portal</html>"),
    () => landed("http://evil.cloudflareaccess.com/cdn-cgi/access/login/x", "<html>x</html>"),                 // not https
    () => landed("https://cloudflareaccess.com.example.invalid/cdn-cgi/access/login/x", "<html>x</html>"),      // look-alike
    () => landed("https://team.cloudflareaccess.com/cdn-cgi/access/certs", "<html>x</html>"),                   // not the login
    () => landed("https://example.cloudflareaccess.com/cdn-cgi/access/login-challenge", "<html>x</html>"),      // login look-alike
    () => landed("https://example.cloudflareaccess.com/cdn-cgi/access/loginish/desk", "<html>x</html>"),
    () => landed("https://example.cloudflareaccess.com/x/cdn-cgi/access/login/desk", "<html>x</html>"),         // not at the root
  ]
  for (const make of cases) {
    const e = await failure(make)
    assert.ok(e instanceof ApiError && e.status === 0 && !e.signIn)
    assert.equal(draftAfterError(e, DRAFT), DRAFT)
  }
})

test("search hands text to the composer by appending, never replacing a draft", () => {
  assert.equal(handOff("", "audit pricing"), "audit pricing")
  assert.equal(handOff("   ", "audit pricing"), "audit pricing")
  assert.equal(handOff("First half  \n", "audit pricing"), "First half\naudit pricing")
})

test("each dialog open starts fresh: seeded 'A', closed, reopened unseeded carries nothing and cannot resend 'A'", () => {
  const seeded = openDialog(NO_DIALOG, "A")
  assert.deepEqual(seeded, { composing: true, seed: { text: "A", key: 1 }, sending: null })
  const plain = openDialog(closeDialog(seeded))
  assert.deepEqual(plain, { composing: true, seed: { text: "", key: 2 }, sending: null })   // new key: the dialog remounts blank
  // A successful create only touches the Home draft when the dialog was seeded; an unseeded one leaves "A" alone.
  const draftAfterCreate = (draft: string, d: typeof plain) => (d.seed.text ? draftAfterSend(draft, d.seed.text) : draft)
  assert.equal(draftAfterCreate("A", plain), "A")
  assert.equal(draftAfterCreate("A", seeded), "")
  assert.equal(openDialog(plain, "B").seed.key, 3)                            // every open bumps, seeded or not
})

test("sign-out, recovery, reopen: no private text survives in the draft, the seed or the dialog", async () => {
  const open = openDialog(NO_DIALOG, DRAFT)
  for (const make of [() => new Response('{"detail":"x"}', { status: 401, headers: json }), () => landed(ACCESS, "<html>login</html>")]) {
    const e = await failure(make)
    const d = dialogAfterError(e, open)
    assert.equal(draftAfterError(e, DRAFT), "")
    assert.deepEqual(d, { composing: false, seed: { text: "", key: open.seed.key + 1 }, sending: null })
    // Data comes back and the founder reopens the dialog: it is blank.
    assert.equal(openDialog(d).seed.text, "")
  }
  // A broken body, 403 or network error keeps everything exactly as it was.
  for (const make of [() => new Response("{", { status: 200, headers: json }), () => new Response('{"detail":"no"}', { status: 403, headers: json }),
    () => { throw new TypeError("Failed to fetch") }]) {
    const e = await failure(make)
    assert.equal(draftAfterError(e, DRAFT), DRAFT)
    assert.equal(dialogAfterError(e, open), open)
  }
})

test("More options: only the text actually sent leaves Home, also when the request is slow", async () => {
  const seed = "Private founder instruction\nAudit pricing"
  // The founder trims the seeded dialog to its first line and sends; Home keeps the unsent second line.
  assert.equal(homeAfterCreate(seed, seed, "Private founder instruction"), "Audit pricing")
  // An unseeded dialog never touches Home.
  assert.equal(homeAfterCreate(seed, "", seed), seed)
  // A real async send: Home is edited while the request is pending; on 201 only the sent part is removed.
  let resolve!: (v: unknown) => void
  const pending = new Promise(r => { resolve = r })
  let home = seed
  const submit = (async () => { const sent = seed; await pending; home = homeAfterCreate(home, seed, sent) })()
  home += "\nInvestigate conversion"                     // typed during the request
  resolve({ key: "TASK-1", status: "READY" })
  await submit
  assert.equal(home, "Investigate conversion")
})

test("a dialog send in flight: no close, no reopen, no second POST; its late result never touches a later dialog", async () => {
  // The real interaction, driven through the same transitions DeskProvider applies, with a slow async request.
  let dialog = openDialog(NO_DIALOG, "A")
  const posts: string[] = []
  let resolve!: (ok: boolean) => void
  const send = async () => {
    const next = beginSend(dialog)
    if (!next) return                                   // refused: already in flight
    dialog = next
    const key = next.sending!
    posts.push(dialog.seed.text)
    const ok = await new Promise<boolean>(r => { resolve = r })
    dialog = endSend(dialog, key, ok)
  }
  const first = send()
  void send()                                           // double click / Cmd+Enter again
  dialog = closeDialog(dialog)                          // Esc, outside click, close button
  dialog = openDialog(dialog)                           // "c", More options, palette
  dialog = openDialog(dialog, "B")
  assert.deepEqual(dialog, { composing: true, seed: { text: "A", key: 1 }, sending: 1 })
  resolve(true)
  await first
  assert.deepEqual(posts, ["A"])                        // exactly one POST
  assert.deepEqual(dialog, { composing: false, seed: { text: "A", key: 1 }, sending: null })

  // A later dialog is never closed or reset by an earlier request's late result.
  dialog = openDialog(dialog)
  const later = dialog
  dialog = endSend(dialog, 1, true)
  dialog = endSend(dialog, 1, false)
  assert.equal(dialog, later)
  // A failed send keeps the dialog open for a retry.
  const retry = beginSend(later)!
  assert.deepEqual(endSend(retry, later.seed.key, false), { ...later, sending: null })
  // A sign-out mid-flight releases the claim; the old request's late result then changes nothing.
  const out = dialogAfterError(new ApiError(401, "x"), retry)
  assert.equal(endSend(out, later.seed.key, true), out)
  assert.equal(beginSend(NO_DIALOG), null)              // nothing to send from a closed dialog
})
