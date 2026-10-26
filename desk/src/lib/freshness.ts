// Per-series freshness, as the API reports it (metrics_store.portfolio, PR #161): every series
// carries its own VERIFIED/STALE. A unit's own `freshness` only says that SOME series is stale
// (or that none exist), so it must never be painted onto one figure. Anything the API sends that
// is not literally VERIFIED or STALE is UNKNOWN: never silently treated as current. Pure
// functions, no imports beyond types, so `npm test` runs them without a browser or a bundler.
import type { Unit } from "./api"

type Series = { freshness?: string | null }
export type Fresh = "VERIFIED" | "STALE" | "UNKNOWN"

/** null = there is no series at all (absent, not stale and not zero). */
export const freshOf = (m?: Series | null): Fresh | null =>
  !m ? null : m.freshness === "VERIFIED" ? "VERIFIED" : m.freshness === "STALE" ? "STALE" : "UNKNOWN"

export const isStale = (m?: Series | null): boolean => freshOf(m) === "STALE"

/** Warn about a figure that is not positively verified: stale OR of unknown freshness. */
export const isUnverified = (m?: Series | null): boolean => { const f = freshOf(m); return f !== null && f !== "VERIFIED" }

export const staleSuffix = (m?: Series | null): string =>
  ({ STALE: " · stale", UNKNOWN: " · unverified", VERIFIED: "" } as const)[freshOf(m) ?? "VERIFIED"]

/** "stale" | "unverified" | "" : the bare word, for places that build their own sentence. */
export const freshLabel = (m?: Series | null): string => staleSuffix(m).replace(/^ · /, "")

export type DataState = "NONE" | "CURRENT" | "PARTIAL" | "STALE" | "UNKNOWN"

export function dataState(u: Pick<Unit, "metrics">): { state: DataState; total: number; stale: number; unknown: number } {
  const total = u.metrics.length
  const stale = u.metrics.filter(isStale).length
  const unknown = u.metrics.filter(m => freshOf(m) === "UNKNOWN").length
  const state: DataState = total === 0 ? "NONE" : stale + unknown === 0 ? "CURRENT"
    : stale === total ? "STALE" : unknown === total ? "UNKNOWN" : "PARTIAL"
  return { state, total, stale, unknown }
}

/** One honest line about a unit's data: counts, never a verdict on every number. */
export function dataSummary(u: Pick<Unit, "metrics">): string {
  const { state, total, stale, unknown } = dataState(u)
  if (state === "NONE") return "No metrics reported"
  if (state === "CURRENT") return `${total} series current`
  if (state === "STALE") return `All ${total} series stale`
  if (state === "UNKNOWN") return `Freshness unknown for all ${total} series`
  if (unknown === 0) return `${stale} of ${total} series stale`
  const parts = [stale && `${stale} stale`, `${unknown} unknown`].filter(Boolean).join(", ")
  return `${parts} of ${total} series`
}
