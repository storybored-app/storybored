import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragOverEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  horizontalListSortingStrategy,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  ArrowLeft,
  Clapperboard,
  Clock,
  FileText,
  Film,
  GripVertical,
  Link2,
  Plus,
  Trash2,
} from "lucide-react";
import { apiDelete, apiGet, apiPatch, apiPost, mediaUrl } from "../lib/api";
import type { BoardProject, Scene, Shot } from "../lib/types";
import { formatDuration, shotLabel, shotThumbTake } from "../lib/format";
import { StatusRing } from "../components/StatusRing";
import { EmptyState, ErrorState, Skeleton } from "../components/EmptyState";
import { Button } from "../components/ui";
import { useToast } from "../lib/toast";
import { ShotDrawer } from "../components/ShotDrawer";
import { HealthBanner } from "../components/HealthBanner";

/* ---------- shot card ---------- */

function ShotCardInner({
  shot,
  sceneIndex,
  shotIndex,
  onClick,
  dragging,
}: {
  shot: Shot;
  sceneIndex: number;
  shotIndex: number;
  onClick?: () => void;
  dragging?: boolean;
}) {
  const thumb = shotThumbTake(shot);
  const url = mediaUrl(thumb?.thumb_path ?? thumb?.file_path);
  return (
    <div
      onClick={onClick}
      className={`group/card w-44 shrink-0 cursor-pointer overflow-hidden rounded-lg border bg-ink-900 transition-colors ${
        dragging ? "border-amber-450/60 shadow-2xl" : "border-line hover:border-line-bright"
      }`}
    >
      <div className="relative aspect-video w-full overflow-hidden bg-ink-850">
        {url ? (
          <img src={url} alt="" className="h-full w-full object-cover" draggable={false} />
        ) : (
          <div className="flex h-full w-full items-center justify-center">
            <Clapperboard size={20} className="text-ink-600" strokeWidth={1.5} />
          </div>
        )}
        {shot.shot_type && (
          <span className="absolute left-1.5 top-1.5 rounded bg-ink-950/80 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-mist backdrop-blur-sm">
            {shot.shot_type}
          </span>
        )}
      </div>
      <div className="flex items-center gap-2 px-2.5 py-2">
        <StatusRing status={shot.status} />
        <span className="text-xs font-semibold text-paper">
          {shotLabel(sceneIndex, shotIndex)}
        </span>
        <span className="min-w-0 flex-1 truncate text-[11px] text-fog">
          {shot.description || "Untitled shot"}
        </span>
        <span className="flex shrink-0 items-center gap-0.5 text-[10px] text-fog">
          <Clock size={9} />
          {formatDuration(shot.duration_s)}
        </span>
      </div>
    </div>
  );
}

function SortableShotCard(props: {
  shot: Shot;
  sceneIndex: number;
  shotIndex: number;
  onClick: () => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: `shot-${props.shot.id}` });
  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={isDragging ? "opacity-30" : ""}
      {...attributes}
      {...listeners}
    >
      <ShotCardInner {...props} />
    </div>
  );
}

/* ---------- scene strip ---------- */

function SceneStrip({
  scene,
  sceneIndex,
  onOpenShot,
  onAddShot,
  onRename,
  onDelete,
  addingShot,
}: {
  scene: Scene;
  sceneIndex: number;
  onOpenShot: (id: number) => void;
  onAddShot: () => void;
  onRename: (patch: Partial<Scene>) => void;
  onDelete: () => void;
  addingShot: boolean;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: `scene-${scene.id}` });
  const [title, setTitle] = useState(scene.title);
  const [slugline, setSlugline] = useState(scene.slugline);
  const [look, setLook] = useState(scene.look ?? "");
  useEffect(() => setTitle(scene.title), [scene.title]);
  useEffect(() => setSlugline(scene.slugline), [scene.slugline]);
  useEffect(() => setLook(scene.look ?? ""), [scene.look]);

  const shots = scene.shots ?? [];

  return (
    <section
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={`rounded-xl border border-line bg-ink-900/40 ${isDragging ? "opacity-40" : ""}`}
    >
      <header className="group flex items-center gap-2 border-b border-line px-4 py-2.5">
        <button
          className="cursor-grab touch-none rounded p-1 text-fog/60 hover:text-fog active:cursor-grabbing"
          {...attributes}
          {...listeners}
          aria-label="Drag to reorder scene"
        >
          <GripVertical size={15} />
        </button>
        <span className="flex h-6 w-6 items-center justify-center rounded border border-amber-450/30 bg-amber-450/10 text-xs font-bold text-amber-450">
          {sceneIndex + 1}
        </span>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onBlur={() => title !== scene.title && onRename({ title })}
          placeholder="Scene title"
          className="min-w-0 flex-shrink bg-transparent text-sm font-semibold text-paper placeholder:text-fog/50 focus:outline-none"
        />
        <input
          value={slugline}
          onChange={(e) => setSlugline(e.target.value)}
          onBlur={() => slugline !== scene.slugline && onRename({ slugline })}
          placeholder="INT. LOCATION — DAY"
          className="min-w-0 flex-1 bg-transparent text-xs uppercase tracking-wide text-fog placeholder:text-fog/40 focus:outline-none"
        />
        <span className="text-[11px] text-fog/70">
          {shots.length} shot{shots.length === 1 ? "" : "s"}
        </span>
        <button
          onClick={onDelete}
          className="rounded p-1 text-transparent transition-colors hover:!text-status-failed group-hover:text-fog/60"
          title="Delete scene"
        >
          <Trash2 size={14} />
        </button>
      </header>
      <div className="border-b border-line/60 px-4 py-1.5">
        <input
          value={look}
          onChange={(e) => setLook(e.target.value)}
          onBlur={() => look !== (scene.look ?? "") && onRename({ look })}
          placeholder="Scene look — place, light, weather, palette (pinned into renders when Continuity is on)"
          className="w-full bg-transparent text-xs text-fog placeholder:text-fog/40 focus:outline-none"
          title="The scene's visual environment. With Continuity on, this is appended to every image render in the scene and steers Enhance."
        />
      </div>
      <div className="flex items-stretch gap-3 overflow-x-auto p-3">
        <SortableContext
          items={shots.map((s) => `shot-${s.id}`)}
          strategy={horizontalListSortingStrategy}
        >
          {shots.map((shot, i) => (
            <SortableShotCard
              key={shot.id}
              shot={shot}
              sceneIndex={sceneIndex}
              shotIndex={i}
              onClick={() => onOpenShot(shot.id)}
            />
          ))}
        </SortableContext>
        <button
          onClick={onAddShot}
          disabled={addingShot}
          className="flex w-24 shrink-0 flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed border-line text-fog transition-colors hover:border-amber-450/40 hover:text-amber-450 disabled:opacity-50"
          style={{ minHeight: 124 }}
        >
          <Plus size={16} />
          <span className="text-[11px] font-medium">shot</span>
        </button>
      </div>
    </section>
  );
}

/* ---------- board page ---------- */

export function BoardPage() {
  const { id } = useParams();
  const projectId = Number(id);
  const qc = useQueryClient();
  const { toast } = useToast();
  const [openShotId, setOpenShotId] = useState<number | null>(null);
  const [activeDrag, setActiveDrag] = useState<string | null>(null);
  // Local mirror of scenes so drags feel instant.
  const [scenes, setScenes] = useState<Scene[]>([]);
  const draggingRef = useRef(false);

  const { data, isLoading, isError, refetch } = useQuery<BoardProject>({
    queryKey: ["project", projectId],
    queryFn: () => apiGet<BoardProject>(`/api/projects/${projectId}`),
    enabled: Number.isFinite(projectId),
  });

  useEffect(() => {
    if (data?.scenes && !draggingRef.current) setScenes(data.scenes);
  }, [data]);

  const invalidate = useCallback(
    () => qc.invalidateQueries({ queryKey: ["project", projectId] }),
    [qc, projectId],
  );

  const onError = useCallback(
    (e: Error) => {
      toast(e.message, "error");
      invalidate();
    },
    [toast, invalidate],
  );

  const addScene = useMutation({
    mutationFn: () =>
      apiPost(`/api/projects/${projectId}/scenes`, { title: `Scene ${scenes.length + 1}` }),
    onSuccess: invalidate,
    onError,
  });
  const patchScene = useMutation({
    mutationFn: ({ sceneId, patch }: { sceneId: number; patch: Partial<Scene> }) =>
      apiPatch(`/api/scenes/${sceneId}`, patch),
    onSuccess: invalidate,
    onError,
  });
  const deleteScene = useMutation({
    mutationFn: (sceneId: number) => apiDelete(`/api/scenes/${sceneId}`),
    onSuccess: invalidate,
    onError,
  });
  const addShot = useMutation({
    mutationFn: (sceneId: number) => apiPost(`/api/scenes/${sceneId}/shots`, {}),
    onSuccess: invalidate,
    onError,
  });
  const toggleContinuity = useMutation({
    mutationFn: () =>
      apiPatch(`/api/projects/${projectId}`, {
        continuity_enabled: !data?.continuity_enabled,
      }),
    onSuccess: invalidate,
    onError,
  });
  const renderVideos = useMutation({
    mutationFn: () => apiPost(`/api/projects/${projectId}/render-videos`, {}),
    onSuccess: () => toast("Queued video renders for approved shots.", "success"),
    onError,
  });

  const shotContainer = useCallback(
    (shotId: number) => scenes.find((sc) => (sc.shots ?? []).some((s) => s.id === shotId)),
    [scenes],
  );

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
  );

  const handleDragStart = (e: DragStartEvent) => {
    draggingRef.current = true;
    setActiveDrag(String(e.active.id));
  };

  const handleDragOver = (e: DragOverEvent) => {
    const activeId = String(e.active.id);
    const overId = e.over ? String(e.over.id) : null;
    if (!overId || !activeId.startsWith("shot-")) return;

    const shotId = Number(activeId.slice(5));
    const from = shotContainer(shotId);
    let to: Scene | undefined;
    if (overId.startsWith("shot-")) to = shotContainer(Number(overId.slice(5)));
    else if (overId.startsWith("scene-"))
      to = scenes.find((sc) => sc.id === Number(overId.slice(6)));
    if (!from || !to || from.id === to.id) return;

    // Move the shot into the target scene locally so the strip opens up.
    setScenes((prev) => {
      const next = prev.map((sc) => ({ ...sc, shots: [...(sc.shots ?? [])] }));
      const src = next.find((sc) => sc.id === from.id)!;
      const dst = next.find((sc) => sc.id === to!.id)!;
      const idx = src.shots!.findIndex((s) => s.id === shotId);
      if (idx === -1) return prev;
      const [moved] = src.shots!.splice(idx, 1);
      let insertAt = dst.shots!.length;
      if (overId.startsWith("shot-")) {
        const overIdx = dst.shots!.findIndex((s) => s.id === Number(overId.slice(5)));
        if (overIdx !== -1) insertAt = overIdx;
      }
      dst.shots!.splice(insertAt, 0, { ...moved, scene_id: dst.id });
      return next;
    });
  };

  const handleDragEnd = async (e: DragEndEvent) => {
    const activeId = String(e.active.id);
    const overId = e.over ? String(e.over.id) : null;
    setActiveDrag(null);

    try {
      if (activeId.startsWith("scene-") && overId?.startsWith("scene-") && activeId !== overId) {
        const fromIdx = scenes.findIndex((s) => `scene-${s.id}` === activeId);
        const toIdx = scenes.findIndex((s) => `scene-${s.id}` === overId);
        if (fromIdx !== -1 && toIdx !== -1) {
          const next = [...scenes];
          const [moved] = next.splice(fromIdx, 1);
          next.splice(toIdx, 0, moved);
          setScenes(next);
          await apiPost(`/api/projects/${projectId}/scenes/reorder`, {
            scene_ids: next.map((s) => s.id),
          });
        }
      } else if (activeId.startsWith("shot-")) {
        const shotId = Number(activeId.slice(5));
        const container = shotContainer(shotId);
        if (container && overId?.startsWith("shot-") && activeId !== overId) {
          const overShotId = Number(overId.slice(5));
          if ((container.shots ?? []).some((s) => s.id === overShotId)) {
            // Reorder within the (possibly new) scene.
            setScenes((prev) =>
              prev.map((sc) => {
                if (sc.id !== container.id) return sc;
                const shots = [...(sc.shots ?? [])];
                const fromIdx = shots.findIndex((s) => s.id === shotId);
                const toIdx = shots.findIndex((s) => s.id === overShotId);
                const [moved] = shots.splice(fromIdx, 1);
                shots.splice(toIdx, 0, moved);
                return { ...sc, shots };
              }),
            );
          }
        }
        // Persist: if the shot changed scene, PATCH first, then reorder.
        const finalScenes = await new Promise<Scene[]>((resolve) =>
          setScenes((prev) => {
            resolve(prev);
            return prev;
          }),
        );
        const home = finalScenes.find((sc) => (sc.shots ?? []).some((s) => s.id === shotId));
        if (home) {
          const original = data?.scenes?.find((sc) =>
            (sc.shots ?? []).some((s) => s.id === shotId),
          );
          if (original && original.id !== home.id) {
            await apiPatch(`/api/shots/${shotId}`, { scene_id: home.id });
            await apiPost(`/api/scenes/${original.id}/shots/reorder`, {
              shot_ids: (finalScenes.find((s) => s.id === original.id)?.shots ?? []).map(
                (s) => s.id,
              ),
            });
          }
          await apiPost(`/api/scenes/${home.id}/shots/reorder`, {
            shot_ids: (home.shots ?? []).map((s) => s.id),
          });
        }
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "Couldn't save the new order.", "error");
    } finally {
      draggingRef.current = false;
      invalidate();
    }
  };

  const activeShot = useMemo(() => {
    if (!activeDrag?.startsWith("shot-")) return null;
    const sid = Number(activeDrag.slice(5));
    for (let i = 0; i < scenes.length; i++) {
      const j = (scenes[i].shots ?? []).findIndex((s) => s.id === sid);
      if (j !== -1) return { shot: scenes[i].shots![j], sceneIndex: i, shotIndex: j };
    }
    return null;
  }, [activeDrag, scenes]);

  if (isLoading)
    return (
      <div className="space-y-5">
        <Skeleton className="h-9 w-72" />
        <Skeleton className="h-44" />
        <Skeleton className="h-44" />
      </div>
    );
  if (isError || !data)
    return <ErrorState body="This board couldn't be loaded." onRetry={() => refetch()} />;

  return (
    <div>
      <HealthBanner />
      <div className="mb-6 flex flex-wrap items-center gap-3">
        <Link to="/" className="rounded-md p-1.5 text-fog hover:text-paper" title="All projects">
          <ArrowLeft size={17} />
        </Link>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-xl font-semibold tracking-tight">{data.title}</h1>
          <p className="text-xs text-fog">
            {scenes.length} scene{scenes.length === 1 ? "" : "s"} ·{" "}
            {scenes.reduce((n, s) => n + (s.shots?.length ?? 0), 0)} shots · {data.aspect_ratio}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            onClick={() => toggleContinuity.mutate()}
            busy={toggleContinuity.isPending}
            title={
              data.continuity_enabled
                ? "Continuity is on: each scene's look is appended to every image render in that scene, and Enhance keeps wardrobe, props, and light consistent across the scene's shots. Click to turn off."
                : "Continuity is off: renders use shot prompts alone. Click to pin each scene's look into its renders and keep shots consistent."
            }
          >
            <Link2 size={14} className={data.continuity_enabled ? "text-amber-450" : ""} />{" "}
            Continuity {data.continuity_enabled ? "on" : "off"}
          </Button>
          <Button onClick={() => renderVideos.mutate()} busy={renderVideos.isPending}>
            <Film size={14} /> Render videos
          </Button>
          <Link to={`/p/${projectId}/script`}>
            <Button>
              <FileText size={14} /> Import script
            </Button>
          </Link>
          <Link to={`/p/${projectId}/export`}>
            <Button variant="primary">
              <Clapperboard size={14} /> Export
            </Button>
          </Link>
        </div>
      </div>

      {scenes.length === 0 ? (
        <EmptyState
          icon={Clapperboard}
          title="An empty board, full of possibility"
          body="Add your first scene, or paste a script and let StoryBored draft the shot list for you."
          action={
            <div className="flex gap-2">
              <Button variant="primary" onClick={() => addScene.mutate()} busy={addScene.isPending}>
                <Plus size={15} /> Add a scene
              </Button>
              <Link to={`/p/${projectId}/script`}>
                <Button>
                  <FileText size={14} /> Import script
                </Button>
              </Link>
            </div>
          }
        />
      ) : (
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragStart={handleDragStart}
          onDragOver={handleDragOver}
          onDragEnd={handleDragEnd}
          onDragCancel={() => {
            draggingRef.current = false;
            setActiveDrag(null);
            invalidate();
          }}
        >
          <SortableContext
            items={scenes.map((s) => `scene-${s.id}`)}
            strategy={verticalListSortingStrategy}
          >
            <div className="space-y-5">
              {scenes.map((scene, i) => (
                <SceneStrip
                  key={scene.id}
                  scene={scene}
                  sceneIndex={i}
                  onOpenShot={setOpenShotId}
                  onAddShot={() => addShot.mutate(scene.id)}
                  addingShot={addShot.isPending}
                  onRename={(patch) => patchScene.mutate({ sceneId: scene.id, patch })}
                  onDelete={() => {
                    if (
                      window.confirm(
                        `Delete scene ${i + 1}${scene.title ? ` "${scene.title}"` : ""} and its shots?`,
                      )
                    )
                      deleteScene.mutate(scene.id);
                  }}
                />
              ))}
            </div>
          </SortableContext>
          <DragOverlay>
            {activeShot && (
              <ShotCardInner
                shot={activeShot.shot}
                sceneIndex={activeShot.sceneIndex}
                shotIndex={activeShot.shotIndex}
                dragging
              />
            )}
          </DragOverlay>
        </DndContext>
      )}

      {scenes.length > 0 && (
        <div className="mt-5">
          <Button onClick={() => addScene.mutate()} busy={addScene.isPending}>
            <Plus size={15} /> Add scene
          </Button>
        </div>
      )}

      {openShotId != null && (
        <ShotDrawer
          shotId={openShotId}
          board={{ ...data, scenes }}
          onClose={() => setOpenShotId(null)}
        />
      )}
    </div>
  );
}
