// When the Desk re-reads /desk/state. Pure so it is testable; state.tsx owns the timers.

/** Run `fn`, never two at once; a call made while one runs is not dropped but runs once right after it.
 *  (Dropping it lost the refresh after an action whenever a background poll was in flight.) */
export function coalesce(fn: () => Promise<void>) {
  let running = false, again = false
  return async function run(): Promise<void> {
    if (running) { again = true; return }
    running = true
    try {
      do { again = false; await fn() } while (again)
    } finally { running = false }
  }
}

export const SLOW_MS = 30_000   // nothing happening: the read model is cheap but not free
export const FAST_MS = 3_000    // just after a founder action: the worker reacts within seconds
export const BURST_MS = 60_000  // how long an action keeps the fast pace

/** Is a background re-read due? Fast for a minute after the founder acted, slow otherwise. */
export const due = (now: number, last: number, fastUntil: number) => now - last >= (now < fastUntil ? FAST_MS : SLOW_MS)
