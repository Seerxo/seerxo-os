import { Component, type ErrorInfo, type ReactNode } from "react"
import { Button } from "@/components/ui/button"

/** Last line of defence: API responses are validated before they render (lib/validate.ts), so this only
 *  catches what that missed. A render error shows a recoverable message instead of a blank app.
 *  `resetKey` clears it when the user navigates or opens something else; `onRetry` lets the owner refetch its data first. */
export class ErrorBoundary extends Component<{ children: ReactNode; label: string; resetKey?: string; onRetry?: () => void }, { error: Error | null; key?: string }> {
  state = { error: null as Error | null, key: this.props.resetKey }
  static getDerivedStateFromError(error: Error) { return { error } }
  static getDerivedStateFromProps(props: { resetKey?: string }, state: { error: Error | null; key?: string }) {
    return props.resetKey === state.key ? null : { error: null, key: props.resetKey }
  }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error(`Founder Desk: ${this.props.label} failed to render`, error, info.componentStack) }
  render() {
    if (!this.state.error) return this.props.children
    return (
      <div role="alert" className="grid justify-items-start gap-2 py-6 text-sm">
        <b className="font-medium">{this.props.label} could not be shown</b>
        <span className="text-muted-foreground [overflow-wrap:anywhere]">{this.state.error.message}. Nothing was changed; no value is guessed.</span>
        <Button size="sm" variant="outline" onClick={() => { this.props.onRetry?.(); this.setState({ error: null }) }}>Try again</Button>
      </div>
    )
  }
}
