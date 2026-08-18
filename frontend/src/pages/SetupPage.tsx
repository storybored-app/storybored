import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronDown,
  ChevronRight,
  Cpu,
  Download,
  ExternalLink,
  FlaskConical,
  Layout as LayoutIcon,
  MonitorPlay,
  PenLine,
  Server,
  Sparkles,
  UserRound,
} from "lucide-react";
import { apiGet, apiPost, apiPut } from "../lib/api";
import { Badge, Button, Field, Input, Select, Spinner } from "../components/ui";
import { formatBytes } from "../lib/format";
import { useToast } from "../lib/toast";
import type {
  CapabilityTier,
  RecommendedPack,
  SettingsMap,
  SetupProbe,
} from "../lib/types";

/* ---------------------------------------------------------------- helpers */

type Path = "have" | "need" | "none";
type StepId = "welcome" | "engine" | "assistant" | "trainer" | "summary";

const STEPS: Record<Path, StepId[]> = {
  have: ["welcome", "engine", "assistant", "trainer", "summary"],
  need: ["welcome", "engine", "assistant", "trainer", "summary"],
  // no GPU: no engine to point at, and training needs one too
  none: ["welcome", "assistant", "summary"],
};

const TIER_COPY: Record<CapabilityTier, { label: string; blurb: string }> = {
  studio: {
    label: "studio-class",
    blurb:
      "This card fits everything: the biggest stills engine, 14B video, and character training.",
  },
  "stills-hd": {
    label: "stills-HD-class",
    blurb:
      "Great for stills — the default engine fits comfortably — plus lightweight video. 14B video and character training want a 24 GB-class card.",
  },
  stills: {
    label: "stills-class",
    blurb:
      "Good for fast photoreal stills and lightweight video clips. The heavier engines want 16–24 GB.",
  },
  "stills-lite": {
    label: "stills-lite-class",
    blurb:
      "Fits a fast, compact stills engine (with some offloading at the low end). Video rendering and training want bigger cards.",
  },
  board: {
    label: "boards only",
    blurb:
      "The engine didn't report a GPU with enough memory for the shipped engines. Boards, script tools and animatic export still work — rendering likely won't.",
  },
};

function probeUrl(params: Record<string, string>): string {
  const qs = new URLSearchParams(params).toString();
  return qs ? `/api/setup/probe?${qs}` : "/api/setup/probe";
}

/** Read an effective setting value. */
function getSetting(s: SettingsMap | undefined, key: string): string {
  const v = s?.effective?.[key];
  return typeof v === "string" ? v : "";
}

function StatusLine({
  status,
  okText,
}: {
  status: string;
  okText: string;
}) {
  if (status === "ok")
    return (
      <p className="flex items-center gap-1.5 text-sm text-status-approved">
        <Check size={14} /> {okText}
      </p>
    );
  const copy: Record<string, string> = {
    unreachable: "Nothing answered at that address. Is it running? Right port?",
    unrecognized:
      "Something answered, but it doesn't look like the right service — double-check the address and port.",
    unauthorized:
      "It wants an API key. Enter one below — it's used for tests once saved, so hosted services may show this until you finish setup.",
    error: "It answered with a server error — check its logs.",
    not_configured: "No address given.",
    missing: "That folder doesn't exist on this machine.",
  };
  return (
    <p className="text-sm text-status-failed">
      {copy[status] ?? `Status: ${status}`}
    </p>
  );
}

function CodeLine({ children }: { children: string }) {
  return (
    <code className="block select-all overflow-x-auto whitespace-nowrap rounded-md border border-line/60 bg-ink-950 px-2.5 py-1.5 font-mono text-[11px] text-paper">
      {children}
    </code>
  );
}

/** Collapsible "I don't have an LLM yet" panel: verified Ollama setup.
 *  `suggested` is the tier-matched model tag from the engine probe (falls
 *  back to the 9b default when the engine wasn't probed). */
function OllamaGuide({
  suggested,
  onUseDefaults,
}: {
  suggested: string;
  onUseDefaults: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg border border-line/60 bg-ink-900">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1.5 px-4 py-2.5 text-left text-xs font-medium text-paper hover:text-amber-350"
      >
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        I don't have an LLM yet — set one up
      </button>
      {open && (
        <div className="space-y-3 border-t border-line/60 px-4 py-3 text-xs leading-relaxed text-mist">
          <p className="text-fog">
            The easiest path is{" "}
            <a
              href="https://ollama.com/download"
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-0.5 text-amber-450 hover:text-amber-350"
            >
              Ollama <ExternalLink size={11} />
            </a>
            , a free local LLM runner. Install it, pull the model, done:
          </p>
          <div className="space-y-1.5">
            <p className="font-medium text-paper">Linux</p>
            <CodeLine>curl -fsSL https://ollama.com/install.sh | sh</CodeLine>
            <p className="font-medium text-paper">macOS / Windows</p>
            <p className="text-fog">
              Download and run the installer from{" "}
              <a
                href="https://ollama.com/download"
                target="_blank"
                rel="noreferrer"
                className="text-amber-450 hover:text-amber-350"
              >
                ollama.com/download
              </a>
              .
            </p>
            <p className="font-medium text-paper">Then, on any platform</p>
            <CodeLine>{`ollama pull ${suggested}`}</CodeLine>
          </div>
          <p className="text-fog">
            That's the model suggested for your hardware. Ollama then answers
            at <code>http://127.0.0.1:11434/v1</code> —{" "}
            <button
              onClick={onUseDefaults}
              className="font-medium text-amber-450 hover:text-amber-350"
            >
              fill those values in below
            </button>{" "}
            and hit Test.
          </p>
          <p className="text-fog">
            Honest resource note: qwen3.5:9b (the default) is a 6.6 GB
            download and needs roughly that much free RAM or VRAM; qwen3.5:4b
            (3.4 GB) suits small GPUs and CPU-only boxes, and qwen3.5:35b-a3b
            (24 GB) wants 32 GB+ of VRAM. CPU works everywhere, just slower.
            Ollama unloads idle models after a few minutes, so it can share a
            GPU with the render engine — and setting{" "}
            <code>llm_keep_alive</code> to <code>0</code> in Settings frees
            the VRAM immediately after each call on a shared-GPU box. If
            you'd rather not run one at all, any OpenAI-compatible hosted API
            works instead: paste its base URL, model name, and API key.
          </p>
        </div>
      )}
    </div>
  );
}

/** One tier-recommended engine: name, size of what's missing, one-click fetch. */
function RecommendedRow({
  rec,
  modelsDirSet,
  busy,
  onDownload,
}: {
  rec: RecommendedPack;
  modelsDirSet: boolean;
  busy: boolean;
  onDownload: (packId: string) => void;
}) {
  return (
    <div className="rounded-md border border-line/60 bg-ink-950 px-3 py-2">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-paper">
          {rec.name}
        </span>
        <Badge tone="fog">{rec.kind}</Badge>
        {rec.available ? (
          <Badge tone="green">installed</Badge>
        ) : (
          <>
            {rec.download_bytes > 0 && (
              <span className="shrink-0 text-xs text-fog">
                {formatBytes(rec.download_bytes)} to download
              </span>
            )}
            {rec.downloadable && modelsDirSet && (
              <Button size="sm" busy={busy} onClick={() => onDownload(rec.id)}>
                <Download size={13} /> Download missing
              </Button>
            )}
          </>
        )}
      </div>
      {rec.license_note && (
        <p className="mt-1 text-[11px] leading-relaxed text-amber-450/90">
          {rec.license_note}
        </p>
      )}
    </div>
  );
}

function StepCard({ children }: { children: React.ReactNode }) {
  return (
    <section className="sb-fade-in rounded-xl border border-line bg-ink-900/40 p-6">
      {children}
    </section>
  );
}

/* ------------------------------------------------------------------ page */

export function SetupPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { toast } = useToast();

  const { data: settings } = useQuery<SettingsMap>({
    queryKey: ["settings"],
    queryFn: () => apiGet<SettingsMap>("/api/settings"),
    retry: 1,
  });

  const [path, setPath] = useState<Path>("have");
  const [stepIdx, setStepIdx] = useState(0);
  const steps = STEPS[path];
  const step = steps[Math.min(stepIdx, steps.length - 1)];

  // candidate values (written only on Finish)
  const [comfyUrl, setComfyUrl] = useState("");
  const [llmUrl, setLlmUrl] = useState("");
  const [llmKey, setLlmKey] = useState("");
  const [llmModel, setLlmModel] = useState("");
  const [trainerDir, setTrainerDir] = useState("");
  const [prefilled, setPrefilled] = useState(false);

  useEffect(() => {
    if (settings && !prefilled) {
      setComfyUrl(getSetting(settings, "comfyui_url") || "http://127.0.0.1:8188");
      setLlmUrl(getSetting(settings, "llm_base_url"));
      setLlmModel(getSetting(settings, "llm_model"));
      setTrainerDir(getSetting(settings, "lora_factory_dir"));
      setPrefilled(true);
    }
  }, [settings, prefilled]);

  // last successful/failed probe per step (Test buttons)
  const [engineProbe, setEngineProbe] = useState<SetupProbe | null>(null);
  const [llmProbe, setLlmProbe] = useState<SetupProbe | null>(null);
  const [trainerProbe, setTrainerProbe] = useState<SetupProbe | null>(null);

  const testEngine = useMutation({
    mutationFn: () => apiGet<SetupProbe>(probeUrl({ comfy_url: comfyUrl.trim() })),
    onSuccess: setEngineProbe,
    onError: (e: Error) => toast(e.message, "error"),
  });
  const testLlm = useMutation({
    mutationFn: () => apiGet<SetupProbe>(probeUrl({ llm_url: llmUrl.trim() })),
    onSuccess: setLlmProbe,
    onError: (e: Error) => toast(e.message, "error"),
  });
  const testTrainer = useMutation({
    mutationFn: () => apiGet<SetupProbe>(probeUrl({ trainer_dir: trainerDir.trim() })),
    onSuccess: setTrainerProbe,
    onError: (e: Error) => toast(e.message, "error"),
  });

  const finish = useMutation({
    mutationFn: () => {
      // write only what this wizard actually configured; empty = leave alone
      const values: Record<string, string> = { setup_complete: "1" };
      if (path !== "none" && comfyUrl.trim()) values.comfyui_url = comfyUrl.trim();
      if (llmUrl.trim()) {
        values.llm_base_url = llmUrl.trim();
        if (llmModel.trim()) values.llm_model = llmModel.trim();
        if (llmKey.trim()) values.llm_api_key = llmKey.trim();
      }
      if (path !== "none" && trainerDir.trim())
        values.lora_factory_dir = trainerDir.trim();
      // Preselect the tier-recommended engines as the defaults — only when
      // the user hasn't already chosen defaults, and always changeable in
      // Settings (a recommendation preselects, never locks).
      const rec = engineProbe?.recommended;
      if (rec?.image && !getSetting(settings, "default_image_workflow"))
        values.default_image_workflow = rec.image.id;
      if (rec?.video && !getSetting(settings, "default_video_workflow"))
        values.default_video_workflow = rec.video.id;
      return apiPut("/api/settings", { values });
    },
    onSuccess: () => {
      qc.invalidateQueries();
      toast("You're set up — make something.", "success");
      navigate("/");
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const next = () => setStepIdx((i) => Math.min(i + 1, steps.length - 1));
  const back = () => setStepIdx((i) => Math.max(i - 1, 0));

  // one-click fetch of a recommended engine's missing files (io-lane jobs)
  const downloadModels = useMutation({
    mutationFn: (packId: string) =>
      apiPost<{ queued: number; skipped: string[] }>(
        `/api/workflows/${packId}/download-models`,
        {},
      ),
    onSuccess: (data) => {
      if (data.queued > 0) {
        toast(
          `Downloading ${data.queued} model file${data.queued === 1 ? "" : "s"} — watch the job tray, then hit Test again.`,
          "success",
        );
      } else {
        toast("Nothing to download — already fetching or nothing missing.", "success");
      }
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const engineOk = engineProbe?.comfy.status === "ok";
  const tier: CapabilityTier = engineProbe?.comfy.tier ?? "board";
  const recommended = engineProbe?.recommended ?? null;
  const recommendedPacks = recommended
    ? [recommended.image, recommended.video].filter(
        (r): r is RecommendedPack => r !== null,
      )
    : [];
  const modelsDirSet = !!getSetting(settings, "comfy_models_dir");
  const llmOk = llmProbe?.llm.status === "ok";
  const llmModels = llmProbe?.llm.models ?? [];
  const trainerOk = trainerProbe?.trainer.status === "ok";

  return (
    <div className="mx-auto max-w-2xl">
      <div className="mb-7">
        <h1 className="text-xl font-semibold tracking-tight">Set up StoryBored</h1>
        <p className="mt-1 text-sm text-fog">
          A couple of minutes, all optional, all changeable later in{" "}
          <Link to="/settings" className="text-amber-450 hover:text-amber-350">
            Settings
          </Link>
          .
        </p>
      </div>

      {/* progress dots */}
      <div className="mb-6 flex items-center gap-2">
        {steps.map((s, i) => (
          <span
            key={s}
            className={`h-1.5 rounded-full transition-all ${
              i === stepIdx
                ? "w-8 bg-amber-450"
                : i < stepIdx
                  ? "w-4 bg-amber-450/50"
                  : "w-4 bg-ink-700"
            }`}
          />
        ))}
      </div>

      {/* ------------------------------------------------ step: welcome */}
      {step === "welcome" && (
        <StepCard>
          <h2 className="text-sm font-semibold">Where will your images come from?</h2>
          <p className="mt-1 text-xs text-fog">
            StoryBored renders through an engine (ComfyUI) that can live on this
            machine or any machine you can reach.
          </p>
          <div className="mt-4 space-y-2">
            {(
              [
                {
                  id: "have" as Path,
                  icon: <Server size={16} />,
                  title: "I have an engine running",
                  blurb: "Point StoryBored at it and check what your GPU can do.",
                },
                {
                  id: "need" as Path,
                  icon: <MonitorPlay size={16} />,
                  title: "I need to install one",
                  blurb: "Quick pointers for installing ComfyUI, then connect it.",
                },
                {
                  id: "none" as Path,
                  icon: <LayoutIcon size={16} />,
                  title: "No GPU — boards only",
                  blurb:
                    "Plan and structure your film without rendering. Connect an engine any time later.",
                },
              ] as const
            ).map((opt) => (
              <button
                key={opt.id}
                onClick={() => {
                  setPath(opt.id);
                  setStepIdx(1);
                }}
                className="flex w-full items-start gap-3 rounded-lg border border-line bg-ink-900 px-4 py-3 text-left transition-colors hover:border-amber-450/40"
              >
                <span className="mt-0.5 text-amber-450">{opt.icon}</span>
                <span>
                  <span className="block text-sm font-medium text-paper">
                    {opt.title}
                  </span>
                  <span className="block text-xs text-fog">{opt.blurb}</span>
                </span>
              </button>
            ))}
          </div>
        </StepCard>
      )}

      {/* ------------------------------------------------- step: engine */}
      {step === "engine" && (
        <StepCard>
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <Cpu size={15} className="text-amber-450" /> Connect your engine
          </h2>
          {path === "need" && (
            <div className="mt-3 rounded-lg border border-line/60 bg-ink-900 p-4 text-xs leading-relaxed text-mist">
              <p className="font-medium text-paper">Installing ComfyUI</p>
              <p className="mt-1 text-fog">
                Grab the desktop app or follow the manual install at{" "}
                <a
                  href="https://docs.comfy.org"
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-0.5 text-amber-450 hover:text-amber-350"
                >
                  docs.comfy.org <ExternalLink size={11} />
                </a>
                . It can run on this machine or any machine on your network with
                the GPU. Once it's up, its address appears in its startup log
                (usually port 8188). StoryBored's engines also need model files —{" "}
                <code>docs/MODELS.md</code> in your StoryBored folder lists what
                to download and where.
              </p>
            </div>
          )}
          <div className="mt-4 space-y-3.5">
            <Field label="Engine address" hint="The ComfyUI URL, e.g. http://127.0.0.1:8188 — another machine works too.">
              <Input
                value={comfyUrl}
                onChange={(e) => {
                  setComfyUrl(e.target.value);
                  setEngineProbe(null);
                }}
                placeholder="http://127.0.0.1:8188"
              />
            </Field>
            <div className="flex items-center gap-3">
              <Button onClick={() => testEngine.mutate()} busy={testEngine.isPending}>
                <FlaskConical size={14} /> Test
              </Button>
              {testEngine.isPending && (
                <span className="text-xs text-fog">Knocking on the engine's door…</span>
              )}
            </div>

            {engineProbe && !testEngine.isPending && (
              <div className="space-y-3 rounded-lg border border-line/60 bg-ink-900 p-4">
                <StatusLine
                  status={engineProbe.comfy.status}
                  okText="Engine found."
                />
                {engineOk && (
                  <>
                    {engineProbe.comfy.gpus.length > 0 ? (
                      <ul className="space-y-1">
                        {engineProbe.comfy.gpus.map((g) => (
                          <li
                            key={g.name}
                            className="flex items-center gap-2 text-sm text-paper"
                          >
                            <Cpu size={13} className="shrink-0 text-fog" />
                            <span className="min-w-0 flex-1 truncate">{g.name}</span>
                            {g.vram_gb != null && (
                              <span className="shrink-0 text-xs text-fog">
                                {g.vram_gb} GB
                              </span>
                            )}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="text-xs text-fog">No GPU reported.</p>
                    )}
                    <p className="flex items-start gap-2 text-xs text-mist">
                      <Badge tone={tier === "board" ? "amber" : "green"}>
                        {TIER_COPY[tier].label}
                      </Badge>
                      <span>{TIER_COPY[tier].blurb}</span>
                    </p>
                    {recommendedPacks.length > 0 && (
                      <div>
                        <p className="mb-1 text-xs font-medium uppercase tracking-wider text-fog">
                          Recommended for your GPU
                        </p>
                        <div className="space-y-1.5">
                          {recommendedPacks.map((rec) => (
                            <RecommendedRow
                              key={rec.id}
                              rec={rec}
                              modelsDirSet={modelsDirSet}
                              busy={downloadModels.isPending}
                              onDownload={(id) => downloadModels.mutate(id)}
                            />
                          ))}
                        </div>
                        {recommendedPacks.some(
                          (r) => !r.available && r.downloadable,
                        ) &&
                          !modelsDirSet && (
                            <p className="mt-1.5 text-[11px] text-fog">
                              These files can be fetched for you in one click —
                              set the engine models folder in{" "}
                              <Link
                                to="/settings"
                                className="text-amber-450 hover:text-amber-350"
                              >
                                Settings
                              </Link>{" "}
                              first (works when StoryBored runs on the same
                              computer as the engine).
                            </p>
                          )}
                        <p className="mt-1.5 text-[11px] text-fog">
                          Finishing setup makes these your default engines —
                          you can pick different ones in Settings any time.
                        </p>
                      </div>
                    )}
                    <div>
                      <p className="mb-1 text-xs font-medium uppercase tracking-wider text-fog">
                        Engines on this system
                      </p>
                      <ul className="space-y-1">
                        {engineProbe.workflows.map((w) => (
                          <li key={w.id} className="text-xs">
                            <div className="flex items-center gap-2">
                              <span className="min-w-0 flex-1 truncate text-paper">
                                {w.name}
                              </span>
                              <Badge tone="fog">{w.kind}</Badge>
                              {w.available ? (
                                <Badge tone="green">ready</Badge>
                              ) : (
                                <Badge tone="red">
                                  {w.missing_models.length || "?"} missing file
                                  {w.missing_models.length === 1 ? "" : "s"}
                                </Badge>
                              )}
                            </div>
                            {w.license_note && (
                              <p className="mt-0.5 text-[11px] leading-relaxed text-amber-450/80">
                                {w.license_note}
                              </p>
                            )}
                          </li>
                        ))}
                      </ul>
                      {engineProbe.workflows.some((w) => !w.available) && (
                        <p className="mt-1.5 text-xs text-fog">
                          Missing files are listed per engine in{" "}
                          <Link
                            to="/settings"
                            className="text-amber-450 hover:text-amber-350"
                          >
                            Settings
                          </Link>{" "}
                          — <code>docs/MODELS.md</code> explains where each one
                          comes from.
                        </p>
                      )}
                    </div>
                  </>
                )}
              </div>
            )}

            <div className="flex justify-between pt-1">
              <Button variant="ghost" onClick={back}>
                <ArrowLeft size={14} /> Back
              </Button>
              <div className="flex gap-2">
                {!engineOk && (
                  <Button variant="ghost" onClick={next}>
                    Skip for now
                  </Button>
                )}
                <Button variant="primary" onClick={next} disabled={!engineOk}>
                  Continue <ArrowRight size={14} />
                </Button>
              </div>
            </div>
          </div>
        </StepCard>
      )}

      {/* ---------------------------------------------- step: assistant */}
      {step === "assistant" && (
        <StepCard>
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <PenLine size={15} className="text-amber-450" /> Writing assistant
            <Badge tone="fog">optional</Badge>
          </h2>
          <p className="mt-1 text-xs leading-relaxed text-fog">
            Worth setting up: it powers script breakdown, story-vibes boards,
            the <em>Enhance</em> button that polishes rough shot notes, and
            motion drafts for video. Skip it and you simply write those
            yourself. Any OpenAI-compatible service works — a local Ollama at{" "}
            <code>http://127.0.0.1:11434/v1</code> is the easiest path, and it
            runs fine without a big GPU.
          </p>
          <div className="mt-4 space-y-3.5">
            <OllamaGuide
              suggested={recommended?.llm ?? "qwen3.5:9b"}
              onUseDefaults={() => {
                setLlmUrl("http://127.0.0.1:11434/v1");
                setLlmModel(recommended?.llm ?? "qwen3.5:9b");
                setLlmProbe(null);
              }}
            />
            <Field label="Service URL">
              <Input
                value={llmUrl}
                onChange={(e) => {
                  setLlmUrl(e.target.value);
                  setLlmProbe(null);
                }}
                placeholder="http://127.0.0.1:11434/v1"
              />
            </Field>
            <div className="flex items-center gap-3">
              <Button
                onClick={() => testLlm.mutate()}
                busy={testLlm.isPending}
                disabled={!llmUrl.trim()}
              >
                <FlaskConical size={14} /> Test
              </Button>
            </div>
            {llmProbe && !testLlm.isPending && (
              <div className="space-y-3 rounded-lg border border-line/60 bg-ink-900 p-4">
                <StatusLine
                  status={llmProbe.llm.status}
                  okText={
                    llmModels.length
                      ? `Found it — ${llmModels.length} model${llmModels.length === 1 ? "" : "s"} available.`
                      : "Found it."
                  }
                />
                {!llmOk && (
                  <p className="text-xs text-fog">
                    Key-protected hosted services may refuse this test — you can
                    still save the address and key, and the first breakdown will
                    tell you if something's off.
                  </p>
                )}
              </div>
            )}
            {llmOk && llmModels.length > 0 ? (
              <Field label="Model">
                <Select value={llmModel} onChange={(e) => setLlmModel(e.target.value)}>
                  <option value="">Choose a model…</option>
                  {llmModels.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </Select>
              </Field>
            ) : (
              <div className="grid grid-cols-2 gap-3">
                <Field label="Model">
                  <Input
                    value={llmModel}
                    onChange={(e) => setLlmModel(e.target.value)}
                    placeholder="model name"
                  />
                </Field>
                <Field label="API key" hint="Leave blank for local services.">
                  <Input
                    type="password"
                    value={llmKey}
                    onChange={(e) => setLlmKey(e.target.value)}
                    placeholder="sk-…"
                  />
                </Field>
              </div>
            )}
            <div className="flex justify-between pt-1">
              <Button variant="ghost" onClick={back}>
                <ArrowLeft size={14} /> Back
              </Button>
              <div className="flex gap-2">
                <Button variant="ghost" onClick={next}>
                  Skip — I'll write everything myself
                </Button>
                <Button
                  variant="primary"
                  onClick={next}
                  disabled={!llmUrl.trim() || !llmModel.trim()}
                >
                  Continue <ArrowRight size={14} />
                </Button>
              </div>
            </div>
          </div>
        </StepCard>
      )}

      {/* ------------------------------------------------ step: trainer */}
      {step === "trainer" && (
        <StepCard>
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <UserRound size={15} className="text-amber-450" /> Character trainer
            <Badge tone="fog">optional</Badge>
          </h2>
          <p className="mt-1 text-xs leading-relaxed text-fog">
            Lets you train a recurring character from 20–40 photos and use them
            as <code>@handles</code> in any shot. It drives an external trainer
            checkout on this machine — <code>docs/TRAINING.md</code> in your
            StoryBored folder explains the setup. Skip it and you can still
            import ready-made character files any time.
          </p>
          <div className="mt-4 space-y-3.5">
            <Field label="Trainer folder" hint="Path to a lora-factory-style checkout on this machine.">
              <Input
                value={trainerDir}
                onChange={(e) => {
                  setTrainerDir(e.target.value);
                  setTrainerProbe(null);
                }}
                placeholder="/path/to/lora-factory"
              />
            </Field>
            <div className="flex items-center gap-3">
              <Button
                onClick={() => testTrainer.mutate()}
                busy={testTrainer.isPending}
                disabled={!trainerDir.trim()}
              >
                <FlaskConical size={14} /> Test
              </Button>
              {trainerProbe && !testTrainer.isPending && (
                <StatusLine
                  status={trainerProbe.trainer.status}
                  okText="Folder found."
                />
              )}
            </div>
            <div className="flex justify-between pt-1">
              <Button variant="ghost" onClick={back}>
                <ArrowLeft size={14} /> Back
              </Button>
              <div className="flex gap-2">
                <Button variant="ghost" onClick={next}>
                  Skip
                </Button>
                <Button variant="primary" onClick={next} disabled={!trainerOk}>
                  Continue <ArrowRight size={14} />
                </Button>
              </div>
            </div>
          </div>
        </StepCard>
      )}

      {/* ------------------------------------------------ step: summary */}
      {step === "summary" && (
        <StepCard>
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <Sparkles size={15} className="text-amber-450" /> Ready
          </h2>
          <ul className="mt-4 space-y-2">
            <li className="flex items-center gap-3 rounded-md border border-line/60 bg-ink-900 px-3 py-2 text-sm">
              <span className="w-28 shrink-0 text-xs text-fog">Engine</span>
              {path === "none" ? (
                <span className="text-fog">
                  none — boards only for now, connect one any time in Settings
                </span>
              ) : engineOk ? (
                <span className="min-w-0 flex-1 truncate text-paper">
                  {comfyUrl} <Badge tone="green">{TIER_COPY[tier].label}</Badge>
                </span>
              ) : (
                <span className="text-fog">skipped — set it later in Settings</span>
              )}
            </li>
            <li className="flex items-center gap-3 rounded-md border border-line/60 bg-ink-900 px-3 py-2 text-sm">
              <span className="w-28 shrink-0 text-xs text-fog">Assistant</span>
              {llmUrl.trim() && llmModel.trim() ? (
                <span className="min-w-0 flex-1 truncate text-paper">
                  {llmModel} <span className="text-fog">at {llmUrl}</span>
                </span>
              ) : (
                <span className="text-fog">
                  skipped — script breakdown, Enhance and motion drafts stay off
                </span>
              )}
            </li>
            {path !== "none" && (
              <li className="flex items-center gap-3 rounded-md border border-line/60 bg-ink-900 px-3 py-2 text-sm">
                <span className="w-28 shrink-0 text-xs text-fog">Trainer</span>
                {trainerDir.trim() && trainerOk ? (
                  <span className="min-w-0 flex-1 truncate text-paper">{trainerDir}</span>
                ) : (
                  <span className="text-fog">
                    skipped — you can still import existing characters
                  </span>
                )}
              </li>
            )}
          </ul>
          {path === "none" && (
            <p className="mt-3 text-xs leading-relaxed text-fog">
              What works without an engine: the full board (scenes, shots,
              descriptions, ordering), script breakdown{llmUrl ? "" : " (once an assistant is set up)"},
              and the animatic exporter. Note the animatic can only include shots
              that already have stills or clips — so until an engine is
              connected, it's a planning tool, not a screening tool.
            </p>
          )}
          <div className="mt-5 flex justify-between">
            <Button variant="ghost" onClick={back}>
              <ArrowLeft size={14} /> Back
            </Button>
            <Button
              variant="primary"
              onClick={() => finish.mutate()}
              busy={finish.isPending}
            >
              <Check size={14} /> Finish setup
            </Button>
          </div>
        </StepCard>
      )}

      {!settings && step === "welcome" && (
        <div className="mt-4 flex justify-center">
          <Spinner />
        </div>
      )}
    </div>
  );
}
