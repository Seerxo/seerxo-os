// Pure needs-you helpers, importable by `npm test` without React.
import type { Need } from "./api"

/** The event a founder may retry from this need: only an unconfirmed critical or recovery delivery (desk.py needs_you,
 *  kind SECURITY, evidence "event <id> · <reason>"). A routing failure or anything else is not retried from the Desk. */
export function retryEventId(n: Need): number | null {
  if (n.kind !== "SECURITY" || n.target.page !== "activity") return null
  const m = /^event (\d+) · /.exec(n.evidence)
  return m ? Number(m[1]) : null
}
