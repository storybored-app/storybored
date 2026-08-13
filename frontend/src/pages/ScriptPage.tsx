import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, FileText, Sparkles, Wand2 } from "lucide-react";
import { apiGet, apiPost, ApiError } from "../lib/api";
import type { BoardProject, BreakdownDraft, DraftScene } from "../lib/types";
import { Button, Field, Spinner, TextArea } from "../components/ui";
import { useToast } from "../lib/toast";
import { EmptyState } from "../components/EmptyState";

interface Checked {
  scenes: boolean[];
  shots: boolean[][];
}

function allChecked(draft: BreakdownDraft): Checked {
  return {
    scenes: draft.scenes.map(() => true),
    shots: draft.scenes.map((s) => s.shots.map(() => true)),
  };
}

function filterDraft(draft: BreakdownDraft, checked: Checked): BreakdownDraft {
  const scenes: DraftScene[] = [];
  draft.scenes.forEach((scene, i) => {
    if (!checked.scenes[i]) return;
    const shots = scene.shots.filter((_, j) => checked.shots[i][j]);
    if (shots.length) scenes.push({ ...scene, shots });
  });
  return { scenes };
}

export function ScriptPage() {
  const { id } = useParams();
  const projectId = Number(id);
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { toast } = useToast();

  const [text, setText] = useState("");
  const [draft, setDraft] = useState<BreakdownDraft | null>(null);
  const [checked, setChecked] = useState<Checked | null>(null);
  const [llmUnconfigured, setLlmUnconfigured] = useState(false);

  const { data: project } = useQuery<BoardProject>({
    queryKey: ["project", projectId],
    queryFn: () => apiGet<BoardProject>(`/api/projects/${projectId}`),
    enabled: Number.isFinite(projectId),
    retry: 1,
  });

  const breakdown = useMutation({
    mutationFn: () =>
      apiPost<BreakdownDraft>("/api/breakdown", {
        project_id: projectId,
        script_text: text,
      }),
    onSuccess: (d) => {
      if (!d?.scenes?.length) {
        toast("The draft came back empty — try a longer script excerpt.", "info");
        return;
      }
      setDraft(d);
      setChecked(allChecked(d));
    },
    onError: (e: Error) => {
      if (e instanceof ApiError && e.status === 503) setLlmUnconfigured(true);
      else toast(e.message, "error");
    },
  });

  const apply = useMutation({
    mutationFn: (d: BreakdownDraft) =>
      apiPost(`/api/projects/${projectId}/apply-breakdown`, { draft: d }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      toast("Added to your board.", "success");
      navigate(`/p/${projectId}`);
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const updateShotField = (
    si: number,
    ji: number,
    field: "description" | "shot_type" | "duration_s",
    value: string,
  ) => {
    setDraft((d) => {
      if (!d) return d;
      const scenes = d.scenes.map((sc, i) =>
        i !== si
          ? sc
          : {
              ...sc,
              shots: sc.shots.map((sh, j) =>
                j !== ji
                  ? sh
                  : {
                      ...sh,
                      [field]: field === "duration_s" ? parseFloat(value) || sh.duration_s : value,
                    },
              ),
            },
      );
      return { scenes };
    });
  };

  const selectedCount = draft && checked
    ? checked.shots.reduce(
        (n, row, i) => n + (checked.scenes[i] ? row.filter(Boolean).length : 0),
        0,
      )
    : 0;

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
          <h1 className="text-xl font-semibold tracking-tight">Import script</h1>
          <p className="text-xs text-fog">{project?.title ?? "…"}</p>
        </div>
      </div>

      {llmUnconfigured ? (
        <EmptyState
          icon={Wand2}
          title="Script breakdown needs a writing assistant"
          body="Connect a language model in Settings and StoryBored will draft your scene and shot list automatically. You can still build the board by hand."
          action={
            <Link to="/settings">
              <Button variant="primary">Open Settings</Button>
            </Link>
          }
        />
      ) : !draft ? (
        <div className="space-y-4">
          <Field
            label="Script"
            hint="Paste a scene, a few pages, or the whole thing — StoryBored drafts scenes and shots for you to review."
          >
            <TextArea
              rows={16}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={"INT. LIGHTHOUSE — NIGHT\n\nThe keeper climbs the spiral stairs…"}
              className="font-mono text-[13px]"
            />
          </Field>
          <div className="flex items-center justify-end gap-3">
            {breakdown.isPending && (
              <span className="flex items-center gap-2 text-sm text-fog">
                <Spinner size={14} /> Reading your script — this can take a minute…
              </span>
            )}
            <Button
              variant="primary"
              disabled={!text.trim()}
              busy={breakdown.isPending}
              onClick={() => breakdown.mutate()}
            >
              <Sparkles size={14} /> Break it down
            </Button>
          </div>
        </div>
      ) : (
        <div className="space-y-5">
          <div className="flex items-center justify-between">
            <p className="text-sm text-fog">
              This is a draft — untick anything you don't want, edit inline, then add it to
              your board.
            </p>
            <Button
              variant="ghost"
              onClick={() => {
                setDraft(null);
                setChecked(null);
              }}
            >
              Start over
            </Button>
          </div>

          {draft.scenes.map((scene, si) => (
            <section key={si} className="overflow-hidden rounded-xl border border-line bg-ink-900/40">
              <header className="flex items-center gap-3 border-b border-line px-4 py-2.5">
                <input
                  type="checkbox"
                  checked={checked?.scenes[si] ?? true}
                  onChange={(e) =>
                    setChecked((c) =>
                      c
                        ? { ...c, scenes: c.scenes.map((v, i) => (i === si ? e.target.checked : v)) }
                        : c,
                    )
                  }
                  className="h-4 w-4 accent-[#f0b429]"
                />
                <span className="text-sm font-semibold text-paper">
                  {si + 1}. {scene.title}
                </span>
                {scene.slugline && (
                  <span className="truncate text-xs uppercase tracking-wide text-fog">
                    {scene.slugline}
                  </span>
                )}
              </header>
              <table className="w-full text-sm">
                <tbody>
                  {scene.shots.map((shot, ji) => (
                    <tr key={ji} className="border-b border-line/50 last:border-b-0">
                      <td className="w-10 px-4 py-2 align-top">
                        <input
                          type="checkbox"
                          checked={checked?.shots[si]?.[ji] ?? true}
                          onChange={(e) =>
                            setChecked((c) =>
                              c
                                ? {
                                    ...c,
                                    shots: c.shots.map((row, i) =>
                                      i === si
                                        ? row.map((v, j) => (j === ji ? e.target.checked : v))
                                        : row,
                                    ),
                                  }
                                : c,
                            )
                          }
                          className="h-4 w-4 accent-[#f0b429]"
                        />
                      </td>
                      <td className="w-24 px-1 py-2 align-top">
                        <input
                          value={shot.shot_type ?? ""}
                          onChange={(e) => updateShotField(si, ji, "shot_type", e.target.value)}
                          className="w-full rounded border border-transparent bg-transparent px-1.5 py-1 text-xs font-semibold uppercase text-mist focus:border-line focus:outline-none"
                          placeholder="TYPE"
                        />
                      </td>
                      <td className="px-1 py-2 align-top">
                        <textarea
                          value={shot.description}
                          onChange={(e) => updateShotField(si, ji, "description", e.target.value)}
                          rows={2}
                          className="w-full resize-none rounded border border-transparent bg-transparent px-1.5 py-1 text-sm leading-snug text-paper focus:border-line focus:outline-none"
                        />
                        {shot.dialogue && (
                          <p className="px-1.5 text-xs italic text-fog">"{shot.dialogue}"</p>
                        )}
                        {(shot.characters ?? []).length > 0 && (
                          <p className="px-1.5 text-xs text-amber-450/80">
                            {(shot.characters ?? []).map((h) => `@${h}`).join(" ")}
                          </p>
                        )}
                      </td>
                      <td className="w-20 px-2 py-2 align-top">
                        <input
                          type="number"
                          min={0.5}
                          step={0.5}
                          value={shot.duration_s ?? 4}
                          onChange={(e) => updateShotField(si, ji, "duration_s", e.target.value)}
                          className="w-full rounded border border-transparent bg-transparent px-1.5 py-1 text-right text-xs text-fog focus:border-line focus:outline-none"
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          ))}

          <div className="flex items-center justify-end gap-3 pb-8">
            <span className="text-sm text-fog">
              {selectedCount} shot{selectedCount === 1 ? "" : "s"} selected
            </span>
            <Button
              variant="primary"
              disabled={selectedCount === 0}
              busy={apply.isPending}
              onClick={() => checked && apply.mutate(filterDraft(draft, checked))}
            >
              <FileText size={14} /> Add to board
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
