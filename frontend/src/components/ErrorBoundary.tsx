import { Component, type ErrorInfo, type ReactNode } from "react";
import { Clapperboard, RotateCcw } from "lucide-react";

interface Props {
  children: ReactNode;
  /** Compact rendering for panel-level boundaries (drawer, tray). */
  compact?: boolean;
}
interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("StoryBored UI error:", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    if (this.props.compact) {
      return (
        <div className="flex items-center gap-2 rounded-md border border-line p-3 text-sm text-fog">
          Something went wrong here.
          <button
            className="inline-flex items-center gap-1 text-amber-450 hover:text-amber-350"
            onClick={() => this.setState({ error: null })}
          >
            <RotateCcw size={13} /> Retry
          </button>
        </div>
      );
    }
    return (
      <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 px-6 text-center">
        <div className="rounded-2xl border border-line p-5">
          <Clapperboard size={32} className="text-amber-450" />
        </div>
        <div>
          <h2 className="text-lg font-semibold">Well, that take didn't work.</h2>
          <p className="mt-1 max-w-sm text-sm text-fog">
            Something unexpected happened in this part of the app. Your work is
            saved on the server.
          </p>
        </div>
        <button
          className="inline-flex h-9 items-center gap-2 rounded-md bg-amber-450 px-4 text-sm font-semibold text-ink-950 hover:bg-amber-350"
          onClick={() => this.setState({ error: null })}
        >
          <RotateCcw size={14} /> Try again
        </button>
      </div>
    );
  }
}
