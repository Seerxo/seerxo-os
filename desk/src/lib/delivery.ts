// One code mission's own pipeline, from /desk/state improvements (app/desk.py improvements(), the Stage C lifecycle in
// app/stage_c.py). Unlike the Overview's totals, every step here belongs to the same task. Only in-flight missions are
// reported (MEASURED and ABANDONED are dropped by the API), and a stage the task has not reached is never drawn as done.
import type { Improvement, Tone } from "./api.ts"

/** app/stage_c.py STAGES, minus ABANDONED (terminal, never reported). Failed tests, review or CI go back to IMPLEMENTING. */
export const DELIVERY_STAGES = [
  ["DETECTED", "Detected"], ["PLANNED", "Planned"], ["IMPLEMENTING", "Implementing"], ["TESTING", "Testing"],
  ["INDEPENDENT_REVIEW", "Independent review"], ["PR_OPEN", "PR open"], ["CI", "CI"], ["READY_TO_MERGE", "Ready to merge"],
  ["MERGED", "Merged"], ["VERIFYING", "Verifying"], ["MEASURED", "Measured"],
] as const

export interface DeliveryView {
  index: number | null        // position in DELIVERY_STAGES; null = a stage this Desk does not know
  label: string
  tone: Tone
  evidence: string[]          // short recorded facts: PR, CI, failed check, repairs, review verdict
}

export function deliveryView(i: Improvement): DeliveryView {
  const index = DELIVERY_STAGES.findIndex(([k]) => k === i.stage)
  const ci = i.ci_state
  const evidence = [
    i.pr_number != null ? `PR #${i.pr_number}` : null,
    ci ? `CI ${ci.toLowerCase()}` : null,
    i.failed_check ? `failed: ${i.failed_check}` : null,
    i.repairs ? `${i.repairs} ${i.repairs === "1" ? "repair" : "repairs"}` : null,
    i.review_verdict ? `review ${i.review_verdict.toLowerCase()}` : null,
  ].filter((x): x is string => !!x)
  const failing = !!i.failed_check || /fail/i.test(ci ?? "")
  const tone: Tone = index < 0 ? "warning" : failing ? "danger" : i.stage === "READY_TO_MERGE" ? "warning" : i.stage === "MERGED" || i.stage === "VERIFYING" ? "success" : "info"
  return { index: index < 0 ? null : index, label: index < 0 ? `Unknown stage ${i.stage}` : DELIVERY_STAGES[index][1], tone, evidence }
}
