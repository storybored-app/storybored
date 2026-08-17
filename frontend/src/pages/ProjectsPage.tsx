import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { Clapperboard, Film, Plus, Trash2 } from "lucide-react";
import { apiDelete, apiGet, apiPost, mediaUrl } from "../lib/api";
import type { Project } from "../lib/types";
import { HealthBanner } from "../components/HealthBanner";
import { EmptyState, ErrorState, Skeleton } from "../components/EmptyState";
import { Modal } from "../components/Modal";
import { Button, Field, Input, Select } from "../components/ui";
import { useToast } from "../lib/toast";

const ASPECTS = ["16:9", "2.39:1", "4:3", "9:16", "1:1"];

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

export function ProjectsPage() {
  const [creating, setCreating] = useState(false);
  const qc = useQueryClient();
  const { toast } = useToast();
  const navigate = useNavigate();

  const { data, isLoading, isError, refetch } = useQuery<Project[]>({
    queryKey: ["projects"],
    queryFn: () => apiGet<Project[]>("/api/projects"),
  });

  const remove = useMutation({
    mutationFn: (id: number) => apiDelete(`/api/projects/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["projects"] }),
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
        <Button variant="primary" onClick={() => setCreating(true)}>
          <Plus size={15} /> New project
        </Button>
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
          {data.map((p) => (
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
                <span className="rounded-full border border-line px-2 py-0.5">{p.aspect_ratio}</span>
                {p.updated_at && (
                  <span>updated {new Date(p.updated_at).toLocaleDateString()}</span>
                )}
              </div>
              </div>
            </Link>
          ))}
        </div>
      )}

      {creating && <NewProjectModal onClose={() => setCreating(false)} />}
    </div>
  );
}
