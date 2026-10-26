// SEZ Command primitives on top of shadcn/ui. Every page is built from these, so the language stays one.
//
// Type roles (use these, not ad-hoc sizes):
//   page title 22/600 · section title 13/600 · object title 14/500 · value tabular
//   body 14/400 · secondary 13 muted · metadata 12 muted · identifier 12 mono · eyebrow 11 caps
// Surfaces: the page background holds sections (heading + hairline, no box). A bordered surface is only for
// an object that is acted on (approval, incident, composer) or elevated (dialog, sheet, popover).
// Truth: every status shows its label (never colour alone); missing data reads UNKNOWN, never 0.
import { useLayoutEffect, useRef, useState, type ReactNode } from "react"
import { ChevronRight } from "lucide-react"
import { cn } from "@/lib/utils"
import type { Tone } from "@/lib/api"
import { humanize, toneOf } from "@/lib/format"
import type { Change } from "@/lib/changes"
import { TRACK, trackIndex, type Lifecycle } from "@/lib/lifecycle"

export const TEXT: Record<Tone, string> = {
  success: "text-success", info: "text-info", warning: "text-warning", danger: "text-destructive", neutral: "text-muted-foreground",
}
export const DOT: Record<Tone, string> = {
  success: "bg-success", info: "bg-info", warning: "bg-warning", danger: "bg-destructive", neutral: "bg-muted-foreground/60",
}
const PILL: Record<Tone, string> = {
  success: "bg-success/10 text-success", info: "bg-info/10 text-info", warning: "bg-warning/12 text-warning",
  danger: "bg-destructive/10 text-destructive", neutral: "bg-muted text-muted-foreground",
}

/** Quiet status: dot + label. `pill` only where the status is the object's headline; `live` only for work an agent is
 *  executing right now (the one repeating motion in the Desk). */
export function Status({ value, tone, label, pill, live, className }: {
  value: string | null | undefined; tone?: Tone; label?: string; pill?: boolean; live?: boolean; className?: string
}) {
  const t = tone ?? toneOf(value)
  return (
    <span className={cn("inline-flex shrink-0 items-center gap-1.5 text-xs font-medium whitespace-nowrap",
      pill ? cn("h-5 rounded-full px-2", PILL[t]) : TEXT[t], className)}>
      <span className={cn("size-1.5 rounded-full", DOT[t], live && "desk-pulse")} aria-hidden />
      {label ?? humanize(value ?? "UNKNOWN")}
    </span>
  )
}

export function Tag({ children, tone = "neutral", className }: { children: ReactNode; tone?: Tone; className?: string }) {
  return <span className={cn("inline-flex h-[18px] shrink-0 items-center whitespace-nowrap rounded-[4px] px-1.5 font-mono text-[11px] leading-none font-medium", PILL[tone], className)}>{children}</span>
}

export const priorityTone = (p: string): Tone => (p === "P0" ? "danger" : p === "P1" ? "warning" : "neutral")

/** A page section: heading row over a hairline, content on the page surface. */
export function Section({ title, description, action, count, children, className, flush }: {
  title: ReactNode; description?: ReactNode; action?: ReactNode; count?: number; children: ReactNode; className?: string; flush?: boolean
}) {
  return (
    <section className={cn("grid min-w-0 content-start gap-3", className)}>
      <header className="flex min-h-8 items-end justify-between gap-3 border-b border-border/80 pb-2">
        <div className="min-w-0">
          <h2 className="flex items-baseline gap-2 text-[15px] font-semibold tracking-tight">
            {title}{count !== undefined && <span className="num text-xs font-normal text-muted-foreground">{count}</span>}
          </h2>
          {description && <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>}
        </div>
        {action && <div className="flex max-w-[65%] min-w-0 flex-wrap items-center justify-end gap-x-2 text-right text-xs text-muted-foreground">{action}</div>}
      </header>
      <div className={cn(!flush && "min-w-0")}>{children}</div>
    </section>
  )
}

/** Inline calm/empty state: a title and one line. */
export function Calm({ title, children, status }: { title?: string; children?: ReactNode; status?: ReactNode }) {
  return (
    <div className="grid gap-0.5 py-2.5 text-sm">
      <div className="flex items-center gap-2 font-medium">{status}{title}</div>
      {children && <p className="text-[13px] text-muted-foreground">{children}</p>}
    </div>
  )
}

/** Page-level empty state: a quiet framed block with a glyph, one sentence and at most one action. */
export function EmptyState({ icon, title, children, action }: { icon?: ReactNode; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="grid justify-items-center gap-2 rounded-xl border border-dashed px-6 py-12 text-center">
      {icon && <div className="mb-1 grid size-9 place-items-center rounded-lg bg-muted text-muted-foreground [&_svg]:size-4">{icon}</div>}
      <p className="text-sm font-medium">{title}</p>
      {children && <p className="max-w-sm text-[13px] text-muted-foreground">{children}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

export function Facts({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(104px,max-content)_minmax(0,1fr)] gap-x-6 gap-y-2 text-[13px]">
      {items.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted-foreground">{k}</dt>
          <dd className="min-w-0 [overflow-wrap:anywhere]">{v == null || v === "" ? <span className="text-muted-foreground">No data yet</span> : v}</dd>
        </div>
      ))}
    </dl>
  )
}

/** Label/value row with a hairline; values right-aligned. */
export function Row({ label, children, hint }: { label: string; children: ReactNode; hint?: ReactNode }) {
  return (
    <div className="flex min-h-9 items-center justify-between gap-4 border-b border-border/60 py-1.5 text-[13px] last:border-0">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className="flex min-w-0 flex-wrap items-center justify-end gap-x-2 gap-y-0.5 text-right">
        {children}{hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      </span>
    </div>
  )
}

export function PageHeader({ title, description, meta, children, eyebrow }: {
  title: ReactNode; description?: ReactNode; meta?: ReactNode; children?: ReactNode; eyebrow?: ReactNode
}) {
  return (
    <header className="page-heading flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
      <div className="grid min-w-0 gap-1.5">
        {eyebrow && <span className="eyebrow">{eyebrow}</span>}
        <h1>{title}</h1>
        {description && <p className="text-muted-foreground">{description}</p>}
      </div>
      {(meta || children) && <div className="flex flex-wrap items-center gap-3 text-[13px] text-muted-foreground">{meta}{children}</div>}
    </header>
  )
}

/** "2 active · 1 attention" style header summary; zero parts are dropped. */
export function Summary({ parts }: { parts: [number, string, Tone?][] }) {
  const shown = parts.filter(([n]) => n)
  if (!shown.length) return null
  return (
    <span className="num flex flex-wrap items-center gap-x-1.5">
      {shown.map(([n, label, tone], i) => (
        <span key={label} className="flex items-center gap-1.5">
          {i > 0 && <span className="text-border">·</span>}
          <b className={cn("font-medium text-foreground", tone && TEXT[tone])}>{n}</b>{label}
        </span>
      ))}
    </span>
  )
}

export interface StatItem { label: string; value: ReactNode; sub?: ReactNode; onClick?: () => void; title?: string }

/** A row of headline figures separated by hairlines: no boxes, no oversized numbers. Each opens what explains it. */
export function StatStrip({ items, className }: { items: StatItem[]; className?: string }) {
  return (
    <div className={cn("grid grid-cols-2 border-y sm:flex sm:divide-x", className)}>
      {items.map((s, i) => {
        const body = <>
          <span className="text-xs text-muted-foreground">{s.label}</span>
          <span className="num text-xl leading-7 font-semibold tracking-tight">{s.value}</span>
          {s.sub && <span className="num text-xs leading-snug text-pretty text-muted-foreground">{s.sub}</span>}
        </>
        const cls = cn("grid min-w-0 flex-1 content-start gap-0.5 px-4 py-3 text-left sm:first:pl-0",
          i % 2 === 1 && "max-sm:border-l", i > 1 && "max-sm:border-t")
        return s.onClick
          ? <button key={s.label} title={s.title} onClick={s.onClick} className={cn(cls, "group transition-colors duration-150 hover:bg-muted/40 focus-visible:bg-muted/40 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring sm:first:-ml-0")}>{body}</button>
          : <div key={s.label} title={s.title} className={cls}>{body}</div>
      })}
    </div>
  )
}

export interface Figure { label: string; value: string; sub?: ReactNode; tone?: Tone; onClick?: () => void }

/** The headline figures of a page: big tabular numbers on hairline-divided cells. Each can open what explains it. */
export function FigureStrip({ items, label = "Key figures" }: { items: Figure[]; label?: string }) {
  const cols = items.length === 3 ? "lg:grid-cols-3" : items.length === 2 ? "lg:grid-cols-2" : "lg:grid-cols-4"
  return (
    <section aria-label={label} className={cn("grid grid-cols-2 gap-px overflow-hidden rounded-xl border bg-border", cols)}>
      {items.map(c => {
        const cls = cn("grid min-w-0 content-start gap-1 bg-card px-4 py-3.5 text-left sm:px-5 sm:py-4", items.length === 3 && "max-lg:last:col-span-2")
        const body = <>
          <span className="eyebrow">{c.label}</span>
          <span className={cn("metric", c.tone === "danger" && "text-destructive", c.tone === "warning" && "text-warning")}>{c.value}</span>
          {c.sub && <span className="text-xs leading-snug text-pretty text-muted-foreground">{c.sub}</span>}
        </>
        return c.onClick
          ? <button key={c.label} type="button" onClick={c.onClick} className={cn(cls, "transition-colors duration-150 hover:bg-muted/50 focus-visible:bg-muted/50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring")}>{body}</button>
          : <div key={c.label} className={cls}>{body}</div>
      })}
    </section>
  )
}

/** Long recorded prose (incident impact, root cause): four lines, then "Show more". The button appears only when the
 *  text is actually cut, measured after layout, so short text never gets a control that does nothing. */
export function ClampText({ children, className }: { children: ReactNode; className?: string }) {
  const ref = useRef<HTMLSpanElement>(null)
  const [open, setOpen] = useState(false)
  const [cut, setCut] = useState(false)
  useLayoutEffect(() => { const e = ref.current; if (e && !open) setCut(e.scrollHeight > e.clientHeight + 1) }, [children, open])
  return (
    <span className="grid justify-items-start gap-1">
      <span ref={ref} className={cn("[overflow-wrap:anywhere]", !open && "line-clamp-4", className)}>{children}</span>
      {(cut || open) && <button type="button" aria-expanded={open} onClick={() => setOpen(o => !o)}
        className="rounded-sm text-xs font-medium text-muted-foreground underline-offset-2 hover:text-foreground hover:underline focus-visible:ring-2 focus-visible:ring-ring/60 focus-visible:outline-none">
        {open ? "Show less" : "Show more"}</button>}
    </span>
  )
}

/** Detail kept one click away: native <details>, closed by default. The summary says what is inside. */
export function Disclosure({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <details className="group grid min-w-0 grid-cols-1 rounded-xl border bg-card open:gap-6 open:pb-5">
      <summary className="flex min-h-12 min-w-0 cursor-pointer list-none items-center gap-2 rounded-xl px-4 text-[13px] transition-colors hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-ring [&::-webkit-details-marker]:hidden">
        <ChevronRight aria-hidden className="size-4 text-muted-foreground transition-transform duration-150 group-open:rotate-90" />
        <span className="shrink-0 font-semibold">{title}</span>{hint && <span className="min-w-0 truncate text-xs text-muted-foreground">{hint}</span>}
      </summary>
      <div className="grid min-w-0 gap-10 px-4 sm:px-5">{children}</div>
    </details>
  )
}

/** One record as one line (native <details>): the summary names it, the body holds the full text. For lists of prose
 *  (decisions, learnings, facts) where every item at full length made the page several screens long. */
export function Expandable({ summary, children }: { summary: ReactNode; children: ReactNode }) {
  return (
    <details className="group min-w-0">
      <summary className="flex min-h-11 min-w-0 cursor-pointer list-none items-center gap-3 py-2.5 text-sm transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring [&::-webkit-details-marker]:hidden">
        <ChevronRight aria-hidden className="size-4 shrink-0 text-muted-foreground transition-transform duration-150 group-open:rotate-90" />
        <span className="flex min-w-0 flex-1 items-center gap-3">{summary}</span>
      </summary>
      <div className="grid min-w-0 gap-2 pb-4 pl-7 [overflow-wrap:anywhere]">{children}</div>
    </details>
  )
}

export const TaskKey = ({ k, className }: { k: string; className?: string }) => <span className={cn("font-mono text-xs text-muted-foreground", className)}>{k}</span>

export const Muted = ({ children, className }: { children: ReactNode; className?: string }) => <span className={cn("text-muted-foreground", className)}>{children}</span>

/** Clickable list row: hover surface; the row is the target. */
export function ListRow({ onClick, children, className }: { onClick?: () => void; children: ReactNode; className?: string }) {
  return (
    <button type="button" onClick={onClick}
      className={cn("group/row -mx-2 flex w-[calc(100%+1rem)] items-center gap-3 rounded-md px-2 py-2 text-left text-sm transition-colors duration-150 hover:bg-muted/60 focus-visible:bg-muted/60 focus-visible:ring-2 focus-visible:ring-ring/60 focus-visible:outline-none", className)}>
      {children}
    </button>
  )
}

/** A founder command's place on its track (Submitted → Queued → Executing → Review → Done), read only from the
 *  task's status. A stopped or unknown command is not drawn on the track: it shows its own state instead. */
export function CommandTrack({ l, compact }: { l: Lifecycle; compact?: boolean }) {
  const at = trackIndex(l.step)
  if (at === null) return <Status value={l.step} label={l.label} tone={l.tone} />
  return (
    <span className="inline-flex items-center gap-2" title={l.detail} aria-label={`${l.label}: ${l.detail}`}>
      <span className="flex items-center gap-[3px]" aria-hidden>
        {TRACK.map((s, i) => (
          <i key={s} className={cn("h-1 rounded-full transition-colors duration-200", compact ? "w-2.5" : "w-4",
            i < at ? "bg-foreground/45" : i === at ? (l.step === "running" ? "desk-pulse bg-info" : l.step === "finished" ? "bg-warning" : "bg-foreground") : "bg-border")} />
        ))}
      </span>
      <span className={cn("text-xs font-medium whitespace-nowrap", TEXT[l.tone] === "text-muted-foreground" ? "text-foreground" : TEXT[l.tone])}>{l.label}</span>
    </span>
  )
}

/** A bordered list surface: the one container for rows of objects (tasks, decisions, units). A heading row sits
 *  above it on the page, never inside a second card. */
export function Panel({ title, action, children, className, description, count }: {
  title?: ReactNode; action?: ReactNode; children: ReactNode; className?: string; description?: ReactNode; count?: number
}) {
  return (
    <section className={cn("grid min-w-0 content-start gap-2.5", className)}>
      {title && (
        <header className="flex min-h-7 items-end justify-between gap-3 px-0.5">
          <div className="min-w-0">
            <h2 className="flex items-baseline gap-2 text-[13px] leading-6 font-semibold tracking-[-0.005em]">
              {title}{count !== undefined && <span className="num rounded-full bg-muted px-1.5 text-[11px] leading-[18px] font-medium text-muted-foreground">{count}</span>}
            </h2>
            {description && <p className="text-xs text-muted-foreground">{description}</p>}
          </div>
          {action && <div className="flex max-w-[65%] min-w-0 flex-wrap items-center justify-end gap-x-3 text-xs text-muted-foreground">{action}</div>}
        </header>
      )}
      <div className="panel min-w-0 divide-y divide-border/70">{children}</div>
    </section>
  )
}

/** One object in a Panel. The whole row is the target; `lead` is a status mark, `trail` a status or value. */
export function PanelRow({ onClick, lead, title, meta, trail, tone, className, label }: {
  onClick?: () => void; lead?: ReactNode; title: ReactNode; meta?: ReactNode; trail?: ReactNode; tone?: Tone; className?: string; label?: string
}) {
  const body = <>
    {lead && <span className="mt-[3px] flex w-4 shrink-0 justify-center self-start">{lead}</span>}
    <span className="grid min-w-0 flex-1 gap-0.5">
      <span className="line-clamp-2 text-[14px] leading-5 font-medium [overflow-wrap:anywhere] sm:truncate sm:[display:block]">{title}</span>
      {meta && <span className={cn("line-clamp-2 text-[13px] leading-5 text-muted-foreground [overflow-wrap:anywhere]", tone && tone !== "neutral" && TEXT[tone])}>{meta}</span>}
    </span>
    {/* Phones: the trail (status, action) moves under the text so titles keep the full width. */}
    {trail && <span className="flex shrink-0 items-center gap-2 self-center text-xs text-muted-foreground max-sm:order-last max-sm:w-full max-sm:pl-7">{trail}</span>}
  </>
  const cls = cn("flex w-full items-start gap-x-3 gap-y-1.5 px-4 py-3 text-left first:rounded-t-[inherit] last:rounded-b-[inherit] max-sm:flex-wrap", className)
  return onClick
    ? <button type="button" aria-label={label} onClick={onClick} className={cn(cls, "transition-colors duration-150 hover:bg-muted/50 focus-visible:bg-muted/50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring")}>{body}</button>
    : <div className={cls}>{body}</div>
}

/** A quiet line inside a Panel when it has nothing to list: what that means and, at most, one next step. */
export function PanelEmpty({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="grid gap-1 px-4 py-5 text-sm">
      <span className="font-medium">{title}</span>
      {children && <span className="text-[13px] text-muted-foreground">{children}</span>}
      {action && <span className="mt-1.5">{action}</span>}
    </div>
  )
}

export const Dot = ({ tone, live, className }: { tone: Tone; live?: boolean; className?: string }) =>
  <span aria-hidden className={cn("inline-block size-2 shrink-0 rounded-full", DOT[tone], live && "desk-pulse", className)} />


/** "Just changed" mark beside a row that moved since the previous read (lib/changes.ts). Text, not only colour. */
export function ChangeMark({ c, className }: { c?: Change; className?: string }) {
  if (!c) return null
  return <span className={cn("inline-flex shrink-0 items-center gap-1 rounded-full bg-info/10 px-1.5 py-px text-[11px] font-medium text-info", className)}>
    <span aria-hidden className="size-1 rounded-full bg-current" />{c.from ? `was ${humanize(c.from)}` : "new"}</span>
}
