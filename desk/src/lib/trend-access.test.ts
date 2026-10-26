import assert from "node:assert/strict"
import { test } from "node:test"
import { ApiError } from "./api.ts"
import { resetTrendAccess, trendFailure, trendRefused, TREND_MESSAGE } from "./trend-access.ts"

test("a 403 is 'not allowed', never a generic load failure, and stops further series requests", () => {
  resetTrendAccess()
  assert.equal(trendRefused(), false)
  assert.equal(trendFailure(new ApiError(403, "desk may not GET /desk/metrics/series")), "forbidden")
  assert.equal(trendRefused(), true)
  assert.match(TREND_MESSAGE.forbidden, /not available to the Desk/)
})

test("network errors and 5xx stay retryable load failures and do not stop requests", () => {
  resetTrendAccess()
  assert.equal(trendFailure(new ApiError(500, "boom")), "failed")
  assert.equal(trendFailure(new TypeError("Failed to fetch")), "failed")
  assert.equal(trendRefused(), false)
})
