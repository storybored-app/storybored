import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ListVideo, XCircle } from "lucide-react";
import { apiGet, apiPost } from "../lib/api";
import type { Job } from "../lib/types";
import { jobTypeLabel } from "../lib/format";
import { ProgressBar } from "./ui";
import { useToast } from "../lib/toast";

const ACTIVE = new Set(["queued", "running"]);

function timeAgoish(job: Job): string {
  if (job.status === "running") return "running";
  if (job.status === "queued") return "waiting";
  return job.status;
}

export function JobTray() {
  const [open, setOpen] = useState(false);
  const qc = useQueryClient();
  const { toast } = useToast();

  const { data: jobs, isError } = useQuery<Job[]>({
    queryKey: ["jobs"],
    queryFn: () => apiGet<Job[]>("/api/jobs"),
    refetchInterval: 60_000, // SSE keeps this fresh; slow poll is a safety net
    retry: 1,
  });

  const cancel = useMutation({
    mutationFn: (id: number) => apiPost(`/api/jobs/${id}/cancel`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
    onError: (e: Error) => toast(e.message, "error"),
  });

  const { active, recent } = useMemo(() => {
    const list = jobs ?? [];
    const active = list
      .filter((j) => ACTIVE.has(j.status))
      .sort((a, b) => (a.status === "running" ? -1 : 1) - (b.status === "running" ? -1 : 1));
    const recent = list
      .filter((j) => !ACTIVE.has(j.status))
      .sort((a, b) => (b.finished_at ?? "").localeCompare(a.finished_at ?? ""))
      .slice(0, 4);
    return { active, recent };
  }, [jobs]);

  if (isError || !jobs) return null;
  if (active.length === 0 && !open) return null;

  return (
    <div className="fixed bottom-5 right-5 z-[60] flex flex-col items-end gap-2">
      {open && (
        <div className="sb-fade-in w-80 overflow-hidden rounded-xl border border-line-bright bg-ink-850/95 shadow-2xl backdrop-blur">
          <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
            <span className="text-xs font-semibold uppercase tracking-wider text-fog">
              Activity
            </span>
            <button
              onClick={() => setOpen(false)}
              className="rounded p-0.5 text-fog hover:text-paper"
              aria-label="Collapse"
            >
              <ChevronDown size={15} />
            </button>
          </div>
          <div className="max-h-80 overflow-y-auto p-2">
            {active.length === 0 && recent.length === 0 && (
              <p className="px-2 py-4 text-center text-sm text-fog">
                Nothing in the queue.
              </p>
            )}
            {active.map((j) => (
              <div key={j.id} className="rounded-lg p-2.5 hover:bg-ink-800/70">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium text-paper">
                    {jobTypeLabel(j.type)}
                  </span>
                  <div className="flex items-center gap-1.5">
                    <span className="text-[11px] text-fog">{timeAgoish(j)}</span>
                    <button
                      onClick={() => cancel.mutate(j.id)}
                      className="rounded p-0.5 text-fog hover:text-status-failed"
                      title="Cancel"
                    >
                      <XCircle size={14} />
                    </button>
                  </div>
                </div>
                {j.detail && (
                  <p className="mt-0.5 truncate text-xs text-fog" title={j.detail}>
                    {j.detail}
                  </p>
                )}
                <div className="mt-1.5">
                  <ProgressBar value={j.status === "queued" ? 0 : j.progress} />
                </div>
              </div>
            ))}
            {recent.length > 0 && (
              <p className="px-2.5 pb-1 pt-3 text-[10px] font-semibold uppercase tracking-wider text-fog/70">
                Recent
              </p>
            )}
            {recent.map((j) => (
              <div key={j.id} className="flex items-center justify-between gap-2 rounded-lg px-2.5 py-1.5">
                <span className="truncate text-xs text-mist">{jobTypeLabel(j.type)}</span>
                <span
                  className={`text-[11px] font-medium ${
                    j.status === "done"
                      ? "text-status-approved"
                      : j.status === "failed"
                        ? "text-status-failed"
                        : "text-fog"
                  }`}
                  title={j.error ?? undefined}
                >
                  {j.status}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
      <button
        onClick={() => setOpen((o) => !o)}
        className={`flex h-10 items-center gap-2 rounded-full border border-line-bright bg-ink-850/95 px-4 shadow-xl backdrop-blur transition-colors hover:border-fog/50 ${
          active.length ? "" : "opacity-80"
        }`}
      >
        <ListVideo size={15} className={active.length ? "text-amber-450" : "text-fog"} />
        <span className="text-sm font-medium text-paper">
          {active.length > 0
            ? `${active.length} job${active.length > 1 ? "s" : ""}`
            : "Queue"}
        </span>
        {active.some((j) => j.status === "running") && (
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-450" />
        )}
      </button>
    </div>
  );
}
