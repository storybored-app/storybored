import { useEffect, useRef, useState } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import type { Character, Job, Shot, Take } from "./types";

// One EventSource for the whole app. Job events patch the query cache
// directly (no refetch); shot/take/character events invalidate with a
// trailing debounce so a burst of events causes one refetch, not thirty.

const DEBOUNCE_MS = 400;

function makeDebouncedInvalidator() {
  const timers = new Map<string, ReturnType<typeof setTimeout>>();
  return (keyId: string, run: () => void) => {
    const existing = timers.get(keyId);
    if (existing) clearTimeout(existing);
    timers.set(
      keyId,
      setTimeout(() => {
        timers.delete(keyId);
        run();
      }, DEBOUNCE_MS),
    );
  };
}

function patchJobsCache(qc: QueryClient, job: Job) {
  qc.setQueriesData<Job[]>({ queryKey: ["jobs"] }, (old) => {
    if (!old) return old;
    const i = old.findIndex((j) => j.id === job.id);
    if (i === -1) return [job, ...old];
    const next = old.slice();
    next[i] = job;
    return next;
  });
  qc.setQueryData<Job>(["job", job.id], job);
}

export function useEvents(): boolean {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(false);
  const invalidateRef = useRef<ReturnType<typeof makeDebouncedInvalidator>>();
  if (!invalidateRef.current) invalidateRef.current = makeDebouncedInvalidator();

  useEffect(() => {
    const debounced = invalidateRef.current!;
    let es: EventSource | null = null;
    let closed = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (closed) return;
      es = new EventSource("/api/events");
      es.onopen = () => setConnected(true);
      es.onerror = () => {
        setConnected(false);
        // EventSource auto-reconnects, but some proxies kill it for good;
        // recycle the connection after a pause to be safe.
        if (es && es.readyState === EventSource.CLOSED) {
          es.close();
          if (!closed) retryTimer = setTimeout(connect, 4000);
        }
      };

      es.addEventListener("job", (ev) => {
        try {
          const job = JSON.parse((ev as MessageEvent).data) as Job;
          patchJobsCache(qc, job);
          if (job.status === "done" || job.status === "failed") {
            // Finished jobs can change board/exports/characters state.
            debounced("finished-jobs", () => {
              qc.invalidateQueries({ queryKey: ["project"] });
              qc.invalidateQueries({ queryKey: ["exports"] });
            });
          }
        } catch {
          /* malformed event */
        }
      });

      es.addEventListener("shot", (ev) => {
        try {
          const shot = JSON.parse((ev as MessageEvent).data) as Shot;
          debounced("shots", () => {
            qc.invalidateQueries({ queryKey: ["project"] });
          });
          debounced(`takes-${shot.id}`, () => {
            qc.invalidateQueries({ queryKey: ["takes", shot.id] });
          });
        } catch {
          /* malformed event */
        }
      });

      es.addEventListener("take", (ev) => {
        try {
          const take = JSON.parse((ev as MessageEvent).data) as Take;
          debounced(`takes-${take.shot_id}`, () => {
            qc.invalidateQueries({ queryKey: ["takes", take.shot_id] });
          });
          debounced("shots", () => {
            qc.invalidateQueries({ queryKey: ["project"] });
          });
        } catch {
          /* malformed event */
        }
      });

      es.addEventListener("character", (ev) => {
        try {
          const ch = JSON.parse((ev as MessageEvent).data) as Character;
          debounced("characters", () => {
            qc.invalidateQueries({ queryKey: ["characters"] });
          });
          debounced(`training-${ch.id}`, () => {
            qc.invalidateQueries({ queryKey: ["training", ch.id] });
          });
        } catch {
          /* malformed event */
        }
      });
    };

    connect();
    return () => {
      closed = true;
      if (retryTimer) clearTimeout(retryTimer);
      es?.close();
    };
  }, [qc]);

  return connected;
}
