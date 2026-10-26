// Active watchdog conditions as the System page may state them. An API that predates the field (production at 0ea0fab,
// 2026-10-16) does not send it: that is "not reported", never "0 active" or "none recorded".
import type { DeskState } from "./api"

type Health = DeskState["health"]
export interface WatchdogView { reported: boolean; count: string; empty: string }

export function watchdogView(h: Pick<Health, "watchdog_conditions" | "watchdog_conditions_total" | "watchdog_conditions_truncated">): WatchdogView {
  const list = h.watchdog_conditions
  if (!Array.isArray(list)) return { reported: false, count: "Not reported", empty: "This API version does not report watchdog conditions, so none can be shown as clear." }
  const total = h.watchdog_conditions_total ?? list.length
  return { reported: true, count: h.watchdog_conditions_truncated ? `${list.length} of ${total} · truncated` : String(total), empty: "No active watchdog condition is recorded." }
}
