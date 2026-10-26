/* Network-only: operational pages and API responses never enter Cache Storage. */
self.addEventListener("install", event => event.waitUntil(self.skipWaiting()))
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()))
self.addEventListener("fetch", event => {
  if (event.request.mode !== "navigate") return
  event.respondWith(fetch(event.request).catch(() => new Response(
    '<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#0d1018"><title>SEZ Company · Offline</title><body style="margin:0;min-height:100dvh;display:grid;place-items:center;background:#0d1018;color:#f0f1f4;font:16px system-ui;padding:24px;box-sizing:border-box"><main><h1>Connection unavailable</h1><p>No company data is shown offline. Reconnect and sign in to continue.</p><a href="/" style="display:inline-flex;align-items:center;min-height:44px;color:#f0f1f4">Try again</a></main></body></html>',
    { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } }
  )))
})
