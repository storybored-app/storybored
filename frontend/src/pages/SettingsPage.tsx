import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, FlaskConical, Plus, Save, X } from "lucide-react";
import { apiGet, apiPut } from "../lib/api";
import {
  healthDetail,
  healthOk,
  type Health,
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

function WorkflowRow({ wf }: { wf: WorkflowManifest }) {
  const [open, setOpen] = useState(false);
  const missing = wf.missing_models ?? [];
  const available = wf.available !== false;
  // No missing-model list + an error means the engine itself was unreachable —
  // don't mislabel that as "missing models".
  const unreachable = !available && !!wf.error;
  const expandable = missing.length > 0 || unreachable;
  return (
    <li className="border-b border-line/50 last:border-b-0">
      <button
        className="flex w-full items-center gap-3 px-4 py-3 text-left"
        onClick={() => setOpen((o) => !o)}
      >
        {expandable ? (
          open ? (
            <ChevronDown size={14} className="text-fog" />
          ) : (
            <ChevronRight size={14} className="text-fog" />
          )
        ) : (
          <span className="w-3.5" />
        )}
        <div className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium text-paper">{wf.name}</span>
          {wf.description && (
            <span className="block truncate text-xs text-fog">{wf.description}</span>
          )}
        </div>
        <Badge tone="fog">{wf.kind}</Badge>
        {available ? (
          <Badge tone="green">ready</Badge>
        ) : unreachable ? (
          <Badge tone="amber">engine offline</Badge>
        ) : (
          <Badge tone="red">missing models</Badge>
        )}
      </button>
      {open && expandable && (
        <div className="px-11 pb-3">
          {unreachable ? (
            <p className="text-xs text-status-failed/90">
              Can't reach the image engine — is it running? Set its address above.
            </p>
          ) : (
            <>
              <p className="mb-1 text-xs text-fog">
                This engine needs model files that aren't installed:
              </p>
              <ul className="space-y-0.5">
                {missing.map((m) => (
                  <li key={m} className="font-mono text-xs text-status-failed/90">
                    {m}
                  </li>
                ))}
              </ul>
            </>
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
  const [llmUrl, setLlmUrl] = useState("");
  const [llmKey, setLlmKey] = useState("");
  const [llmKeyDirty, setLlmKeyDirty] = useState(false);
  const [llmModel, setLlmModel] = useState("");
  const [styleLoras, setStyleLoras] = useState<StyleLora[]>([]);
  const [newStyleLora, setNewStyleLora] = useState("");
  const [loaded, setLoaded] = useState(false);

  const keySet = settings?.effective?.llm_api_key_set === true;

  useEffect(() => {
    if (settings && !loaded) {
      setComfyUrl(getSetting(settings, "comfyui_url"));
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

  const saveEngine = useMutation({
    mutationFn: () =>
      apiPut("/api/settings", { values: { comfyui_url: comfyUrl.trim() } }),
    onSuccess: () => {
      toast("Settings saved.", "success");
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["health"] });
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

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
      <div className="mb-7">
        <h1 className="text-xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 text-sm text-fog">What StoryBored is connected to.</p>
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
                className="h-8 w-24"
                title="Strength"
              />
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
        <header className="border-b border-line px-4 py-3">
          <h2 className="text-sm font-semibold">Engines</h2>
          <p className="mt-0.5 text-xs text-fog">
            Rendering styles installed on this system. Add more by dropping a pack into the
            workflows folder.
          </p>
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
              <WorkflowRow key={wf.id} wf={wf} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
