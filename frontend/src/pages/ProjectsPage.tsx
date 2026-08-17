import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import {
  Clapperboard,
  Download,
  FileDown,
  FolderUp,
  Film,
  Plus,
  Trash2,
} from "lucide-react";
import { apiDelete, apiGet, apiPost, apiPostForm, mediaUrl } from "../lib/api";
import type { Job, Project } from "../lib/types";
import { HealthBanner } from "../components/HealthBanner";
import { EmptyState, ErrorState, Skeleton } from "../components/EmptyState";
import { Modal } from "../components/Modal";
import { Button, Field, Input, ProgressBar, Select, Spinner } from "../components/ui";
import { useToast } from "../lib/toast";

const ASPECTS = ["16:9", "2.39:1", "4:3", "9:16", "1:1"];

interface ImportResponse {
  project: Project;
  warnings: string[];
  characters: {
    linked: string[];
    created: string[];
    renamed: Record<string, string>;
  };
}

function NewProjectModal({ onClose }: { onClose: () => void }) {
  const [title, setTitle] = useState("");
  const [aspect, setAspect] = useState("16:9");
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { toast } = useToast();

  const create = useMutation({
    mutationFn: () =>
      apiPost<Project>("/api/projects", { title: title.trim(), aspect_ratio: aspect }),
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      onClose();
      navigate(`/p/${p.id}`);
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (title.trim()) create.mutate();
  };

  return (
    <Modal title="New project" onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <Field label="Title">
          <Input
            autoFocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="e.g. The Last Lighthouse"
          />
        </Field>
        <Field label="Aspect ratio" hint="How wide your frame is — 16:9 is standard.">
          <Select value={aspect} onChange={(e) => setAspect(e.target.value)}>
            {ASPECTS.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </Select>
        </Field>
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={create.isPending} disabled={!title.trim()}>
            Create project
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function ImportProjectModal({ onClose }: { onClose: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<"merge" | "rename">("merge");
  const [result, setResult] = useState<ImportResponse | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { toast } = useToast();

  const doImport = useMutation({
    mutationFn: () => {
      const form = new FormData();
      form.append("file", file!);
      form.append("mode", mode);
      return apiPostForm<ImportResponse>("/api/projects/import", form);
    },
    onSuccess: (r) => {
      setResult(r);
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["characters"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  if (result) {
    const renamed = Object.entries(result.characters.renamed);
    return (
      <Modal title="Project imported" onClose={onClose}>
        <div className="space-y-4">
          <p className="text-sm text-mist">
            <span className="font-semibold text-paper">{result.project.title}</span> is now
            on your board{result.characters.linked.length > 0 && (
              <>
                {" "}
                — reusing {result.characters.linked.map((h) => `@${h}`).join(", ")}
              </>
            )}
            .
          </p>
          {renamed.length > 0 && (
            <p className="text-xs text-fog">
              Renamed to avoid clashes:{" "}
              {renamed.map(([from, to]) => `@${from} → @${to}`).join(", ")}
            </p>
          )}
          {result.warnings.length > 0 && (
            <div className="rounded-md border border-amber-450/30 bg-amber-450/10 p-3">
              <p className="mb-1 text-xs font-semibold text-amber-450">
                A few things to check
              </p>
              <ul className="list-disc space-y-1 pl-4 text-xs text-mist">
                {result.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" onClick={onClose}>
              Close
            </Button>
            <Button variant="primary" onClick={() => navigate(`/p/${result.project.id}`)}>
              Open project
            </Button>
          </div>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="Import a project" onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm text-fog">
          Bring in a <span className="font-mono text-xs">.storybored</span> file exported
          from StoryBored — the board, its images and videos come with it.
        </p>
        <input
          ref={fileInput}
          type="file"
          accept=".storybored,application/zip"
          className="hidden"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
        <button
          type="button"
          onClick={() => fileInput.current?.click()}
          className="flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-line-bright px-4 py-6 text-sm text-fog transition-colors hover:border-amber-450/50 hover:text-mist"
        >
          <FolderUp size={16} />
          {file ? file.name : "Choose a .storybored file"}
        </button>
        <Field label="If a character with the same @handle already exists…">
          <div className="space-y-2">
            <label className="flex cursor-pointer items-start gap-2 text-sm text-mist">
              <input
                type="radio"
                name="import-mode"
                checked={mode === "merge"}
                onChange={() => setMode("merge")}
                className="mt-0.5 accent-[#f0b429]"
              />
              <span>
                <span className="font-medium text-paper">Reuse my characters</span>
                <span className="block text-xs text-fog">
                  Imported shots use your existing character when the @handle matches.
                </span>
              </span>
            </label>
            <label className="flex cursor-pointer items-start gap-2 text-sm text-mist">
              <input
                type="radio"
                name="import-mode"
                checked={mode === "rename"}
                onChange={() => setMode("rename")}
                className="mt-0.5 accent-[#f0b429]"
              />
              <span>
                <span className="font-medium text-paper">Keep them separate</span>
                <span className="block text-xs text-fog">
                  Imported characters get a new @handle (e.g. @ava2) and mentions are
                  updated.
                </span>
              </span>
            </label>
          </div>
        </Field>
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={() => doImport.mutate()}
            busy={doImport.isPending}
            disabled={!file}
          >
            <FolderUp size={14} /> Import
          </Button>
        </div>
      </div>
    </Modal>
  );
}

/** The newest archive-export job for a project, if any (SSE keeps this live). */
function exportJobFor(jobs: Job[] | undefined, projectId: number): Job | undefined {
  return (jobs ?? []).find((j) => j.type === "project_export" && j.project_id === projectId);
}

export function ProjectsPage() {
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState(false);
  const qc = useQueryClient();
  const { toast } = useToast();
  const navigate = useNavigate();

  const { data, isLoading, isError, refetch } = useQuery<Project[]>({
    queryKey: ["projects"],
    queryFn: () => apiGet<Project[]>("/api/projects"),
  });

  const { data: jobs } = useQuery<Job[]>({
    queryKey: ["jobs"],
    queryFn: () => apiGet<Job[]>("/api/jobs"),
    retry: 1,
  });

  const remove = useMutation({
    mutationFn: (id: number) => apiDelete(`/api/projects/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["projects"] }),
    onError: (e: Error) => toast(e.message, "error"),
  });

  const exportProject = useMutation({
    mutationFn: (id: number) => apiPost<{ job_id: number }>(`/api/projects/${id}/export`),
    onSuccess: () => {
      toast("Packing your project into a file…", "success");
      qc.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const loadDemo = useMutation({
    mutationFn: () => apiPost<Project>("/api/demo"),
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      toast("Demo project loaded — take a look around.", "success");
      navigate(`/p/${p.id}`);
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  return (
    <div>
      <HealthBanner />
      <div className="mb-7 flex items-end justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Projects</h1>
          <p className="mt-1 text-sm text-fog">
            Every film starts as a board of shots.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => setImporting(true)}>
            <FolderUp size={15} /> Import
          </Button>
          <Button variant="primary" onClick={() => setCreating(true)}>
            <Plus size={15} /> New project
          </Button>
        </div>
      </div>

      {isLoading && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[...Array(3)].map((_, i) => (
            <Skeleton key={i} className="h-40" />
          ))}
        </div>
      )}

      {isError && <ErrorState onRetry={() => refetch()} />}

      {data && data.length === 0 && (
        <EmptyState
          icon={Clapperboard}
          title="No projects yet"
          body="Create a project, sketch out scenes and shots, then let the engine bring them to life. New here? Load the demo project to see a finished board."
          action={
            <div className="flex flex-wrap justify-center gap-2">
              <Button variant="primary" onClick={() => setCreating(true)}>
                <Plus size={15} /> Start your first project
              </Button>
              <Button
                variant="outline"
                onClick={() => loadDemo.mutate()}
                busy={loadDemo.isPending}
              >
                Load demo project
              </Button>
            </div>
          }
        />
      )}

      {data && data.length > 0 && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {data.map((p) => {
            const exportJob = exportJobFor(jobs, p.id);
            const exporting =
              exportJob?.status === "queued" || exportJob?.status === "running";
            return (
              <Link
                key={p.id}
                to={`/p/${p.id}`}
                className="group relative overflow-hidden rounded-xl border border-line bg-ink-900 transition-colors hover:border-line-bright"
              >
                <div className="sb-slate-stripes absolute inset-x-0 top-0 z-10 h-1.5 opacity-30 transition-opacity group-hover:opacity-60" />
                <div className="relative aspect-video w-full overflow-hidden bg-ink-850">
                  {p.thumbnail_path ? (
                    <img
                      src={mediaUrl(p.thumbnail_path)}
                      alt=""
                      className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.03]"
                    />
                  ) : (
                    <div className="flex h-full w-full items-center justify-center">
                      <Film size={26} className="text-fog/50" />
                    </div>
                  )}
                  <button
                    onClick={(e) => {
                      e.preventDefault();
                      if (
                        window.confirm(
                          `Delete "${p.title}"? All its generated images, videos and exports will be deleted from disk too. This can't be undone.`,
                        )
                      )
                        remove.mutate(p.id);
                    }}
                    className="absolute right-2 top-2 rounded bg-ink-900/70 p-1 text-transparent backdrop-blur-sm transition-colors hover:!text-status-failed group-hover:text-fog"
                    title="Delete project"
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
                <div className="p-5 pt-4">
                  <h2 className="truncate text-base font-semibold text-paper">{p.title}</h2>
                  <p className="mt-1 line-clamp-2 min-h-4 text-sm text-fog">{p.description}</p>
                  <div className="mt-4 flex items-center gap-2 text-[11px] text-fog">
                    <span className="rounded-full border border-line px-2 py-0.5">
                      {p.aspect_ratio}
                    </span>
                    {p.updated_at && (
                      <span>updated {new Date(p.updated_at).toLocaleDateString()}</span>
                    )}
                    <span className="ml-auto flex items-center gap-1.5">
                      {exportJob?.status === "done" && (
                        <button
                          onClick={(e) => {
                            e.preventDefault();
                            window.location.href = `/api/projects/${p.id}/export/download`;
                          }}
                          className="flex items-center gap-1 rounded-md border border-line-bright px-2 py-1 text-[11px] font-medium text-mist transition-colors hover:text-paper"
                          title="Download the exported project file"
                        >
                          <Download size={12} /> Download
                        </button>
                      )}
                      <button
                        onClick={(e) => {
                          e.preventDefault();
                          if (!exporting) exportProject.mutate(p.id);
                        }}
                        disabled={exporting}
                        className="flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11px] font-medium text-fog transition-colors hover:border-line-bright hover:text-mist disabled:opacity-70"
                        title="Export this project as a single shareable file"
                      >
                        {exporting ? <Spinner size={12} /> : <FileDown size={12} />}
                        {exporting ? "Exporting…" : "Export"}
                      </button>
                    </span>
                  </div>
                  {exporting && (
                    <div className="mt-2">
                      <ProgressBar value={exportJob?.progress ?? 0} />
                    </div>
                  )}
                  {exportJob?.status === "failed" && (
                    <p className="mt-2 text-[11px] text-status-failed">
                      Export failed: {exportJob.error ?? "unknown error"}
                    </p>
                  )}
                </div>
              </Link>
            );
          })}
        </div>
      )}

      {creating && <NewProjectModal onClose={() => setCreating(false)} />}
      {importing && <ImportProjectModal onClose={() => setImporting(false)} />}
    </div>
  );
}
