// Why a metric trend has no chart. A 403 means the Desk's API scope does not include the series route (app/api_auth.py
// DESK lacks GET /desk/metrics/series as of 2026-10-16), which is not a transient load failure. Once the API has refused
// the Desk, further series requests this session would be refused too, so they are not sent.
import { ApiError } from "./api.ts"

export type TrendFailure = "forbidden" | "failed"

// ponytail: one flag per page load; a reload re-checks, so a backend fix is picked up without code changes here.
let refused = false

export const trendRefused = () => refused

export function trendFailure(e: unknown): TrendFailure {
  if (e instanceof ApiError && e.status === 403) { refused = true; return "forbidden" }
  return "failed"
}

export const TREND_MESSAGE: Record<TrendFailure, string> = {
  forbidden: "History is not available to the Desk: the API does not allow this read. The latest value still stands.",
  failed: "The trend could not be loaded. The latest value still stands.",
}

/** Test hook: reset the per-load flag. */
export const resetTrendAccess = () => { refused = false }
