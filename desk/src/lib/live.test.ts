import assert from "node:assert/strict"
import { test } from "node:test"
import { connectLive, INITIAL, linkState, STALE_AFTER_MS, type LinkSnap } from "./live.ts"

class FakeSource {
  static last: FakeSource
  readyState = 0
  onopen: ((e: Event) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  listeners: Record<string, ((e: Event) => void)[]> = {}
  closed = false
  url: string
  constructor(url: string) { this.url = url; FakeSource.last = this }
  addEventListener(type: string, fn: (e: Event) => void) { (this.listeners[type] ??= []).push(fn) }
  emit(type: string) { for (const fn of this.listeners[type] ?? []) fn(new Event(type)) }
  close() { this.closed = true; this.readyState = 2 }
}

function harness() {
  let t = 1_000, snap: LinkSnap = INITIAL, changes = 0
  const timers: (() => void)[] = []
  const stop = connectLive({ url: "/desk/stream", onChange: () => changes++, onSnap: s => { snap = s }, Source: FakeSource,
    now: () => t, later: fn => timers.push(fn) })
  return { es: () => FakeSource.last, snap: () => snap, changes: () => changes, tick: (ms: number) => { t += ms }, now: () => t, timers, stop }
}

test("live only while the stream is open and recently heard; never live before the first open", () => {
  const h = harness()
  assert.equal(linkState(h.snap(), true, h.now()), "connecting")
  h.es().onopen!(new Event("open"))
  assert.equal(linkState(h.snap(), true, h.now()), "live")
  h.tick(STALE_AFTER_MS + 1)
  assert.equal(linkState(h.snap(), true, h.now()), "stale")      // silent stream: displayed data may be outdated
  h.es().emit("heartbeat")
  assert.equal(linkState(h.snap(), true, h.now()), "live")
  assert.equal(linkState(h.snap(), false, h.now()), "offline")
})

test("each change notice asks for exactly one authoritative re-read; heartbeats ask for none", () => {
  const h = harness()
  h.es().onopen!(new Event("open"))
  h.es().emit("heartbeat")
  h.es().emit("change"); h.es().emit("change")
  assert.equal(h.changes(), 2)
})

test("a lost connection shows reconnecting; the browser's own retry reopens it", () => {
  const h = harness()
  h.es().onopen!(new Event("open"))
  h.es().readyState = 0
  h.es().onerror!(new Event("error"))
  assert.equal(linkState(h.snap(), true, h.now()), "reconnecting")
  h.es().onopen!(new Event("open"))
  assert.equal(linkState(h.snap(), true, h.now()), "live")
})

test("a refused stream (older API) falls back to polling and is retried later, never shown live", () => {
  const h = harness()
  const first = h.es()
  first.readyState = 2
  first.onerror!(new Event("error"))
  assert.equal(linkState(h.snap(), true, h.now()), "polling")
  assert.equal(h.timers.length, 1)
  h.timers[0]()
  assert.notEqual(h.es(), first)                                  // a new attempt
})

test("the server saying it cannot read state is stale, not live", () => {
  const h = harness()
  h.es().onopen!(new Event("open"))
  h.es().emit("unavailable")
  assert.equal(linkState(h.snap(), true, h.now()), "stale")
})

test("cleanup closes the stream and cancels the retry", () => {
  const h = harness()
  h.stop()
  assert.equal(h.es().closed, true)
})
