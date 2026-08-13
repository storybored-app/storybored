import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  CheckCircle2,
  Image as ImageIcon,
  Minus,
  Play,
  Plus,
  Sparkles,
  Star,
  Trash2,
  X,
} from "lucide-react";
import { apiDelete, apiGet, apiPatch, apiPost, mediaUrl } from "../lib/api";
import type { BoardProject, Job, Shot, Take, WorkflowManifest } from "../lib/types";
import { activeVideoJob, shotLabel, videoTake } from "../lib/format";
import { statusLabel, StatusRing } from "./StatusRing";
import { Button, Field, Input, Select, Spinner } from "./ui";
import { useToast } from "../lib/toast";
import { MentionTextarea } from "./MentionTextarea";
import { Lightbox } from "./Lightbox";
import { ErrorBoundary } from "./ErrorBoundary";

const AUTOSAVE_MS = 800;

interface FormState {
  description: string;
  shot_type: string;
  camera: string;
  dialogue: string;
  duration_s: string;
  motion_prompt: string;
}

function formFromShot(s: Shot): FormState {
  return {
    description: s.description ?? "",
    shot_type: s.shot_type ?? "",
    camera: s.camera ?? "",
    dialogue: s.dialogue ?? "",
    duration_s: String(s.duration_s ?? 4),
    motion_prompt: s.motion_prompt ?? "",
  };
}

function diffForm(form: FormState, shot: Shot): Record<string, unknown> {
  const patch: Record<string, unknown> = {};
  if (form.description !== (shot.description ?? "")) patch.description = form.description;
  if (form.shot_type !== (shot.shot_type ?? "")) patch.shot_type = form.shot_type;
  if (form.camera !== (shot.camera ?? "")) patch.camera = form.camera;
  if (form.dialogue !== (shot.dialogue ?? "")) patch.dialogue = form.dialogue;
  if (form.motion_prompt !== (shot.motion_prompt ?? "")) patch.motion_prompt = form.motion_prompt;
  const dur = parseFloat(form.duration_s);
  if (Number.isFinite(dur) && dur > 0 && dur !== shot.duration_s) patch.duration_s = dur;
  return patch;
}

/** Dynamic params from a workflow manifest (prompt/image are auto-wired). */
function ParamFields({
  workflow,
  values,
  onChange,
}: {
  workflow?: WorkflowManifest;
  values: Record<string, string>;
  onChange: (key: string, v: string) => void;
}) {
  const params = (workflow?.parameters ?? []).filter(
    (p) => p.type !== "prompt" && p.type !== "image",
  );
  if (params.length === 0) return null;
  return (
    <div className="grid grid-cols-2 gap-3">
      {params.map((p) => {
        const label = p.label ?? p.key;
        const val = values[p.key] ?? (p.default != null ? String(p.default) : "");
        if (p.options && p.options.length) {
          return (
            <Field key={p.key} label={label}>
              <Select value={val} onChange={(e) => onChange(p.key, e.target.value)}>
                {p.options.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </Select>
            </Field>
          );
        }
        return (
          <Field key={p.key} label={label} hint={p.type === "seed" ? "Blank = random" : undefined}>
            <Input
              type={p.type === "int" || p.type === "float" || p.type === "seed" ? "number" : "text"}
              value={val}
              placeholder={p.type === "seed" ? "random" : undefined}
              onChange={(e) => onChange(p.key, e.target.value)}
            />
          </Field>
        );
      })}
    </div>
  );
}

function buildParams(
  workflow: WorkflowManifest | undefined,
  values: Record<string, string>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const p of workflow?.parameters ?? []) {
    if (p.type === "prompt" || p.type === "image") continue;
    const raw = values[p.key];
    if (raw == null || raw === "") continue;
    if (p.type === "int" || p.type === "seed") {
      const n = parseInt(raw, 10);
      if (Number.isFinite(n)) out[p.key] = n;
    } else if (p.type === "float") {
      const n = parseFloat(raw);
      if (Number.isFinite(n)) out[p.key] = n;
    } else {
      out[p.key] = raw;
    }
  }
  return out;
}

function TakeTile({
  take,
  picked,
  onOpen,
  onPick,
  onDelete,
}: {
  take: Take;
  picked: boolean;
  onOpen: () => void;
  onPick: () => void;
  onDelete: () => void;
}) {
  const url = mediaUrl(take.thumb_path ?? take.file_path);
  return (
    <div
      className={`group relative aspect-video cursor-pointer overflow-hidden rounded-md border transition-colors ${
        picked ? "border-amber-450/70" : "border-line hover:border-line-bright"
      }`}
      onClick={onOpen}
    >
      {take.status === "pending" && (
        <div className="flex h-full w-full items-center justify-center bg-ink-850">
          <Spinner />
        </div>
      )}
      {take.status === "failed" && (
        <div
          className="flex h-full w-full items-center justify-center bg-status-failed/10 px-2 text-center"
          title={take.error ?? undefined}
        >
          <span className="text-[10px] text-status-failed">failed</span>
        </div>
      )}
      {take.status === "done" &&
        (url ? (
          <img src={url} alt="" className="h-full w-full object-cover" />
        ) : (
          <div className="flex h-full w-full items-center justify-center bg-ink-850">
            <ImageIcon size={16} className="text-ink-600" />
          </div>
        ))}
      {picked && (
        <span className="absolute left-1 top-1 rounded bg-ink-950/80 p-0.5">
          <Star size={11} className="fill-amber-450 text-amber-450" />
        </span>
      )}
      {take.status === "done" && (
        <div className="absolute inset-x-0 bottom-0 flex justify-end gap-1 bg-gradient-to-t from-ink-950/90 to-transparent p-1 opacity-0 transition-opacity group-hover:opacity-100">
          {!picked && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onPick();
              }}
              className="rounded bg-ink-850/90 p-1 text-mist hover:text-amber-450"
              title="Pick this take"
            >
              <Star size={12} />
            </button>
          )}
          <button
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
            className="rounded bg-ink-850/90 p-1 text-mist hover:text-status-failed"
            title="Delete take"
          >
            <Trash2 size={12} />
          </button>
        </div>
      )}
    </div>
  );
}

export function ShotDrawer({
  shotId,
  board,
  onClose,
}: {
  shotId: number;
  board: BoardProject;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const [tab, setTab] = useState<"stills" | "video">("stills");
  const [nTakes, setNTakes] = useState(2);
  const [imageWf, setImageWf] = useState<string>("");
  const [videoWf, setVideoWf] = useState<string>("");
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [lightboxIdx, setLightboxIdx] = useState<number | null>(null);
  const [saved, setSaved] = useState(true);

  // Locate the shot + its label on the board.
  const located = useMemo(() => {
    for (let i = 0; i < (board.scenes ?? []).length; i++) {
      const shots = board.scenes![i].shots ?? [];
      const j = shots.findIndex((s) => s.id === shotId);
      if (j !== -1) return { shot: shots[j], label: shotLabel(i, j) };
    }
    return null;
  }, [board, shotId]);
  const shot = located?.shot;

  const { data: takes } = useQuery<Take[]>({
    queryKey: ["takes", shotId],
    queryFn: () => apiGet<Take[]>(`/api/shots/${shotId}/takes`),
    retry: 1,
  });

  const { data: workflows } = useQuery<WorkflowManifest[]>({
    queryKey: ["workflows"],
    queryFn: () => apiGet<WorkflowManifest[]>("/api/workflows"),
    staleTime: 60_000,
    retry: 1,
  });

  const { data: jobs } = useQuery<Job[]>({
    queryKey: ["jobs"],
    queryFn: () => apiGet<Job[]>("/api/jobs"),
    retry: 1,
  });

  const imageWorkflows = (workflows ?? []).filter((w) => w.kind === "image");
  const videoWorkflows = (workflows ?? []).filter((w) => w.kind === "video");
  // Preselect the configured default engine, falling back to any available one.
  const preselect = (list: WorkflowManifest[]) =>
    (
      list.find((w) => w.default && w.available !== false) ??
      list.find((w) => w.available !== false) ??
      list[0]
    ).id;
  useEffect(() => {
    if (!imageWf && imageWorkflows.length) setImageWf(preselect(imageWorkflows));
  }, [imageWorkflows, imageWf]);
  useEffect(() => {
    if (!videoWf && videoWorkflows.length) setVideoWf(preselect(videoWorkflows));
  }, [videoWorkflows, videoWf]);

  const selectedImageWf = imageWorkflows.find((w) => w.id === imageWf);

  /* ---- autosaving form ---- */
  const [form, setForm] = useState<FormState | null>(shot ? formFromShot(shot) : null);
  const shotRef = useRef(shot);
  shotRef.current = shot;
  const formRef = useRef(form);
  formRef.current = form;

  // Reset form when switching to a different shot.
  const lastShotId = useRef(shotId);
  useEffect(() => {
    if (lastShotId.current !== shotId) {
      flushRef.current(); // save the outgoing shot's pending edit before we swap
      lastShotId.current = shotId;
      setForm(shotRef.current ? formFromShot(shotRef.current) : null);
      setSaved(true);
      setTab("stills");
      setLightboxIdx(null);
    } else if (!formRef.current && shotRef.current) {
      setForm(formFromShot(shotRef.current));
    }
  }, [shotId, shot]);

  const save = useMutation({
    // The patch carries its own shot id so a flush-on-close still targets the
    // shot the edit belongs to, even after shotId has moved on.
    mutationFn: ({ id, patch }: { id: number; patch: Record<string, unknown> }) =>
      apiPatch(`/api/shots/${id}`, patch),
    onSuccess: () => {
      setSaved(true);
      qc.invalidateQueries({ queryKey: ["project", board.id] });
    },
    onError: (e: Error) => toast(`Couldn't save: ${e.message}`, "error"),
  });
  const saveRef = useRef(save);

  const enhance = useMutation({
    mutationFn: () =>
      apiPost<{ description: string }>(`/api/shots/${shotId}/enhance`, {
        description: form?.description,
        shot_type: form?.shot_type,
        camera: form?.camera,
      }),
    onSuccess: (r) => {
      setForm((f) => (f ? { ...f, description: r.description } : f));
      toast("Prompt enhanced — review it, tweak anything, then it saves like any edit.", "success");
    },
    onError: (e: Error) => toast(`Enhance failed: ${e.message}`, "error"),
  });
  saveRef.current = save;

  // The latest unsaved edit, kept so we can flush it on close / shot switch.
  const pendingRef = useRef<{ id: number; patch: Record<string, unknown> } | null>(null);
  const flush = () => {
    if (pendingRef.current) {
      saveRef.current.mutate(pendingRef.current);
      pendingRef.current = null;
    }
  };
  const flushRef = useRef(flush);
  flushRef.current = flush;

  useEffect(() => {
    if (!form || !shot) return;
    const patch = diffForm(form, shot);
    if (Object.keys(patch).length === 0) {
      pendingRef.current = null;
      return;
    }
    setSaved(false);
    pendingRef.current = { id: shot.id, patch };
    const t = setTimeout(flush, AUTOSAVE_MS);
    return () => clearTimeout(t);
  }, [form, shot]);

  // Flush any pending edit when the drawer unmounts (close / navigate away).
  useEffect(() => () => flushRef.current(), []);

  /* ---- actions ---- */
  const invalidateAll = () => {
    qc.invalidateQueries({ queryKey: ["project", board.id] });
    qc.invalidateQueries({ queryKey: ["takes", shotId] });
    qc.invalidateQueries({ queryKey: ["jobs"] });
  };

  const generate = useMutation({
    mutationFn: () =>
      apiPost(`/api/shots/${shotId}/generate`, {
        workflow_id: imageWf || undefined,
        n_takes: nTakes,
        params: buildParams(selectedImageWf, paramValues),
      }),
    onSuccess: () => {
      toast(`Queued ${nTakes} take${nTakes > 1 ? "s" : ""}.`, "success");
      invalidateAll();
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const pick = useMutation({
    mutationFn: (takeId: number) => apiPost(`/api/takes/${takeId}/pick`),
    onSuccess: invalidateAll,
    onError: (e: Error) => toast(e.message, "error"),
  });

  const deleteTake = useMutation({
    mutationFn: (takeId: number) => apiDelete(`/api/takes/${takeId}`),
    onSuccess: invalidateAll,
    onError: (e: Error) => toast(e.message, "error"),
  });

  const approve = useMutation({
    mutationFn: (un: boolean) =>
      apiPost(`/api/shots/${shotId}/${un ? "unapprove" : "approve"}`),
    onSuccess: invalidateAll,
    onError: (e: Error) => toast(e.message, "error"),
  });

  const renderVideo = useMutation({
    mutationFn: () =>
      apiPost(`/api/shots/${shotId}/render-video`, {
        workflow_id: videoWf || undefined,
        motion_prompt: form?.motion_prompt || undefined,
      }),
    onSuccess: () => {
      toast("Video render queued.", "success");
      invalidateAll();
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const deleteShot = useMutation({
    mutationFn: () => apiDelete(`/api/shots/${shotId}`),
    onSuccess: () => {
      onClose();
      qc.invalidateQueries({ queryKey: ["project", board.id] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && lightboxIdx === null) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, lightboxIdx]);

  if (!shot || !form) return null;

  const imageTakes = (takes ?? []).filter((t) => t.kind === "image");
  const vTake = videoTake({ ...shot, takes: takes ?? shot.takes });
  const vUrl = mediaUrl(vTake?.file_path);
  const isApproved = shot.status === "approved";
  const canApprove = shot.picked_take_id != null;

  // Video render mirrors the backend rule exactly: the shot must be approved AND
  // its picked take must be a finished still. Anything less is a guaranteed 409.
  const pickedTake = (takes ?? shot.takes ?? []).find(
    (t) => t.id === shot.picked_take_id,
  );
  const pickedDone =
    !!pickedTake && pickedTake.kind === "image" && pickedTake.status === "done";
  const videoInFlight = !!activeVideoJob(jobs, shotId) || vTake?.status === "pending";
  const canRenderVideo = isApproved && pickedDone;
  const renderHint = !isApproved
    ? "Approve this shot first — the video renders from the approved still."
    : !pickedDone
      ? "Pick a finished still first — the video starts from it."
      : null;

  return (
    <>
      <div className="fixed inset-0 z-40" onClick={onClose} />
      <aside className="sb-drawer-in fixed bottom-0 right-0 top-14 z-50 flex w-full max-w-md flex-col border-l border-line-bright bg-ink-900 shadow-2xl">
        <ErrorBoundary compact>
          {/* header */}
          <div className="flex items-center gap-3 border-b border-line px-5 py-3.5">
            <StatusRing status={shot.status} />
            <h2 className="text-sm font-semibold">Shot {located?.label}</h2>
            <span className="text-xs text-fog">{statusLabel(shot.status)}</span>
            <span className="ml-auto flex items-center gap-1 text-[11px] text-fog">
              {saved ? (
                <>
                  <Check size={12} className="text-status-approved" /> saved
                </>
              ) : (
                "saving…"
              )}
            </span>
            <button
              onClick={() => {
                if (window.confirm("Delete this shot?")) deleteShot.mutate();
              }}
              className="rounded p-1 text-fog hover:text-status-failed"
              title="Delete shot"
            >
              <Trash2 size={14} />
            </button>
            <button onClick={onClose} className="rounded p-1 text-fog hover:text-paper" aria-label="Close">
              <X size={16} />
            </button>
          </div>

          <div className="flex-1 overflow-y-auto px-5 py-4">
            {/* fields */}
            <div className="space-y-3.5">
              <Field label="Description" hint="Type @ to reference a character.">
                <div>
                  <MentionTextarea
                    value={form.description}
                    onChange={(v) => setForm((f) => (f ? { ...f, description: v } : f))}
                    rows={3}
                    placeholder="A lighthouse keeper climbs the spiral stairs, lantern in hand…"
                  />
                  <div className="mt-1.5 flex items-center justify-between gap-2">
                    <span className="text-[11px] text-fog">
                      {enhance.isPending ? "PromptSmith is polishing your notes…" : ""}
                    </span>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => enhance.mutate()}
                      busy={enhance.isPending}
                      disabled={!form.description.trim()}
                      title="Rewrite these notes as a polished image prompt — the result lands here for you to review"
                    >
                      <Sparkles size={12} /> Enhance
                    </Button>
                  </div>
                </div>
              </Field>
              <div className="grid grid-cols-3 gap-3">
                <Field label="Shot type">
                  <Input
                    value={form.shot_type}
                    onChange={(e) => setForm((f) => (f ? { ...f, shot_type: e.target.value } : f))}
                    placeholder="WIDE"
                  />
                </Field>
                <Field label="Camera">
                  <Input
                    value={form.camera}
                    onChange={(e) => setForm((f) => (f ? { ...f, camera: e.target.value } : f))}
                    placeholder="slow push in"
                  />
                </Field>
                <Field label="Duration (s)">
                  <Input
                    type="number"
                    min={0.5}
                    step={0.5}
                    value={form.duration_s}
                    onChange={(e) => setForm((f) => (f ? { ...f, duration_s: e.target.value } : f))}
                  />
                </Field>
              </div>
              <Field label="Dialogue">
                <Input
                  value={form.dialogue}
                  onChange={(e) => setForm((f) => (f ? { ...f, dialogue: e.target.value } : f))}
                  placeholder="(optional)"
                />
              </Field>
            </div>

            {/* tabs */}
            <div className="mt-6 flex gap-1 border-b border-line">
              {(["stills", "video"] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium capitalize transition-colors ${
                    tab === t
                      ? "border-amber-450 text-paper"
                      : "border-transparent text-fog hover:text-mist"
                  }`}
                >
                  {t}
                </button>
              ))}
            </div>

            {tab === "stills" && (
              <div className="space-y-4 pt-4">
                {imageWorkflows.length > 0 ? (
                  <>
                    <Field label="Engine">
                      <Select value={imageWf} onChange={(e) => setImageWf(e.target.value)}>
                        {imageWorkflows.map((w) => (
                          <option key={w.id} value={w.id} disabled={w.available === false}>
                            {w.name}
                            {w.default ? " (default)" : ""}
                            {w.available === false ? " (unavailable)" : ""}
                          </option>
                        ))}
                      </Select>
                    </Field>
                    <ParamFields
                      workflow={selectedImageWf}
                      values={paramValues}
                      onChange={(k, v) => setParamValues((p) => ({ ...p, [k]: v }))}
                    />
                    <div className="flex items-center gap-3">
                      <div className="flex items-center rounded-md border border-line">
                        <button
                          className="px-2 py-2 text-fog hover:text-paper disabled:opacity-40"
                          onClick={() => setNTakes((n) => Math.max(1, n - 1))}
                          disabled={nTakes <= 1}
                          aria-label="Fewer takes"
                        >
                          <Minus size={13} />
                        </button>
                        <span className="w-14 text-center text-sm">
                          {nTakes} take{nTakes > 1 ? "s" : ""}
                        </span>
                        <button
                          className="px-2 py-2 text-fog hover:text-paper disabled:opacity-40"
                          onClick={() => setNTakes((n) => Math.min(8, n + 1))}
                          disabled={nTakes >= 8}
                          aria-label="More takes"
                        >
                          <Plus size={13} />
                        </button>
                      </div>
                      <Button
                        variant="primary"
                        className="flex-1"
                        onClick={() => generate.mutate()}
                        busy={generate.isPending}
                        disabled={selectedImageWf?.available === false}
                      >
                        <Sparkles size={14} /> Generate
                      </Button>
                    </div>
                  </>
                ) : (
                  <p className="rounded-md border border-line p-3 text-sm text-fog">
                    No image engine is set up yet — check Settings.
                  </p>
                )}

                {imageTakes.length > 0 && (
                  <>
                    <div className="grid grid-cols-3 gap-2">
                      {imageTakes.map((t) => (
                        <TakeTile
                          key={t.id}
                          take={t}
                          picked={shot.picked_take_id === t.id}
                          onOpen={() => setLightboxIdx(imageTakes.findIndex((x) => x.id === t.id))}
                          onPick={() => pick.mutate(t.id)}
                          onDelete={() => deleteTake.mutate(t.id)}
                        />
                      ))}
                    </div>
                    <Button
                      variant={isApproved ? "outline" : "primary"}
                      className="w-full"
                      disabled={!canApprove && !isApproved}
                      onClick={() => approve.mutate(isApproved)}
                      busy={approve.isPending}
                    >
                      <CheckCircle2 size={15} />
                      {isApproved
                        ? "Approved — click to unapprove"
                        : canApprove
                          ? "Approve this shot"
                          : "Pick a take to approve"}
                    </Button>
                  </>
                )}
                {imageTakes.length === 0 && (
                  <p className="pt-1 text-center text-xs text-fog">
                    No takes yet. Describe the shot above, then hit Generate.
                  </p>
                )}
              </div>
            )}

            {tab === "video" && (
              <div className="space-y-4 pt-4">
                <Field
                  label="Motion"
                  hint="Describe how the shot should move — camera and subject."
                >
                  <MentionTextarea
                    value={form.motion_prompt}
                    onChange={(v) => setForm((f) => (f ? { ...f, motion_prompt: v } : f))}
                    rows={2}
                    placeholder="waves crash as the light sweeps past, slow dolly forward"
                  />
                </Field>
                {videoWorkflows.length > 0 && (
                  <Field label="Engine">
                    <Select value={videoWf} onChange={(e) => setVideoWf(e.target.value)}>
                      {videoWorkflows.map((w) => (
                        <option key={w.id} value={w.id} disabled={w.available === false}>
                          {w.name}
                          {w.default ? " (default)" : ""}
                          {w.available === false ? " (unavailable)" : ""}
                        </option>
                      ))}
                    </Select>
                  </Field>
                )}
                <Button
                  variant="primary"
                  className="w-full"
                  onClick={() => renderVideo.mutate()}
                  busy={renderVideo.isPending}
                  disabled={!canRenderVideo || videoInFlight}
                >
                  <Play size={14} />{" "}
                  {videoInFlight ? "Rendering…" : "Render video"}
                </Button>
                {renderHint && (
                  <p className="text-center text-xs text-fog">{renderHint}</p>
                )}
                {vTake && vTake.status === "done" && vUrl && (
                  <video src={vUrl} controls className="w-full rounded-lg border border-line" />
                )}
                {vTake && vTake.status === "pending" && (
                  <div className="flex items-center justify-center gap-2 rounded-md border border-line p-4 text-sm text-fog">
                    <Spinner size={14} /> Rendering…
                  </div>
                )}
                {vTake && vTake.status === "failed" && (
                  <p className="rounded-md border border-status-failed/30 bg-status-failed/10 p-3 text-xs text-status-failed">
                    {vTake.error ?? "The video render failed."}
                  </p>
                )}
              </div>
            )}
          </div>
        </ErrorBoundary>
      </aside>

      {lightboxIdx !== null && (
        <Lightbox
          takes={imageTakes}
          index={lightboxIdx}
          pickedId={shot.picked_take_id}
          onClose={() => setLightboxIdx(null)}
          onNavigate={setLightboxIdx}
          onPick={(t) => pick.mutate(t.id)}
        />
      )}
    </>
  );
}
