import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Check, Clapperboard, Download, Film, Play } from "lucide-react";
import { apiGet, apiPost, mediaUrl } from "../lib/api";
import type { BoardProject, ExportEntry, Job, Shot } from "../lib/types";
import { activeVideoJob, formatDuration, shotLabel, videoTake } from "../lib/format";
import { Badge, Button, ProgressBar, Spinner } from "../components/ui";
import { EmptyState, ErrorState, Skeleton } from "../components/EmptyState";
import { useToast } from "../lib/toast";

function exportPath(e: ExportEntry | string): string | undefined {
  if (typeof e === "string") return e;
  return e.file_path ?? e.path;
}

export function ExportPage() {
  const { id } = useParams();
  const projectId = Number(id);
  const qc = useQueryClient();
  const { toast } = useToast();
  const [animaticJobId, setAnimaticJobId] = useState<number | null>(null);
  const [titleCards, setTitleCards] = useState(true);
  const [animaticScene, setAnimaticScene] = useState<number | "all">("all");

  const { data, isLoading, isError, refetch } = useQuery<BoardProject>({
    queryKey: ["project", projectId],
    queryFn: () => apiGet<BoardProject>(`/api/projects/${projectId}`),
    enabled: Number.isFinite(projectId),
  });

  const { data: exports } = useQuery<(ExportEntry | string)[]>({
    queryKey: ["exports", projectId],
    queryFn: () => apiGet<(ExportEntry | string)[]>(`/api/projects/${projectId}/exports`),
    enabled: Number.isFinite(projectId),
    retry: 1,
  });

  const { data: jobs } = useQuery<Job[]>({
    queryKey: ["jobs"],
    queryFn: () => apiGet<Job[]>("/api/jobs"),
    retry: 1,
  });
  const animaticJob = useMemo(
    () =>
      animaticJobId != null
        ? (jobs ?? []).find((j) => j.id === animaticJobId) ?? null
        : null,
    [jobs, animaticJobId],
  );

  const approvedShots = useMemo(() => {
    const out: { shot: Shot; label: string }[] = [];
    (data?.scenes ?? []).forEach((scene, i) => {
      (scene.shots ?? []).forEach((shot, j) => {
        if (shot.status === "approved") out.push({ shot, label: shotLabel(i, j) });
      });
    });
    return out;
  }, [data]);

  const renderOne = useMutation({
    mutationFn: (shotId: number) => apiPost(`/api/shots/${shotId}/render-video`, {}),
    onSuccess: () => {
      toast("Video render queued.", "success");
      qc.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const renderAll = useMutation({
    mutationFn: () => apiPost(`/api/projects/${projectId}/render-videos`, {}),
    onSuccess: () => {
      toast("Queued videos for all approved shots that need one.", "success");
      qc.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const exportAnimatic = useMutation({
    mutationFn: () =>
      apiPost<{ job_id: number }>(`/api/projects/${projectId}/animatic`, {
        title_cards: titleCards,
        scene_id: animaticScene === "all" ? undefined : animaticScene,
      }),
    onSuccess: (r) => {
      setAnimaticJobId(r.job_id);
      toast("Cutting your animatic…", "success");
      qc.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  if (isLoading)
    return (
      <div className="space-y-4">
        <Skeleton className="h-9 w-72" />
        <Skeleton className="h-64" />
      </div>
    );
  if (isError || !data)
    return <ErrorState body="This project couldn't be loaded." onRetry={() => refetch()} />;

  // The backend already returns exports newest-first; sort by created_at desc
  // when present so we always play/download the latest cut, not the oldest.
  const latestExport = [...(exports ?? [])]
    .sort((a, b) => {
      const ta = typeof a === "string" ? "" : a.created_at ?? "";
      const tb = typeof b === "string" ? "" : b.created_at ?? "";
      return tb.localeCompare(ta);
    })
    .map(exportPath)
    .filter((p): p is string => !!p)[0];
  const latestUrl = mediaUrl(latestExport);

  return (
    <div className="mx-auto max-w-4xl">
      <div className="mb-6 flex items-center gap-3">
        <Link
          to={`/p/${projectId}`}
          className="rounded-md p-1.5 text-fog hover:text-paper"
          title="Back to board"
        >
          <ArrowLeft size={17} />
        </Link>
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Export</h1>
          <p className="text-xs text-fog">{data.title}</p>
        </div>
      </div>

      {approvedShots.length === 0 ? (
        <EmptyState
          icon={Clapperboard}
          title="Nothing approved yet"
          body="Approve shots on the board (generate stills, pick your favorite take, hit Approve) and they'll line up here for the final cut."
          action={
            <Link to={`/p/${projectId}`}>
              <Button variant="primary">Back to the board</Button>
            </Link>
          }
        />
      ) : (
        <div className="space-y-6">
          {/* approved shots checklist */}
          <section className="overflow-hidden rounded-xl border border-line bg-ink-900/40">
            <header className="flex items-center justify-between border-b border-line px-4 py-3">
              <h2 className="text-sm font-semibold">
                Approved shots{" "}
                <span className="font-normal text-fog">({approvedShots.length})</span>
              </h2>
              <Button size="sm" onClick={() => renderAll.mutate()} busy={renderAll.isPending}>
                <Film size={13} /> Render all videos
              </Button>
            </header>
            <ul>
              {approvedShots.map(({ shot, label }) => {
                const v = videoTake(shot);
                // A video_gen job may be queued/running before its take row
                // exists — treat that as "rendering" so we don't re-queue it.
                const jobActive = !!activeVideoJob(jobs, shot.id);
                const rendering = v?.status === "pending" || jobActive;
                const busyThis =
                  renderOne.isPending && renderOne.variables === shot.id;
                return (
                  <li
                    key={shot.id}
                    className="flex items-center gap-3 border-b border-line/50 px-4 py-2.5 last:border-b-0"
                  >
                    <span className="w-10 text-sm font-semibold text-paper">{label}</span>
                    <span className="min-w-0 flex-1 truncate text-sm text-mist">
                      {shot.description || "Untitled shot"}
                    </span>
                    <span className="text-xs text-fog">{formatDuration(shot.duration_s)}</span>
                    {v?.status === "done" ? (
                      <Badge tone="green">
                        <Check size={10} /> video
                      </Badge>
                    ) : rendering ? (
                      <Badge tone="amber" pulse>
                        rendering
                      </Badge>
                    ) : v?.status === "failed" ? (
                      <Badge tone="red">failed</Badge>
                    ) : (
                      <Badge tone="fog">still only</Badge>
                    )}
                    {v?.status !== "done" && (
                      <Button
                        size="sm"
                        onClick={() => renderOne.mutate(shot.id)}
                        disabled={rendering || busyThis}
                      >
                        <Play size={12} /> {rendering ? "Rendering…" : "Render"}
                      </Button>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>

          {/* animatic */}
          <section className="rounded-xl border border-line bg-ink-900/40 p-5">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="text-sm font-semibold">Animatic</h2>
                <p className="mt-1 text-xs leading-relaxed text-fog">
                  One MP4 of your board, in order — finished videos where you have
                  them, held stills where you don't. Export the whole thing or a
                  single scene.
                </p>
                <div className="mt-3 flex flex-wrap items-center gap-4">
                  <select
                    value={animaticScene === "all" ? "all" : String(animaticScene)}
                    onChange={(e) =>
                      setAnimaticScene(e.target.value === "all" ? "all" : Number(e.target.value))
                    }
                    className="h-8 rounded-md border border-line bg-ink-900 px-2 text-xs text-mist"
                  >
                    <option value="all">Whole board</option>
                    {(data.scenes ?? []).map((scene, i) => (
                      <option key={scene.id} value={scene.id}>
                        Scene {i + 1}: {scene.title || scene.slugline || "Untitled"}
                      </option>
                    ))}
                  </select>
                  <label className="flex items-center gap-2 text-xs text-fog">
                    <input
                      type="checkbox"
                      checked={titleCards}
                      onChange={(e) => setTitleCards(e.target.checked)}
                      className="h-4 w-4 accent-[#f0b429]"
                    />
                    Scene title cards
                  </label>
                </div>
              </div>
              <Button
                variant="primary"
                onClick={() => exportAnimatic.mutate()}
                busy={
                  exportAnimatic.isPending ||
                  animaticJob?.status === "running" ||
                  animaticJob?.status === "queued"
                }
              >
                <Clapperboard size={14} /> Export animatic
              </Button>
            </div>

            {animaticJob && (animaticJob.status === "running" || animaticJob.status === "queued") && (
              <div className="mt-4 space-y-2">
                <div className="flex items-center gap-2 text-xs text-fog">
                  <Spinner size={13} />
                  {animaticJob.detail || "Assembling clips…"}
                </div>
                <ProgressBar value={animaticJob.progress} />
              </div>
            )}
            {animaticJob?.status === "failed" && (
              <p className="mt-4 rounded-md border border-status-failed/30 bg-status-failed/10 p-3 text-xs text-status-failed">
                Export failed: {animaticJob.error ?? "unknown error"}
              </p>
            )}

            {latestUrl && (
              <div className="mt-5 space-y-3">
                <video src={latestUrl} controls className="w-full rounded-lg border border-line" />
                <a
                  href={latestUrl}
                  download
                  className="inline-flex h-9 items-center gap-2 rounded-md border border-line-bright px-3.5 text-sm font-medium text-mist hover:text-paper"
                >
                  <Download size={14} /> Download MP4
                </a>
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
