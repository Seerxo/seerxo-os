import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"
import { runInNewContext } from "node:vm"

const source = readFileSync(new URL("../../public/sw.js", import.meta.url), "utf8")

test("service worker uses the network for pages, never intercepts API calls or caches data", async () => {
  const handlers: Record<string, (event: { request: { mode: string }; respondWith: (p: Promise<Response>) => void }) => void> = {}
  const self = { addEventListener: (name: string, fn: typeof handlers[string]) => { handlers[name] = fn },
    skipWaiting: () => Promise.resolve(), clients: { claim: () => Promise.resolve() } }
  let calls = 0
  runInNewContext(source, { self, Response, fetch: async () => { calls++; return new Response("live response") } })
  let response: Promise<Response> | undefined
  handlers.fetch({ request: { mode: "cors" }, respondWith: p => { response = p } })
  assert.equal(response, undefined)
  handlers.fetch({ request: { mode: "navigate" }, respondWith: p => { response = p } })
  assert.equal((await response!).status, 200)
  assert.equal(await (await response!).text(), "live response")
  assert.equal(calls, 1)

  runInNewContext(source, { self, Response, fetch: async () => { throw new Error("offline") } })
  handlers.fetch({ request: { mode: "navigate" }, respondWith: p => { response = p } })
  const offline = await response!
  assert.match(await offline.text(), /No company data is shown offline/)
  assert.equal(offline.headers.get("Cache-Control"), "no-store")
})
