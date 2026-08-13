import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

export function EmptyState({
  icon: Icon,
  title,
  body,
  action,
}: {
  icon: LucideIcon;
  title: string;
  body?: string;
  action?: ReactNode;
}) {
  return (
    <div className="sb-fade-in flex flex-col items-center justify-center gap-4 rounded-xl border border-dashed border-line px-8 py-16 text-center">
      <div className="relative">
        <div className="sb-slate-stripes absolute -inset-3 rounded-2xl opacity-25" />
        <div className="relative rounded-2xl border border-line bg-ink-900 p-4">
          <Icon size={26} className="text-amber-450" strokeWidth={1.75} />
        </div>
      </div>
      <div className="max-w-sm">
        <h3 className="text-base font-semibold text-paper">{title}</h3>
        {body && <p className="mt-1.5 text-sm leading-relaxed text-fog">{body}</p>}
      </div>
      {action}
    </div>
  );
}

export function ErrorState({
  title = "Can't reach the StoryBored server",
  body = "Check that the server is running, then try again.",
  onRetry,
}: {
  title?: string;
  body?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="sb-fade-in flex flex-col items-center justify-center gap-3 rounded-xl border border-line bg-ink-900/60 px-8 py-14 text-center">
      <span className="inline-block h-2.5 w-2.5 rounded-full bg-status-failed" />
      <h3 className="text-base font-semibold text-paper">{title}</h3>
      <p className="max-w-sm text-sm text-fog">{body}</p>
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-1 h-8 rounded-md border border-line-bright px-3 text-sm text-mist hover:text-paper"
        >
          Try again
        </button>
      )}
    </div>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`sb-skeleton rounded-md ${className}`} />;
}
