// Dense tables read badly on a phone and tablet: the page stays put and all important fields remain visible.
// Below `lg` the same rows are shown as compact cards. One column list drives both (lib/rows.ts).
import type { ReactNode } from "react"
import type { ColSpec } from "@/lib/rows"
import { layout, order, rightAligned } from "@/lib/rows"
import { cn } from "@/lib/utils"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"

export interface Col<T> extends ColSpec { cell: (row: T) => ReactNode; className?: string }

interface Props<T> {
  label: string                       // accessible name for the list/table
  rows: T[]
  rowKey: (row: T) => string
  titleHead?: string                  // first column header; omit every header with `headless`
  title: (row: T) => ReactNode
  desc?: (row: T) => ReactNode        // secondary line under the title (table: same cell; card: under the heading)
  cols?: Col<T>[]                     // extra columns -> card fields (label: value)
  aside?: Col<T>                      // right-aligned state: last table column, top-right of the card
  asideFirst?: boolean                // table: put `aside` right after the title instead of last
  headless?: boolean
  descColumn?: boolean                // table: description in its own column (key / description / state tables)
  onRowClick?: (row: T) => void
}

export function DataRows<T>({ label, rows, rowKey, titleHead = "", title, desc, cols = [], aside, asideFirst, headless, descColumn, onRowClick }: Props<T>) {
  const all: Col<T>[] = order(cols, aside, asideFirst)
  const { card } = layout(all)
  const act = onRowClick
  return <>
    <div className="max-lg:hidden" role="region" aria-label={label}>
      <Table>
        {!headless && <TableHeader><TableRow>
          <TableHead>{titleHead}</TableHead>
          {descColumn && <TableHead />}
          {all.map(c => <TableHead key={c.head} className={rightAligned(c, c === aside && !asideFirst) ? "text-right" : undefined}>{c.head}</TableHead>)}
        </TableRow></TableHeader>}
        <TableBody>
          {rows.map(r => (
            <TableRow key={rowKey(r)} className={act ? "cursor-pointer focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring" : undefined} onClick={act && (() => act(r))}
              tabIndex={act ? 0 : undefined} onKeyDown={act && (e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); act(r) } })}>
              {descColumn
                ? <><TableCell className="font-medium whitespace-normal [overflow-wrap:anywhere]">{title(r)}</TableCell>
                    <TableCell className="text-sm whitespace-normal text-muted-foreground">{desc?.(r)}</TableCell></>
                : <TableCell className="whitespace-normal [overflow-wrap:anywhere]"><div className="grid gap-0.5"><span className="font-medium">{title(r)}</span>
                    {desc && <span className="text-xs text-muted-foreground">{desc(r)}</span>}</div></TableCell>}
              {all.map(c => <TableCell key={c.head} className={cn("whitespace-normal [overflow-wrap:anywhere]", c === aside && !asideFirst && "text-right", c.className)}>{c.cell(r)}</TableCell>)}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
    <ul aria-label={label} className="grid grid-cols-1 divide-y-0 gap-2 lg:hidden">
      {rows.map(r => (
        <li key={rowKey(r)} className="surface-panel grid gap-2.5 p-3.5 sm:p-4">
          <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-0.5">
            {act
              ? <button type="button" onClick={() => act(r)} className="min-w-0 text-left font-medium [overflow-wrap:anywhere] underline-offset-2 hover:underline focus-visible:underline focus-visible:ring-2 focus-visible:ring-ring/60 focus-visible:outline-none rounded-sm">{title(r)}</button>
              : <span className="min-w-0 font-medium [overflow-wrap:anywhere]">{title(r)}</span>}
            {aside && <span className="ml-auto max-w-full text-right text-sm [overflow-wrap:anywhere]">{aside.cell(r)}</span>}
          </div>
          {desc && <p className="text-xs [overflow-wrap:anywhere] text-muted-foreground">{desc(r)}</p>}
          <dl className="grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-4 gap-y-0.5 text-sm">
            {cols.filter(c => card.includes(c.head)).map(c => <div key={c.head} className="col-span-2 grid grid-cols-subgrid items-baseline">
              <dt className="text-xs text-muted-foreground">{c.head}</dt><dd className="num min-w-0 text-right [overflow-wrap:anywhere]">{c.cell(r)}</dd></div>)}
          </dl>
        </li>
      ))}
    </ul>
  </>
}
