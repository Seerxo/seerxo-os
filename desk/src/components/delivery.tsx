// Code missions in flight, each on its own Stage C track (lib/delivery.ts). Shared by the Command Center, Work and
// Automation so a mission reads the same everywhere. Opening a row opens the task: its runs and evidence live there.
import { useState, type ReactNode } from "react"
import type { Improvement } from "@/lib/api"
import { ago } from "@/lib/format"
import { DELIVERY_STAGES, deliveryView, type DeliveryView } from "@/lib/delivery"
import { cn } from "@/lib/utils"
import { useDesk } from "@/state"
import { DOT, Panel, PanelEmpty, PanelRow, Status, TaskKey } from "@/components/desk"

export function DeliveryTrack({ v, className }: { v: DeliveryView; className?: string }) {
  const n = DELIVERY_STAGES.length
  return (
    <span role="img" aria-label={v.index === null ? v.label : `${v.label}, step ${v.index + 1} of ${n}`}
      className={cn("flex items-center gap-[3px]", className)}>
      {DELIVERY_STAGES.map(([k, label], i) => (
        <i key={k} title={label} className={cn("h-1.5 flex-1 rounded-full transition-colors",
          v.index === null ? "bg-border" : i < v.index ? "bg-foreground/40" : i === v.index ? DOT[v.tone] : "bg-border")} />
      ))}
    </span>
  )
}

export function DeliveryRow({ i }: { i: Improvement }) {
  const { open } = useDesk()
  const v = deliveryView(i)
  return (
    <PanelRow onClick={() => open({ kind: "task", key: i.task })} label={`${i.title}, ${v.label}`} className="items-center"
      title={i.title}
      meta={<span className="grid gap-1.5">
        <span><TaskKey k={i.task} />{v.evidence.length > 0 && <> · {v.evidence.join(" · ")}</>}{i.updated_at && <> · {ago(i.updated_at)} ago</>}</span>
        <DeliveryTrack v={v} className="max-w-[22rem]" />
      </span>}
      trail={<Status value={i.stage} label={v.label} tone={v.tone} />} />
  )
}

/** In-flight code missions. `limit` keeps the Command Center short; the full list lives on Automation. */
export function DeliveryPanel({ items, limit, action, expandable }: { items: Improvement[] | undefined; limit?: number; action?: ReactNode; expandable?: boolean }) {
  const [all, setAll] = useState(false)
  if (items === undefined) return (
    <Panel title="Code missions" description="Each on its own pipeline"><PanelEmpty title="Not reported">This API version does not report code missions.</PanelEmpty></Panel>
  )
  const shown = limit && !all ? items.slice(0, limit) : items
  return (
    <Panel title="Code missions" count={items.length} description="Each row is one task on its own pipeline, detected to measured" action={action}>
      {shown.length ? shown.map(i => <DeliveryRow key={i.task} i={i} />)
        : <PanelEmpty title="No code mission in flight">Missions appear here from detection until their effect is measured.</PanelEmpty>}
      {expandable && items.length > shown.length && <button type="button" onClick={() => setAll(true)}
        className="flex w-full items-center justify-center px-4 py-2.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted/40 hover:text-foreground">
        Show all {items.length} · {items.length - shown.length} more</button>}
    </Panel>
  )
}
