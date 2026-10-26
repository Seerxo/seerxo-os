// What an agent produced for a task: the list/ranking/analysis it returned (`artifacts.actions`, persisted by the worker).
// artifacts is unvalidated JSON from the API, so only non-empty strings are shown; anything else is ignored, never rendered.
export function deliverableItems(artifacts: unknown): string[] {
  if (!artifacts || typeof artifacts !== "object" || Array.isArray(artifacts)) return []
  const items = (artifacts as Record<string, unknown>).actions
  return Array.isArray(items) ? items.filter((x): x is string => typeof x === "string" && x.trim().length > 0).slice(0, 50) : []
}
