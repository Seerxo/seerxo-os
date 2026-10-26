// The founder's controls, one implementation wherever they appear (Approvals, the Command Center, Automation), so a
// decision always asks the same confirmation. Nothing is shown as done before the API answers; the refetch after it
// (state.tsx kick) is the truth.
import { useState } from "react"
import { AlertTriangle, Check, Loader2, Pause, Play, RotateCw, ShieldCheck, ShieldOff, X } from "lucide-react"
import type { Approval } from "@/lib/api"
import { cn } from "@/lib/utils"
import { useData, useDesk } from "@/state"
import { Facts, Status } from "@/components/desk"
import { Confirm } from "@/components/detail"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger,
} from "@/components/ui/alert-dialog"

export const HIGH_RISK = ["HIGH", "CRITICAL"]

/** High-risk approval: materially different — warning frame and the key must be typed. */
function HighRiskApprove({ a, onConfirm, trigger }: { a: Approval; onConfirm: () => void; trigger: React.ReactNode }) {
  const [typed, setTyped] = useState("")
  return (
    <AlertDialog onOpenChange={() => setTyped("")}>
      <AlertDialogTrigger asChild>{trigger}</AlertDialogTrigger>
      <AlertDialogContent className="border-warning/40">
        <AlertDialogHeader>
          <AlertDialogTitle className="flex items-center gap-2"><AlertTriangle className="size-4 text-warning" />High-risk approval</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="grid gap-3 text-sm">
              <p className="text-foreground">{a.action}</p>
              <Facts items={[["Risk", <Status key="risk" value={a.risk} />], ["If approved", a.expected_effect || "Not recorded"], ["Rollback", a.rollback || "Not recorded"]]} />
              <label className="grid gap-1.5 text-muted-foreground"><span>Type <span className="font-mono text-foreground">{a.key}</span> to approve</span>
                <Input value={typed} onChange={e => setTyped(e.target.value)} autoComplete="off" spellCheck={false} aria-label={`Type ${a.key} to approve`} />
              </label>
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction className="min-h-10" disabled={typed.trim() !== a.key} onClick={onConfirm}>Approve {a.key}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** Reject and Approve for one approval record. `compact` is the one-line row form used on the Command Center. */
export function ApprovalDecision({ a, compact }: { a: Approval; compact?: boolean }) {
  const { act } = useDesk()
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null)
  const decide = async (granted: boolean) => {
    setBusy(granted ? "approve" : "reject")
    await act(`/approvals/${encodeURIComponent(a.key)}`, { granted, decided_by: "founder", notes: "decided from Founder Desk" }, `${a.key} ${granted ? "approved" : "rejected"}`)
    setBusy(null)
  }
  const size = compact ? "h-8 px-2.5 text-xs" : "h-10 px-4"
  const spin = <Loader2 className="animate-spin" />
  const approve = <Button className={size} disabled={!!busy} aria-label={compact ? `Approve ${a.key}` : undefined}>
    {busy === "approve" ? spin : <Check />}Approve</Button>
  return (
    <span className={cn("flex gap-2", !compact && "max-sm:[&>*]:flex-1")}>
      <Confirm title={`Reject ${a.key}?`} description={<p>{a.action}. Nothing changes; the requester sees it was rejected.</p>} action="Reject" destructive onConfirm={() => void decide(false)}>
        <Button variant="outline" className={size} disabled={!!busy} aria-label={compact ? `Reject ${a.key}` : undefined}>
          {busy === "reject" ? spin : <X />}Reject</Button>
      </Confirm>
      {HIGH_RISK.includes(a.risk) ? <HighRiskApprove a={a} onConfirm={() => void decide(true)} trigger={approve} />
        : <Confirm title={`Approve ${a.key}?`} action="Approve" onConfirm={() => void decide(true)}
            description={<Facts items={[["Action", a.action], ["Risk", a.risk], ["If approved", a.expected_effect || "Not recorded"], ["Rollback", a.rollback || "Not recorded"]]} />}>
            {approve}
          </Confirm>}
    </span>
  )
}

/** The agent kill switch (§114): pausing stops autonomous execution only, never production services. Pausing works for
 *  any authorized caller; resuming needs the verified founder session (the API refuses others). */
/** `iconOnPhone` keeps the header on one row on a phone; the accessible name stays. */
export function AgentsSwitch({ className, iconOnPhone }: { className?: string; iconOnPhone?: boolean }) {
  const d = useData()
  const { act } = useDesk()
  const paused = d.control.agents_paused
  const [busy, setBusy] = useState(false)
  const flip = async () => {
    setBusy(true)
    await act("/control/agents", { paused: !paused, actor: "founder", reason: `${paused ? "resume" : "pause"} from Founder Desk` }, `Agents ${paused ? "resumed" : "paused"}`)
    setBusy(false)
  }
  const label = paused ? "Resume agents" : "Pause agents"
  return (
    <Confirm title={`${paused ? "Resume" : "Pause"} autonomous agents?`} action={paused ? "Resume agents" : "Pause agents"} destructive={!paused}
      description={<p>{paused ? "Agents may pick up READY work again. Resuming needs your verified founder sign-in." : "Autonomous execution stops; queued work waits."} Production services keep running either way.</p>}
      onConfirm={() => void flip()}>
      <Button variant={paused ? "default" : "outline"} disabled={busy} aria-label={iconOnPhone ? label : undefined}
        className={cn("h-10", !paused && "text-destructive hover:text-destructive", iconOnPhone && "max-sm:w-10 max-sm:px-0", className)}>
        {busy ? <Loader2 className="animate-spin" /> : paused ? <Play /> : <Pause />}<span className={cn(iconOnPhone && "max-sm:sr-only")}>{label}</span></Button>
    </Confirm>
  )
}

/** Retry one unconfirmed alert delivery (POST /notifications/{id}/retry, verified founder only). The backend stopped
 *  automatic retries to avoid duplicates, so the dialog says to check Telegram first. */
export function RetryDelivery({ eventId, compact }: { eventId: number; compact?: boolean }) {
  const { act } = useDesk()
  const [busy, setBusy] = useState(false)
  return (
    <Confirm title={`Retry delivery of event ${eventId}?`} action="Retry delivery"
      description={<p>Telegram may already have received this alert; a retry can send it twice. Check Telegram first, then retry only if it did not arrive.</p>}
      onConfirm={() => { setBusy(true); void act(`/notifications/${eventId}/retry`, {}, `Delivery of event ${eventId} retried`).finally(() => setBusy(false)) }}>
      <Button variant="outline" disabled={busy} className={compact ? "h-8 px-2.5 text-xs" : "h-10"}>
        {busy ? <Loader2 className="animate-spin" /> : <RotateCw />}Retry delivery</Button>
    </Confirm>
  )
}

/** Approve a candidate learning (POST /learnings/{id}/approve, verified founder only). Agents then load it as an
 *  instruction, so the dialog shows the exact lesson. `onDone` lets the caller re-read memory. */
export function ApproveLearning({ id, title, lesson, onDone }: { id: number; title: string; lesson: string; onDone: () => void }) {
  const { act } = useDesk()
  const [busy, setBusy] = useState(false)
  return (
    <Confirm title="Approve this learning?" action="Approve learning"
      description={<><p className="font-medium text-foreground">{title}</p><p>{lesson}</p><p>Agents load approved learnings as instructions.</p></>}
      onConfirm={() => { setBusy(true); void act(`/learnings/${id}/approve`, { actor: "founder", reason: "approved from Founder Desk" }, "Learning approved")
        .then(ok => { if (ok) onDone() }).finally(() => setBusy(false)) }}>
      <Button size="sm" disabled={busy} className="w-fit">{busy ? <Loader2 className="animate-spin" /> : <Check />}Approve</Button>
    </Confirm>
  )
}

/** Authorize one Stage C attempt for one task (POST /stage-c/authorization): high risk, so the task key is typed. The
 *  server binds it to the deployed SHA, caps it at 60 minutes and refuses if API and worker disagree. It enables
 *  nothing by itself: the global flag, host acceptance and readiness still apply. The answer is shown as returned. */
export function AuthorizeStageC({ taskKey }: { taskKey: string }) {
  const { act } = useDesk()
  const [typed, setTyped] = useState("")
  const [minutes, setMinutes] = useState(30)
  const [busy, setBusy] = useState(false)
  return (
    <AlertDialog onOpenChange={() => setTyped("")}>
      <AlertDialogTrigger asChild><Button variant="outline" disabled={busy} className="h-9">{busy ? <Loader2 className="animate-spin" /> : <ShieldCheck />}Authorize Stage C</Button></AlertDialogTrigger>
      <AlertDialogContent className="border-warning/40">
        <AlertDialogHeader>
          <AlertDialogTitle className="flex items-center gap-2"><AlertTriangle className="size-4 text-warning" />Authorize one autonomous coding attempt</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="grid gap-3 text-sm">
              <p>One attempt on <span className="font-mono text-foreground">{taskKey}</span>, bound to the deployed commit, for at most {minutes} minutes. It enables nothing on its own: the Stage C flag, host acceptance and readiness still decide.</p>
              <label className="grid gap-1.5 text-muted-foreground"><span>Minutes (1–60)</span>
                <Input type="number" min={1} max={60} value={minutes} onChange={e => setMinutes(Math.max(1, Math.min(60, Number(e.target.value) || 30)))} aria-label="Minutes" /></label>
              <label className="grid gap-1.5 text-muted-foreground"><span>Type <span className="font-mono text-foreground">{taskKey}</span> to authorize</span>
                <Input value={typed} onChange={e => setTyped(e.target.value)} autoComplete="off" spellCheck={false} aria-label={`Type ${taskKey} to authorize`} /></label>
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction disabled={typed.trim() !== taskKey}
            onClick={() => { setBusy(true); void act("/stage-c/authorization", { task_key: taskKey, minutes }, `Stage C authorized for ${taskKey} (${minutes} min)`).finally(() => setBusy(false)) }}>
            Authorize {taskKey}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** Withdraw Stage C authority (pilot and standing). Withdrawing is always allowed to the founder (and host operator). */
export function RevokeStageC() {
  const { act } = useDesk()
  const [busy, setBusy] = useState(false)
  const revoke = async () => {
    setBusy(true)
    const a = await act("/stage-c/authorization/revoke", { reason: "revoked from Founder Desk" }, "One-task Stage C authorization revoked")
    const b = await act("/stage-c/standing/revoke", { reason: "revoked from Founder Desk" }, "Standing Stage C authorization revoked")
    setBusy(false)
    return a && b
  }
  return (
    <Confirm title="Revoke all Stage C authority?" action="Revoke" destructive
      description={<p>Withdraws the one-task and the standing Stage C authorizations, so no new autonomous coding attempt can start under them.</p>}
      onConfirm={() => void revoke()}>
      <Button variant="outline" disabled={busy} className="h-9 text-destructive hover:text-destructive">{busy ? <Loader2 className="animate-spin" /> : <ShieldOff />}Revoke Stage C authority</Button>
    </Confirm>
  )
}

/** "Mark READY": hand a stopped or not-yet-released task to the worker (POST /tasks/{key}/status, verified founder
 *  only; lib/ready.ts decides when it is offered). The dialog names the task, its current status and what READY does;
 *  the API's refusal (an open run, or for code writes a missing Stage C condition) is shown as returned, never as done. */
export function MarkReady({ taskKey, status, codeWrite, note }: { taskKey: string; status: string; codeWrite: boolean; note?: string | null }) {
  const { act } = useDesk()
  const [busy, setBusy] = useState(false)
  return (
    <Confirm title={`Mark ${taskKey} READY?`} action="Mark READY"
      description={<>
        <Facts items={[["Task", <span key="k" className="font-mono">{taskKey}</span>], ["Now", <Status key="s" value={status} />], ["After", "READY: the worker may pick it up on its next pass"]]} />
        {note && <p className="text-warning">{note}</p>}
        {codeWrite && <p>This task writes code. The API makes it READY only if agents are running, autonomous code writing is on, the host acceptance and preflight are current for the deployed commit, an unexpired Stage C authorization covers it, and no published pull request owns it. Otherwise it refuses and says why.</p>}
      </>}
      onConfirm={() => { setBusy(true); void act(`/tasks/${encodeURIComponent(taskKey)}/status`, { status: "READY", actor: "founder", reason: "Marked READY from Founder Desk" }, `${taskKey} is READY`).finally(() => setBusy(false)) }}>
      <Button size="sm" className="h-9" disabled={busy}>{busy ? <Loader2 className="animate-spin" /> : <Play />}Mark READY</Button>
    </Confirm>
  )
}
