// Why a stopped task stopped, shown on its list row without opening it. The list read model carries no error field, so
// for the few tasks that need attention (BLOCKED / FAILED, at most MAX) this reads GET /tasks/{key} once per task version
// (key + updated_at) and keeps the recorded reason (lib/task-story.ts). Healthy work costs no call; a failed read leaves
// the row saying where the reason is, never guessing one.
import { useEffect, useState } from "react"
import { api, type DeskState, type TaskDetail } from "./api"
import { story } from "./task-story"

const cache = new Map<string, string | null>()
const inflight = new Set<string>()
const MAX = 8

export function useReasons(d: DeskState): Record<string, string> {
  const stopped = d.tasks.filter(t => ["BLOCKED", "FAILED"].includes(t.status)).slice(0, MAX)
  const signature = stopped.map(t => `${t.key}@${t.updated_at}`).join(",")
  const [, bump] = useState(0)
  useEffect(() => {
    let live = true
    for (const id of signature ? signature.split(",") : []) {
      if (cache.has(id) || inflight.has(id)) continue
      inflight.add(id)
      api<TaskDetail>(`/tasks/${encodeURIComponent(id.slice(0, id.indexOf("@")))}`)
        .then(t => { const b = story(t).blocker; cache.set(id, b && b !== "unrecorded" ? b : null) })
        .catch(() => cache.set(id, null))
        .finally(() => { inflight.delete(id); if (live) bump(n => n + 1) })
    }
    return () => { live = false }
  }, [signature])
  const out: Record<string, string> = {}
  for (const t of stopped) {
    const r = cache.get(`${t.key}@${t.updated_at}`)
    if (r) out[t.key] = r
  }
  return out
}
