import assert from "node:assert/strict"
import { test } from "node:test"
import { api, ApiError } from "./api.ts"

test("Access login HTML and denied API responses cannot be read as company data", async () => {
  const original = globalThis.fetch
  try {
    globalThis.fetch = async () => new Response("<html>Sign in</html>", { headers: { "content-type": "text/html" } })
    await assert.rejects(api("/desk/state"), (error: unknown) => error instanceof ApiError && error.status === 0)
    globalThis.fetch = async () => new Response('{"detail":"founder authentication required"}',
      { status: 401, headers: { "content-type": "application/json" } })
    await assert.rejects(api("/desk/state"), (error: unknown) => error instanceof ApiError && error.status === 401)
  } finally { globalThis.fetch = original }
})
