// A needs-you item (desk.py needs_you) opens where it is resolved; opening never approves or acknowledges anything.
import type { Need } from "./api"
import { useDesk, type Page } from "@/state"

/** The one action a need asks for, named for what it does. */
export function needAction(n: Need): string {
  return ({ approvals: "Review", incidents: "Open incident", tasks: "Open task", health: "Check", automation: "Check" } as Record<string, string>)[n.target.page]
    ?? "Open"
}

export function useOpenNeed() {
  const { go, open } = useDesk()
  return (n: Need) => {
    if (n.target.page === "tasks" && n.target.key) { open({ kind: "task", key: n.target.key }); return }
    go((n.target.page === "overview" ? "health" : n.target.page) as Page)
  }
}
