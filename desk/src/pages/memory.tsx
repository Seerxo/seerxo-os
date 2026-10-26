import { useEffect, useState } from "react"
import { RefreshCw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { toast } from "sonner"
import { api, type Memory } from "@/lib/api"
import { when } from "@/lib/format"
import { checkMemory } from "@/lib/validate"
import { Calm, Expandable, Facts, FigureStrip, PageHeader, Status } from "@/components/desk"
import { Chips } from "@/components/detail"
import { ApproveLearning } from "@/components/controls"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"

/** PERMANENT_FACTS.md as readable facts: "## FACT-xx · title", a yaml block of key: value
 *  lines (indented lines continue the previous value), then prose. Unparsed text stays text. */
interface Fact { title: string; fields: [string, string][]; prose: string }
function parseFacts(md: string): Fact[] {
  return md.split(/\n(?=## )/).filter(p => p.startsWith("## ")).map(part => {
    const [head, ...rest] = part.split("\n")
    const body = rest.join("\n")
    const yaml = /```ya?ml\n([\s\S]*?)```/.exec(body)?.[1] ?? ""
    const fields: [string, string][] = []
    for (const line of yaml.split("\n")) {
      const kv = /^([a-z_]+):\s*(.*)$/i.exec(line)
      if (kv) fields.push([kv[1].replaceAll("_", " "), kv[2]])
      else if (line.trim() && fields.length) fields[fields.length - 1][1] += " " + line.trim()
    }
    const prose = body.replace(/```[\s\S]*?```/g, "").replace(/^---$/gm, "")
      .replace(/\*\*(.+?)\*\*/g, "$1").replace(/`([^`]+)`/g, "$1").trim()  // inline markdown marks read as noise
    return { title: head.replace(/^## /, ""), fields, prose }
  })
}

export function MemoryPage() {
  const [memory, setMemory] = useState<Memory | null>(null)
  const [q, setQ] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const [tab, setTab] = useState<string | null>(null)
  // Counts per tab. The first tab with anything in it opens first; an empty Decisions tab is not the page.
  const tabs = memory ? ([["decisions", "Decisions", memory.decisions.length], ["learnings", "Learnings", memory.learnings.length],
    ["permanent", "Permanent", memory.permanent ? parseFacts(memory.permanent).length : 0], ["notes", "Notebooks", memory.notes.length]] as const) : []
  useEffect(() => {
    api<unknown>("/desk/memory").then(checkMemory).then(setMemory)
      .catch(e => { setError((e as Error).message); toast.error((e as Error).message) })
  }, [attempt])
  const hit = (o: unknown) => !q || JSON.stringify(o).toLowerCase().includes(q.toLowerCase())
  const none = (msg: string) => <Calm>{q ? "Nothing matches the search." : msg}</Calm>
  const grid = "panel grid grid-cols-1 [&_p]:max-w-3xl divide-y divide-border/70 px-4 sm:px-5 [overflow-wrap:anywhere]"
  const first = tabs.find(([, , n]) => n > 0)?.[0] ?? "decisions"
  return <>
    <PageHeader title="Memory" description="Decisions, learnings, permanent facts and agent notebooks." />
    {error ? <div role="alert" className="panel grid justify-items-start gap-2 px-4 py-5">
        <span className="flex items-center gap-2 text-sm font-medium"><Status value="failed" tone="danger" label="Memory could not be loaded" /></span>
        <p className="text-[13px] text-muted-foreground">{error}. Nothing is shown rather than an empty list.</p>
        <Button size="sm" variant="outline" onClick={() => { setError(null); setAttempt(n => n + 1) }}><RefreshCw />Try again</Button></div>
    : !memory ? <div className="grid gap-4" aria-busy="true" aria-label="Loading memory"><Skeleton className="h-[92px] rounded-xl" /><Skeleton className="h-10 w-80" /><Skeleton className="h-40 rounded-xl" /></div> : (<>
      <FigureStrip label="Memory at a glance" items={tabs.map(([v, label, n]) => ({ label, value: String(n), onClick: () => setTab(v),
        sub: ({ decisions: "settled by the founder", learnings: "lessons from runs", permanent: "facts never re-asked", notes: "agent working notes" } as Record<string, string>)[v] }))} />
      <Tabs value={tab ?? first} onValueChange={setTab} className="min-w-0 gap-4">
        <div className="flex w-full min-w-0 flex-wrap items-center justify-between gap-3">
          <TabsList className="h-auto min-w-0 max-w-full justify-start overflow-x-auto [scrollbar-width:none]">
            {tabs.map(([v, label, n]) => <TabsTrigger key={v} value={v} className="shrink-0">{label}<span className="num ml-1.5 text-xs text-muted-foreground">{n}</span></TabsTrigger>)}
          </TabsList>
          <Input type="search" value={q} onChange={e => setQ(e.target.value)} placeholder="Search memory" aria-label="Search memory" className="w-64 max-sm:w-full" />
        </div>
        <TabsContent value="decisions" tabIndex={-1}>
          {memory.decisions.filter(hit).length ? <div className={grid}>{memory.decisions.filter(hit).map(x => {
            const body = <><p className="text-sm">{x.decision}</p>
              {x.reason && <p className="text-sm text-muted-foreground">{x.reason}</p>}
              <p className="text-xs text-muted-foreground">{x.approved_by ? `Approved by ${x.approved_by} · ` : ""}{when(x.created_at)}</p></>
            const head = <><span className="shrink-0 font-mono text-xs text-muted-foreground">{x.key}</span>
              <span className="min-w-0 flex-1 truncate font-medium">{x.title}</span><Status value={x.status} className="max-sm:hidden" /></>
            return q ? <article key={x.key} className="grid gap-2 py-4"><div className="flex min-w-0 items-center gap-3">{head}</div>{body}</article>
              : <Expandable key={x.key} summary={head}>{body}</Expandable>
          })}</div> : none("No decisions recorded.")}
        </TabsContent>
        <TabsContent value="learnings" tabIndex={-1}>
          {memory.learnings.filter(hit).length ? <div className={grid}>{memory.learnings.filter(hit).map(l => {
            // A candidate becomes trusted memory only through the founder (§53A); approving re-reads memory.
            const body = <><p className="text-sm text-muted-foreground">{l.lesson}</p><div className="flex flex-wrap items-center gap-2"><Chips items={l.tags} /></div>
              {l.status === "CANDIDATE" && <ApproveLearning id={l.id} title={l.title} lesson={l.lesson} onDone={() => setAttempt(a => a + 1)} />}</>
            const head = <><span className="min-w-0 flex-1 truncate font-medium">{l.title}</span>{l.status === "CANDIDATE" && <Status value="CANDIDATE" label="Candidate" tone="warning" />}<span className="shrink-0 text-xs text-muted-foreground max-sm:hidden">{when(l.created_at)}</span></>
            return q ? <article key={l.id} className="grid gap-2 py-4"><div className="flex min-w-0 items-center gap-3">{head}</div>{body}</article>
              : <Expandable key={l.id} summary={head}>{body}</Expandable>
          })}</div> : none("No learnings recorded.")}
        </TabsContent>
        <TabsContent value="permanent" tabIndex={-1}>
          {!memory.permanent ? <Calm>PERMANENT_FACTS.md is not available on this server.</Calm> : (() => {
            const facts = parseFacts(memory.permanent).filter(hit)
            return facts.length ? <div className={grid}>{facts.map(f => {
              const body = <>{f.fields.length > 0 && <Facts items={f.fields} />}
                {f.prose && <p className="text-sm whitespace-pre-line text-muted-foreground">{f.prose}</p>}</>
              const head = <span className="min-w-0 flex-1 truncate font-medium">{f.title}</span>
              return q ? <article key={f.title} className="grid gap-2 py-4"><h3 className="font-semibold">{f.title}</h3>{body}</article>
                : <Expandable key={f.title} summary={head}>{body}</Expandable>
            })}</div> : none("No permanent facts recorded.")
          })()}
        </TabsContent>
        <TabsContent value="notes" tabIndex={-1}>
          {memory.notes.filter(hit).length ? <div className={grid}>{memory.notes.filter(hit).map(n => (
            <article key={n.note} className="grid gap-2 py-4"><p className="text-sm">{n.note}</p>
              <p className="text-xs text-muted-foreground">Seen ×{n.refs} · last {when(n.last_seen_at)} · {n.promoted ? "promoted" : "not promoted"}</p><Chips items={n.tags} /></article>
          ))}</div> : none("No notebook entries.")}
        </TabsContent>
      </Tabs>
    </>)}
  </>
}
