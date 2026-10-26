// One column list drives both renderings of a dense table: the desktop <table> and the compact
// mobile cards. A column the cards omit must say so (`mobile: "hide"`); nothing is dropped silently,
// so the two views cannot drift apart. Pure, so `npm test` pins it.
export interface ColSpec { head: string; mobile?: "hide" }

export function layout(cols: ColSpec[]): { table: string[]; card: string[] } {
  return { table: cols.map(c => c.head), card: cols.filter(c => c.mobile !== "hide").map(c => c.head) }
}

/** A column whose cells are right-aligned must have a right-aligned header (numbers under their label). */
export const rightAligned = (c: { className?: string }, isAside = false): boolean =>
  isAside || /(^|\s)text-right(\s|$)/.test(c.className ?? "")

/** Desktop column order. `aside` (the state shown top-right on a phone) is the last column unless
 *  `asideFirst` puts it right after the title, e.g. Agent, Status, ... Never duplicated, never dropped. */
export function order<T>(cols: T[], aside: T | undefined, asideFirst = false): T[] {
  return !aside ? [...cols] : asideFirst ? [aside, ...cols] : [...cols, aside]
}
