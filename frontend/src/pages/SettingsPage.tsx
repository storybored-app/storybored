import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  ChevronDown,
  ChevronRight,
  Download,
  ExternalLink,
  FileJson,
  FlaskConical,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  Wand2,
  X,
} from "lucide-react";
import { apiDelete, apiGet, apiPost, apiPut } from "../lib/api";
import { ImportWorkflowWizard } from "../components/ImportWorkflowWizard";
import { formatBytes } from "../lib/format";
import {
  healthDetail,
  healthOk,
  type EngineLoraRow,
  type EngineModelSlot,
  type Health,
  type MissingModelInfo,
  type SettingsMap,
  type StyleLora,
  type WorkflowManifest,
} from "../lib/types";
import { Badge, Button, Field, Input, Select } from "../components/ui";
import { ErrorState, Skeleton } from "../components/EmptyState";
import { useToast } from "../lib/toast";

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full ${
        ok ? "bg-status-approved" : "bg-status-failed"
      }`}
    />
  );
}

/** Read an effective setting value (backend keys are lowercase). */
function getSetting(s: SettingsMap | undefined, key: string): string {
  const v = s?.effective?.[key.toLowerCase()];
  return typeof v === "string" ? v : "";
}

/** One entry of the engine_loras setting: node override or appended LoRA. */
type EngineLoraEntry = {
  node?: string;
  lora_name?: string;
  strength?: number;
  enabled?: boolean;
};

/** Parse the engine_loras setting (JSON object keyed by engine id) defensively. */
function parseEngineLorasSetting(raw: string): Record<string, EngineLoraEntry[]> {
  try {
    const data = JSON.parse(raw || "{}");
    return data && typeof data === "object" && !Array.isArray(data) ? data : {};
  } catch {
    return {};
  }
}

/** Parse the engine_models setting (pack id → slot key → filename) defensively. */
function parseEngineModelsSetting(raw: string): Record<string, Record<string, string>> {
  try {
    const data = JSON.parse(raw || "{}");
    return data && typeof data === "object" && !Array.isArray(data) ? data : {};
  } catch {
    return {};
  }
}

/** Parse the style_loras setting (JSON string) defensively. */
function parseStyleLoras(raw: string): StyleLora[] {
  try {
    const data = JSON.parse(raw || "[]");
    if (!Array.isArray(data)) return [];
    return data
      .filter((s) => s && typeof s.lora_name === "string" && s.lora_name)
      .map((s) => ({
        lora_name: s.lora_name,
        strength: typeof s.strength === "number" ? s.strength : 1,
        enabled: s.enabled !== false,
      }));
  } catch {
    return [];
  }
}

/** Diff the edited stack + additions against baked values → setting entries. */
function buildEngineLoraEntries(
  stack: EngineLoraRow[],
  added: StyleLora[],
): EngineLoraEntry[] {
  const entries: EngineLoraEntry[] = [];
  for (const row of stack) {
    const e: EngineLoraEntry = { node: row.node };
    if (row.strength !== row.baked_strength) e.strength = row.strength;
    if (!row.enabled) e.enabled = false;
    if (e.strength !== undefined || e.enabled === false) entries.push(e);
  }
  for (const a of added) {
    entries.push({ lora_name: a.lora_name, strength: a.strength, enabled: a.enabled });
  }
  return entries;
}

function ModelSlotRow({
  slot,
  onPick,
}: {
  slot: EngineModelSlot;
  onPick: (key: string, value: string) => void;
}) {
  // The current value might be missing from the enum (engine offline or file
  // removed) — keep it selectable so the row never shows the wrong model.
  const options = slot.options.includes(slot.value)
    ? slot.options
    : [slot.value, ...slot.options];
  const large = new Set(slot.large_files ?? []);
  return (
    <div className="rounded-md border border-line/60 bg-ink-900 px-3 py-1.5">
      <div className="flex items-center gap-3">
        <span className="shrink-0 text-xs text-fog">{slot.label}</span>
        <Select
          value={slot.value}
          onChange={(e) => onPick(slot.key, e.target.value)}
          className="h-8 min-w-0 flex-1 text-xs"
        >
          {options.map((name) => (
            <option key={name} value={name}>
              {name}
              {name === slot.baked ? " (pack default)" : ""}
              {large.has(name) ? " ⚠ very large" : ""}
            </option>
          ))}
        </Select>
        {slot.value !== slot.baked && (
          <button
            onClick={() => onPick(slot.key, slot.baked)}
            className="shrink-0 text-[10px] uppercase tracking-wide text-amber-450/80 hover:text-amber-450"
            title={`Back to the pack default (${slot.baked})`}
          >
            swapped — reset
          </button>
        )}
      </div>
      {large.has(slot.value) && (
        <p className="mt-1 text-[11px] text-amber-450/90">
          This file is over 24&nbsp;GB — likely more than a 24&nbsp;GB card can hold.
          Prefer a quantized build of the same model if one exists.
        </p>
      )}
    </div>
  );
}

function WorkflowRow({
  wf,
  availableLoras,
  modelsDirSet,
  onMakeDefault,
  onSaveLoras,
  onSaveModels,
  onDownload,
  onRemove,
}: {
  wf: WorkflowManifest;
  availableLoras: string[] | undefined;
  /** True when the shared models folder is configured (enables Download). */
  modelsDirSet: boolean;
  onMakeDefault: (wf: WorkflowManifest) => void;
  onSaveLoras: (packId: string, entries: EngineLoraEntry[]) => void;
  onSaveModels: (packId: string, slots: Record<string, string>) => void;
  onDownload: (packId: string, filenames?: string[]) => void;
  onRemove: (wf: WorkflowManifest) => void;
}) {
  const [open, setOpen] = useState(false);
  const [stack, setStack] = useState<EngineLoraRow[]>(wf.loras ?? []);
  const [added, setAdded] = useState<StyleLora[]>(wf.added_loras ?? []);
  const [newLora, setNewLora] = useState("");

  // Resync the editor whenever the server payload changes (save/reset/refetch).
  const wfKey = JSON.stringify([wf.loras, wf.added_loras]);
  useEffect(() => {
    setStack(wf.loras ?? []);
    setAdded(wf.added_loras ?? []);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wfKey]);

  const missing = wf.missing_models ?? [];
  // catalog-enriched rows; fall back to bare filenames if info is absent
  const missingInfo: MissingModelInfo[] =
    wf.missing_models_info ??
    missing.map((f) => ({ filename: f, folder: "", downloadable: false }));
  const anyDownloadable = missingInfo.some((m) => m.downloadable);
  const missingNodes = wf.missing_nodes ?? [];
  const available = wf.available !== false;
  // No missing-model list + an error means the engine itself was unreachable —
  // don't mislabel that as "missing models".
  const unreachable = !available && !!wf.error;

  const commit = (nextStack: EngineLoraRow[], nextAdded: StyleLora[]) => {
    setStack(nextStack);
    setAdded(nextAdded);
    onSaveLoras(wf.id, buildEngineLoraEntries(nextStack, nextAdded));
  };
  const pickModel = (key: string, value: string) => {
    // Rebuild the pack's whole override map from the slots: value ≠ baked → keep.
    const slots: Record<string, string> = {};
    for (const s of wf.models ?? []) {
      const v = s.key === key ? value : s.value;
      if (v && v !== s.baked) slots[s.key] = v;
    }
    onSaveModels(wf.id, slots);
  };
  const inChain = new Set([
    ...stack.map((r) => r.lora_name),
    ...added.map((a) => a.lora_name),
  ]);

  return (
    <li className="border-b border-line/50 last:border-b-0">
      <button
        className="flex w-full items-center gap-3 px-4 py-3 text-left"
        onClick={() => setOpen((o) => !o)}
      >
        {open ? (
          <ChevronDown size={14} className="text-fog" />
        ) : (
          <ChevronRight size={14} className="text-fog" />
        )}
        <div className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium text-paper">{wf.name}</span>
          {wf.description && (
            <span className="block truncate text-xs text-fog">{wf.description}</span>
          )}
        </div>
        {wf.default && <Badge tone="amber">default</Badge>}
        {(wf.loras_modified || wf.models_modified) && <Badge tone="fog">customized</Badge>}
        {wf.removable && <Badge tone="fog">imported</Badge>}
        <Badge tone="fog">{wf.kind}</Badge>
        {available ? (
          <Badge tone="green">ready</Badge>
        ) : unreachable ? (
          <Badge tone="amber">engine offline</Badge>
        ) : missing.length > 0 ? (
          <Badge tone="red">missing models</Badge>
        ) : (
          <Badge tone="red">missing nodes</Badge>
        )}
      </button>
      {open && (
        <div className="px-11 pb-4">
          {unreachable && (
            <p className="mb-2 text-xs text-status-failed/90">
              Can't reach the image engine — is it running? Set its address above.
            </p>
          )}
          {!unreachable && missingInfo.length > 0 && (
            <div className="mb-3">
              <div className="mb-1.5 flex items-center gap-2">
                <p className="flex-1 text-xs text-fog">
                  This engine needs model files that aren't installed:
                </p>
                {anyDownloadable && modelsDirSet && (
                  <Button size="sm" onClick={() => onDownload(wf.id)}>
                    <Download size={13} /> Download all missing
                  </Button>
                )}
              </div>
              <ul className="space-y-1.5">
                {missingInfo.map((m) => (
                  <li
                    key={m.filename}
                    className="rounded-md border border-line/60 bg-ink-900 px-3 py-2"
                  >
                    <div className="flex items-center gap-2">
                      <span className="min-w-0 flex-1 truncate font-mono text-xs text-status-failed/90">
                        {m.filename}
                      </span>
                      {m.size_bytes != null && (
                        <span className="shrink-0 text-[10px] text-fog">
                          {formatBytes(m.size_bytes)}
                        </span>
                      )}
                      {(m.source || m.page) && (
                        <a
                          href={m.page ?? m.source}
                          target="_blank"
                          rel="noreferrer"
                          className="flex shrink-0 items-center gap-1 text-[10px] uppercase tracking-wide text-amber-450/80 hover:text-amber-450"
                          title="Open the model's page"
                        >
                          <ExternalLink size={11} /> source
                        </a>
                      )}
                      {m.downloadable && modelsDirSet && (
                        <Button
                          size="sm"
                          onClick={() => onDownload(wf.id, [m.filename])}
                          title="Fetch this file into the engine's models folder"
                        >
                          <Download size={13} /> Download
                        </Button>
                      )}
                    </div>
                    <p className="mt-0.5 text-[11px] text-fog">
                      {m.folder ? (
                        <>
                          goes in{" "}
                          <span className="font-mono">models/{m.folder}/</span>
                        </>
                      ) : (
                        "see the engine's docs for the right folder"
                      )}
                      {m.license ? ` · ${m.license}` : ""}
                    </p>
                    {m.notes && !m.downloadable && (
                      <p className="mt-0.5 text-[11px] text-fog/80">{m.notes}</p>
                    )}
                  </li>
                ))}
              </ul>
              {anyDownloadable && !modelsDirSet && (
                <p className="mt-1.5 text-[11px] text-fog">
                  Some of these can be downloaded for you — set the models folder in
                  the engine section above (works when StoryBored runs on the same
                  computer as the engine). Otherwise use the source links and place
                  each file in the folder shown.
                </p>
              )}
            </div>
          )}
          {!unreachable && missingNodes.length > 0 && (
            <div className="mb-2">
              <p className="mb-1 text-xs text-fog">
                Missing custom nodes — install the node pack that provides each of
                these in your rendering engine, then hit Refresh:
              </p>
              <ul className="space-y-0.5">
                {missingNodes.map((n) => (
                  <li key={n} className="font-mono text-xs text-status-failed/90">
                    {n}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {(wf.models ?? []).length > 0 && (
            <div className="mb-3">
              <span className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-fog">
                Model
              </span>
              <div className="space-y-1.5">
                {(wf.models ?? []).map((slot) => (
                  <ModelSlotRow key={slot.key} slot={slot} onPick={pickModel} />
                ))}
              </div>
            </div>
          )}

          <div className="mb-2 flex items-center gap-2">
            <span className="flex-1 text-xs font-medium uppercase tracking-wider text-fog">
              Built-in LoRA stack
            </span>
            {wf.loras_modified && (
              <Button size="sm" onClick={() => onSaveLoras(wf.id, [])}>
                Reset to pack defaults
              </Button>
            )}
            {!wf.default && (
              <Button size="sm" onClick={() => onMakeDefault(wf)}>
                Make default
              </Button>
            )}
          </div>

          {stack.length === 0 && added.length === 0 ? (
            <p className="text-xs text-fog/80">This engine has no LoRAs.</p>
          ) : (
            <div className="space-y-1.5">
              {stack.map((row, i) => (
                <div
                  key={row.node}
                  className="flex items-center gap-3 rounded-md border border-line/60 bg-ink-900 px-3 py-1.5"
                >
                  <input
                    type="checkbox"
                    checked={row.enabled}
                    onChange={(e) =>
                      commit(
                        stack.map((x, j) =>
                          j === i ? { ...x, enabled: e.target.checked } : x,
                        ),
                        added,
                      )
                    }
                    className="h-4 w-4 accent-amber-450"
                    title={row.enabled ? "On — in the render chain" : "Off — skipped"}
                  />
                  <span
                    className={`min-w-0 flex-1 truncate font-mono text-xs ${
                      row.enabled ? "text-paper" : "text-fog"
                    }`}
                  >
                    {row.lora_name}
                  </span>
                  {row.disabled_with_character && (
                    <span className="shrink-0 text-[10px] uppercase tracking-wide text-fog/70">
                      off with @characters
                    </span>
                  )}
                  {/* wrapper constrains width — the Input's own w-full wins over a w-* override */}
                  <div className="w-20 shrink-0">
                    <Input
                      type="number"
                      step={0.05}
                      min={-5}
                      max={5}
                      value={row.strength}
                      onChange={(e) =>
                        setStack(
                          stack.map((x, j) =>
                            j === i
                              ? { ...x, strength: Number(e.target.value) || 0 }
                              : x,
                          ),
                        )
                      }
                      onBlur={() =>
                        onSaveLoras(wf.id, buildEngineLoraEntries(stack, added))
                      }
                      className="h-7 text-xs"
                      title={
                        row.strength !== row.baked_strength
                          ? `Strength (pack default ${row.baked_strength})`
                          : "Strength"
                      }
                    />
                  </div>
                </div>
              ))}
              {added.map((a, i) => (
                <div
                  key={a.lora_name}
                  className="flex items-center gap-3 rounded-md border border-amber-450/25 bg-ink-900 px-3 py-1.5"
                >
                  <input
                    type="checkbox"
                    checked={a.enabled}
                    onChange={(e) =>
                      commit(
                        stack,
                        added.map((x, j) =>
                          j === i ? { ...x, enabled: e.target.checked } : x,
                        ),
                      )
                    }
                    className="h-4 w-4 accent-amber-450"
                  />
                  <span
                    className={`min-w-0 flex-1 truncate font-mono text-xs ${
                      a.enabled ? "text-paper" : "text-fog"
                    }`}
                  >
                    {a.lora_name}
                  </span>
                  <span className="shrink-0 text-[10px] uppercase tracking-wide text-amber-450/80">
                    added
                  </span>
                  <div className="w-20 shrink-0">
                    <Input
                      type="number"
                      step={0.05}
                      min={-5}
                      max={5}
                      value={a.strength}
                      onChange={(e) =>
                        setAdded(
                          added.map((x, j) =>
                            j === i
                              ? { ...x, strength: Number(e.target.value) || 0 }
                              : x,
                          ),
                        )
                      }
                      onBlur={() =>
                        onSaveLoras(wf.id, buildEngineLoraEntries(stack, added))
                      }
                      className="h-7 text-xs"
                      title="Strength"
                    />
                  </div>
                  <button
                    onClick={() =>
                      commit(
                        stack,
                        added.filter((_, j) => j !== i),
                      )
                    }
                    className="text-fog transition-colors hover:text-status-failed"
                    title="Remove"
                  >
                    <X size={14} />
                  </button>
                </div>
              ))}
            </div>
          )}

          {(wf.supports_loras ?? wf.supports_characters) && (
            <div className="mt-2 flex items-center gap-2">
              <Select
                value={newLora}
                onChange={(e) => setNewLora(e.target.value)}
                className="h-8 text-xs"
              >
                <option value="">
                  {availableLoras
                    ? availableLoras.length
                      ? "Add a LoRA to this engine…"
                      : "No LoRAs found on the engine"
                    : "Loading LoRA list…"}
                </option>
                {(availableLoras ?? [])
                  .filter((name) => !inChain.has(name))
                  .map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
              </Select>
              <Button
                size="sm"
                disabled={!newLora}
                onClick={() => {
                  if (!newLora) return;
                  commit(stack, [
                    ...added,
                    { lora_name: newLora, strength: 1.0, enabled: true },
                  ]);
                  setNewLora("");
                }}
              >
                <Plus size={14} /> Add
              </Button>
            </div>
          )}

          {wf.removable && (
            <div className="mt-3 flex justify-end border-t border-line/50 pt-3">
              <Button
                variant="danger"
                size="sm"
                onClick={() => onRemove(wf)}
                title="Delete this imported engine's files — shots already rendered with it keep their images"
              >
                <Trash2 size={13} /> Remove this engine
              </Button>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

export function SettingsPage() {
  const qc = useQueryClient();
  const { toast } = useToast();

  const {
    data: health,
    isError: healthError,
    refetch: refetchHealth,
    isFetching: healthFetching,
  } = useQuery<Health>({
    queryKey: ["health"],
    queryFn: () => apiGet<Health>("/api/health"),
    refetchInterval: 30_000,
    retry: 1,
  });

  const { data: settings, isLoading: settingsLoading } = useQuery<SettingsMap>({
    queryKey: ["settings"],
    queryFn: () => apiGet<SettingsMap>("/api/settings"),
    retry: 1,
  });

  const { data: workflows, isError: wfError } = useQuery<WorkflowManifest[]>({
    queryKey: ["workflows"],
    queryFn: () => apiGet<WorkflowManifest[]>("/api/workflows"),
    retry: 1,
  });

  const [comfyUrl, setComfyUrl] = useState("");
  const [lorasDir, setLorasDir] = useState("");
  const [trainerDir, setTrainerDir] = useState("");
  const [modelsDir, setModelsDir] = useState("");
  const [llmUrl, setLlmUrl] = useState("");
  const [llmKey, setLlmKey] = useState("");
  const [llmKeyDirty, setLlmKeyDirty] = useState(false);
  const [llmModel, setLlmModel] = useState("");
  const [styleLoras, setStyleLoras] = useState<StyleLora[]>([]);
  const [newStyleLora, setNewStyleLora] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [importOpen, setImportOpen] = useState(false);

  const keySet = settings?.effective?.llm_api_key_set === true;

  useEffect(() => {
    if (settings && !loaded) {
      setComfyUrl(getSetting(settings, "comfyui_url"));
      setLorasDir(getSetting(settings, "comfy_loras_dir"));
      setTrainerDir(getSetting(settings, "lora_factory_dir"));
      setModelsDir(getSetting(settings, "comfy_models_dir"));
      setLlmUrl(getSetting(settings, "llm_base_url"));
      setLlmModel(getSetting(settings, "llm_model"));
      setStyleLoras(parseStyleLoras(getSetting(settings, "style_loras")));
      setLoaded(true);
    }
  }, [settings, loaded]);

  const { data: availableLoras } = useQuery<string[]>({
    queryKey: ["available-loras"],
    queryFn: () => apiGet<string[]>("/api/characters/available-loras"),
    retry: 1,
  });

  const saveStyleLoras = useMutation({
    mutationFn: (next: StyleLora[]) =>
      apiPut("/api/settings", {
        values: { style_loras: next.length ? JSON.stringify(next) : "" },
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
    onError: (e: Error) => toast(e.message, "error"),
  });

  // Toggles / add / remove persist immediately; strength persists on blur.
  const commitStyleLoras = (next: StyleLora[]) => {
    setStyleLoras(next);
    saveStyleLoras.mutate(next);
  };

  const addStyleLora = () => {
    if (!newStyleLora) return;
    commitStyleLoras([
      ...styleLoras,
      { lora_name: newStyleLora, strength: 1.0, enabled: true },
    ]);
    setNewStyleLora("");
  };

  const saveEngineLorasMut = useMutation({
    mutationFn: (next: Record<string, EngineLoraEntry[]>) =>
      apiPut("/api/settings", {
        values: { engine_loras: Object.keys(next).length ? JSON.stringify(next) : "" },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (e: Error) => {
      toast(e.message, "error");
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
  });

  // Merge one engine's entries into the whole setting (other engines untouched).
  const saveEngineLoras = (packId: string, entries: EngineLoraEntry[]) => {
    const next = parseEngineLorasSetting(getSetting(settings, "engine_loras"));
    if (entries.length) next[packId] = entries;
    else delete next[packId];
    saveEngineLorasMut.mutate(next);
  };

  const saveEngineModelsMut = useMutation({
    mutationFn: (next: Record<string, Record<string, string>>) =>
      apiPut("/api/settings", {
        values: { engine_models: Object.keys(next).length ? JSON.stringify(next) : "" },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (e: Error) => {
      toast(e.message, "error");
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
  });

  // Merge one engine's model choices into the whole setting.
  const saveEngineModels = (packId: string, slots: Record<string, string>) => {
    const next = parseEngineModelsSetting(getSetting(settings, "engine_models"));
    if (Object.keys(slots).length) next[packId] = slots;
    else delete next[packId];
    saveEngineModelsMut.mutate(next);
  };

  // Re-check availability NOW: the server drops its 60s engine cache first, so
  // models/nodes installed a moment ago show up without waiting.
  const refreshWorkflows = useMutation({
    mutationFn: () => apiGet<WorkflowManifest[]>("/api/workflows?refresh=true"),
    onSuccess: (data) => qc.setQueryData(["workflows"], data),
    onError: (e: Error) => toast(e.message, "error"),
  });

  // Fetch missing model files into the shared models folder (io-lane jobs —
  // downloads never block renders; progress shows in the job tray).
  const downloadModels = useMutation({
    mutationFn: ({ packId, filenames }: { packId: string; filenames?: string[] }) =>
      apiPost<{ queued: number; skipped: string[] }>(
        `/api/workflows/${packId}/download-models`,
        filenames ? { filenames } : {},
      ),
    onSuccess: (data) => {
      if (data.queued > 0) {
        toast(
          `Downloading ${data.queued} model file${data.queued === 1 ? "" : "s"} — watch the job tray.`,
          "success",
        );
      } else if (data.skipped.length > 0) {
        toast("Those files have no verified download — use the notes to find them.", "error");
      } else {
        toast("Nothing to download — already fetching or nothing missing.", "success");
      }
      qc.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  // Delete a user-imported pack's folder (shipped packs have no Remove button
  // and the server refuses them anyway).
  const removeWorkflow = useMutation({
    mutationFn: (wf: WorkflowManifest) => apiDelete(`/api/workflows/${wf.id}`),
    onSuccess: (_data, wf) => {
      toast(`${wf.name} removed.`, "success");
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const makeDefault = useMutation({
    mutationFn: (wf: WorkflowManifest) =>
      apiPut("/api/settings", {
        values: {
          [wf.kind === "video" ? "default_video_workflow" : "default_image_workflow"]:
            wf.id,
        },
      }),
    onSuccess: (_data, wf) => {
      toast(`${wf.name} is now the default ${wf.kind} engine.`, "success");
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const saveEngine = useMutation({
    mutationFn: () =>
      apiPut("/api/settings", {
        values: {
          comfyui_url: comfyUrl.trim(),
          comfy_loras_dir: lorasDir.trim(),
          comfy_models_dir: modelsDir.trim(),
        },
      }),
    onSuccess: () => {
      toast("Settings saved.", "success");
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["health"] });
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const saveTrainer = useMutation({
    mutationFn: () =>
      apiPut("/api/settings", { values: { lora_factory_dir: trainerDir.trim() } }),
    onSuccess: () => {
      toast("Settings saved.", "success");
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["health"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const testTrainer = async () => {
    const res = await refetchHealth();
    const part = res.data?.trainer;
    if (healthOk(part)) toast("Trainer folder found.", "success");
    else toast(`Character trainer: ${healthDetail(part)}`, "error");
  };

  const testEngine = async () => {
    const res = await refetchHealth();
    const part = res.data?.comfy;
    if (healthOk(part)) toast("Image engine is reachable.", "success");
    else toast(`Image engine: ${healthDetail(part)}`, "error");
  };

  const saveLlm = useMutation({
    mutationFn: () => {
      const values: Record<string, string> = {
        llm_base_url: llmUrl.trim(),
        llm_model: llmModel.trim(),
      };
      // The key is never echoed back — only send it when the user typed one.
      if (llmKeyDirty) values.llm_api_key = llmKey.trim();
      return apiPut("/api/settings", { values });
    },
    onSuccess: () => {
      toast("Settings saved.", "success");
      setLlmKeyDirty(false);
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["health"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const testLlm = async () => {
    const res = await refetchHealth();
    const part = res.data?.llm;
    if (healthOk(part)) toast("Writing assistant is reachable.", "success");
    else toast(`Writing assistant: ${healthDetail(part)}`, "error");
  };

  const rows: { key: string; label: string; hint: string }[] = [
    { key: "comfy", label: "Image & video engine", hint: "Renders your stills and clips" },
    { key: "llm", label: "Writing assistant", hint: "Drafts shot lists from scripts" },
    { key: "trainer", label: "Character trainer", hint: "Teaches the engine new faces" },
    { key: "ffmpeg", label: "Video assembler", hint: "Cuts the final animatic" },
  ];

  return (
    <div className="mx-auto max-w-3xl">
      <div className="mb-7 flex items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Settings</h1>
          <p className="mt-1 text-sm text-fog">What StoryBored is connected to.</p>
        </div>
        <Link
          to="/setup"
          className="flex items-center gap-1.5 text-sm font-medium text-amber-450 hover:text-amber-350"
        >
          <Wand2 size={14} /> Setup wizard
        </Link>
      </div>

      {healthError && (
        <div className="mb-6">
          <ErrorState onRetry={() => refetchHealth()} />
        </div>
      )}

      {/* status */}
      <section className="mb-6 overflow-hidden rounded-xl border border-line bg-ink-900/40">
        <header className="border-b border-line px-4 py-3">
          <h2 className="text-sm font-semibold">System status</h2>
        </header>
        <ul>
          {rows.map((r) => {
            const part = health?.[r.key];
            const ok = healthOk(part);
            return (
              <li
                key={r.key}
                className="flex items-center gap-3 border-b border-line/50 px-4 py-3 last:border-b-0"
              >
                <StatusDot ok={ok} />
                <div className="min-w-0 flex-1">
                  <span className="block text-sm font-medium text-paper">{r.label}</span>
                  <span className="block text-xs text-fog">{r.hint}</span>
                </div>
                <span className={`max-w-48 truncate text-xs ${ok ? "text-status-approved" : "text-fog"}`}>
                  {health ? healthDetail(part) : "checking…"}
                </span>
              </li>
            );
          })}
        </ul>
      </section>

      {/* image engine config */}
      <section className="mb-6 rounded-xl border border-line bg-ink-900/40 p-5">
        <h2 className="text-sm font-semibold">Image & video engine</h2>
        <p className="mt-1 text-xs text-fog">
          Where your rendering engine is running. Change this if it's on another
          computer or a different port.
        </p>
        {settingsLoading ? (
          <div className="mt-4 space-y-3">
            <Skeleton className="h-9" />
          </div>
        ) : (
          <div className="mt-4 space-y-3.5">
            <Field label="Image engine address">
              <Input
                value={comfyUrl}
                onChange={(e) => setComfyUrl(e.target.value)}
                placeholder="http://127.0.0.1:8188"
              />
            </Field>
            <Field
              label="LoRA folder"
              hint="Where uploaded character files are copied so the engine can load them (the engine's models/loras folder). Optional — needed only for file uploads."
            >
              <Input
                value={lorasDir}
                onChange={(e) => setLorasDir(e.target.value)}
                placeholder="/path/to/ComfyUI/models/loras"
              />
            </Field>
            <Field
              label="Engine models folder (optional)"
              hint="The engine's models directory, if it's on this computer — lets StoryBored download missing model files for you and warn about oversized ones."
            >
              <Input
                value={modelsDir}
                onChange={(e) => setModelsDir(e.target.value)}
                placeholder="~/ComfyUI/models"
              />
            </Field>
            <div className="flex justify-end gap-2">
              <Button onClick={testEngine} busy={healthFetching}>
                <FlaskConical size={14} /> Test
              </Button>
              <Button
                variant="primary"
                onClick={() => saveEngine.mutate()}
                busy={saveEngine.isPending}
              >
                <Save size={14} /> Save
              </Button>
            </div>
          </div>
        )}
      </section>

      {/* LLM config */}
      <section className="mb-6 rounded-xl border border-line bg-ink-900/40 p-5">
        <h2 className="text-sm font-semibold">Writing assistant</h2>
        <p className="mt-1 text-xs text-fog">
          Any OpenAI-compatible service works — local or hosted. Used only for script
          breakdowns.
        </p>
        {settingsLoading ? (
          <div className="mt-4 space-y-3">
            <Skeleton className="h-9" />
            <Skeleton className="h-9" />
          </div>
        ) : (
          <div className="mt-4 space-y-3.5">
            <Field label="Service URL">
              <Input
                value={llmUrl}
                onChange={(e) => setLlmUrl(e.target.value)}
                placeholder="http://127.0.0.1:11434/v1"
              />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="API key" hint="Leave blank for local services.">
                <Input
                  type="password"
                  value={llmKey}
                  onChange={(e) => {
                    setLlmKey(e.target.value);
                    setLlmKeyDirty(true);
                  }}
                  placeholder={keySet ? "•••••••• (saved)" : "sk-…"}
                />
              </Field>
              <Field label="Model">
                <Input
                  value={llmModel}
                  onChange={(e) => setLlmModel(e.target.value)}
                  placeholder="model name"
                />
              </Field>
            </div>
            <div className="flex justify-end gap-2">
              <Button onClick={testLlm} busy={healthFetching}>
                <FlaskConical size={14} /> Test
              </Button>
              <Button variant="primary" onClick={() => saveLlm.mutate()} busy={saveLlm.isPending}>
                <Save size={14} /> Save
              </Button>
            </div>
          </div>
        )}
      </section>

      {/* trainer config */}
      <section className="mb-6 rounded-xl border border-line bg-ink-900/40 p-5">
        <h2 className="text-sm font-semibold">Character trainer</h2>
        <p className="mt-1 text-xs text-fog">
          Optional — enables training characters from photos. Point this at a
          lora-factory-style checkout on this machine (see docs/TRAINING.md).
        </p>
        {settingsLoading ? (
          <div className="mt-4 space-y-3">
            <Skeleton className="h-9" />
          </div>
        ) : (
          <div className="mt-4 space-y-3.5">
            <Field label="Trainer folder">
              <Input
                value={trainerDir}
                onChange={(e) => setTrainerDir(e.target.value)}
                placeholder="/path/to/lora-factory"
              />
            </Field>
            <div className="flex justify-end gap-2">
              <Button onClick={testTrainer} busy={healthFetching}>
                <FlaskConical size={14} /> Test
              </Button>
              <Button
                variant="primary"
                onClick={() => saveTrainer.mutate()}
                busy={saveTrainer.isPending}
              >
                <Save size={14} /> Save
              </Button>
            </div>
          </div>
        )}
      </section>

      {/* style LoRAs */}
      <section className="mb-6 rounded-xl border border-line bg-ink-900/40 p-5">
        <h2 className="text-sm font-semibold">Style LoRAs</h2>
        <p className="mt-1 text-xs text-fog">
          Extra look/style LoRAs layered into every still you render. Flip them on and
          off any time — characters keep the final say on identity.
        </p>
        <div className="mt-4 space-y-2">
          {styleLoras.length === 0 && (
            <p className="text-xs text-fog/80">
              None yet — pick a LoRA below to try a look on your next render.
            </p>
          )}
          {styleLoras.map((s, i) => (
            <div
              key={s.lora_name}
              className="flex items-center gap-3 rounded-md border border-line/60 bg-ink-900 px-3 py-2"
            >
              <input
                type="checkbox"
                checked={s.enabled}
                onChange={(e) =>
                  commitStyleLoras(
                    styleLoras.map((x, j) =>
                      j === i ? { ...x, enabled: e.target.checked } : x,
                    ),
                  )
                }
                className="h-4 w-4 accent-amber-450"
                title={s.enabled ? "On — applied to renders" : "Off"}
              />
              <span
                className={`min-w-0 flex-1 truncate font-mono text-xs ${
                  s.enabled ? "text-paper" : "text-fog"
                }`}
              >
                {s.lora_name}
              </span>
              <div className="w-24 shrink-0">
                <Input
                  type="number"
                  step={0.05}
                  min={-5}
                  max={5}
                  value={s.strength}
                  onChange={(e) =>
                    setStyleLoras(
                      styleLoras.map((x, j) =>
                        j === i ? { ...x, strength: Number(e.target.value) || 0 } : x,
                      ),
                    )
                  }
                  onBlur={() => saveStyleLoras.mutate(styleLoras)}
                  className="h-8"
                  title="Strength"
                />
              </div>
              <button
                onClick={() => commitStyleLoras(styleLoras.filter((_, j) => j !== i))}
                className="text-fog transition-colors hover:text-status-failed"
                title="Remove"
              >
                <X size={14} />
              </button>
            </div>
          ))}
          <div className="flex items-center gap-2 pt-1">
            <Select value={newStyleLora} onChange={(e) => setNewStyleLora(e.target.value)}>
              <option value="">
                {availableLoras
                  ? availableLoras.length
                    ? "Choose a LoRA…"
                    : "No LoRAs found on the engine"
                  : "Loading LoRA list…"}
              </option>
              {(availableLoras ?? [])
                .filter((name) => !styleLoras.some((s) => s.lora_name === name))
                .map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
            </Select>
            <Button onClick={addStyleLora} disabled={!newStyleLora}>
              <Plus size={14} /> Add
            </Button>
          </div>
        </div>
      </section>

      {/* workflow packs */}
      <section className="overflow-hidden rounded-xl border border-line bg-ink-900/40">
        <header className="flex items-start gap-3 border-b border-line px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 className="text-sm font-semibold">Engines</h2>
            <p className="mt-0.5 text-xs text-fog">
              Rendering styles installed on this system — the <em>default</em> one renders
              shots that don't pick an engine. Expand a row to swap its base model and
              edit the LoRAs it runs with. Add more engines by dropping a pack into the
              workflows folder.
            </p>
          </div>
          <Button
            size="sm"
            onClick={() => setImportOpen(true)}
            title="Turn a ComfyUI workflow export into a new engine"
          >
            <FileJson size={13} /> Import workflow
          </Button>
          <Button
            size="sm"
            onClick={() => refreshWorkflows.mutate()}
            busy={refreshWorkflows.isPending}
            title="Re-check which models and nodes the engine has right now"
          >
            <RefreshCw size={13} /> Refresh
          </Button>
        </header>
        {wfError ? (
          <p className="px-4 py-6 text-center text-sm text-fog">
            Couldn't load the engine list — is the server running?
          </p>
        ) : !workflows ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-10" />
            <Skeleton className="h-10" />
          </div>
        ) : workflows.length === 0 ? (
          <p className="px-4 py-6 text-center text-sm text-fog">No engine packs found.</p>
        ) : (
          <ul>
            {workflows.map((wf) => (
              <WorkflowRow
                key={wf.id}
                wf={wf}
                availableLoras={availableLoras}
                modelsDirSet={!!getSetting(settings, "comfy_models_dir")}
                onMakeDefault={(w) => makeDefault.mutate(w)}
                onSaveLoras={saveEngineLoras}
                onSaveModels={saveEngineModels}
                onDownload={(packId, filenames) =>
                  downloadModels.mutate({ packId, filenames })
                }
                onRemove={(w) => {
                  if (window.confirm(`Remove the "${w.name}" engine?`)) {
                    removeWorkflow.mutate(w);
                  }
                }}
              />
            ))}
          </ul>
        )}
      </section>

      {importOpen && <ImportWorkflowWizard onClose={() => setImportOpen(false)} />}
    </div>
  );
}
