// Types mirroring docs/CONTRACT.md. Fields the backend may omit are optional
// so the UI degrades gracefully rather than crashing on shape drift.

export type ShotStatus = "draft" | "queued" | "generated" | "approved";
export type TakeKind = "image" | "video";
export type TakeStatus = "pending" | "done" | "failed";
export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled";
export type JobType =
  | "image_gen"
  | "video_gen"
  | "animatic"
  | "dataset_prep"
  | "lora_train"
  | "lora_shootout"
  | "character_thumb"
  | "project_export"
  | "model_download";
export type CharacterStatus = "ready" | "dataset" | "training" | "trained";

export interface Project {
  id: number;
  title: string;
  description: string;
  aspect_ratio: string;
  created_at?: string;
  updated_at?: string;
  /** board-order thumbnail of the first picked still (list endpoint only) */
  thumbnail_path?: string | null;
}

export interface Take {
  id: number;
  shot_id: number;
  kind: TakeKind;
  status: TakeStatus;
  file_path?: string | null;
  thumb_path?: string | null;
  workflow_id?: string;
  params_json?: string | null;
  seed?: number;
  error?: string | null;
  created_at?: string;
}

export interface Shot {
  id: number;
  scene_id: number;
  idx: number;
  description: string;
  shot_type: string;
  camera: string;
  dialogue: string;
  duration_s: number;
  motion_prompt: string;
  /** Where the picked still anchors the video clip: "first" | "last". */
  frame_position?: string;
  status: ShotStatus;
  picked_take_id?: number | null;
  video_take_id?: number | null;
  takes?: Take[];
}

export interface Scene {
  id: number;
  project_id: number;
  idx: number;
  title: string;
  slugline: string;
  description: string;
  shots?: Shot[];
}

export interface BoardProject extends Project {
  scenes?: Scene[];
}

export interface Character {
  id: number;
  name: string;
  handle: string;
  trigger: string;
  class_word: string;
  lora_name?: string;
  lora_strength: number;
  thumbnail_path?: string | null;
  notes?: string;
  status: CharacterStatus;
}

export interface Job {
  id: number;
  type: JobType;
  status: JobStatus;
  lane?: string;
  /** set when the job belongs to a project (gen/animatic/export jobs) */
  project_id?: number | null;
  payload_json?: string | null;
  result_json?: string | null;
  error?: string | null;
  progress: number;
  detail: string;
  created_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface WorkflowParameter {
  key: string;
  label?: string;
  type: string; // prompt | seed | int | float | string | bool | image ...
  node?: string;
  input?: string;
  default?: unknown;
  min?: number;
  max?: number;
  options?: string[];
}

/** One swappable model slot of an engine pack (e.g. the base UNET). */
export interface EngineModelSlot {
  key: string;
  label: string;
  node: string;
  input: string;
  /** Effective file: the user's override when set, else the baked one. */
  value: string;
  /** The pack's baked-in file. */
  baked: string;
  /** Engine dropdown choices ([] when the engine is unreachable). */
  options: string[];
  /** Options whose on-disk size likely exceeds a 24 GB card (only populated
   *  when COMFY_MODELS_DIR is configured and the file could be statted). */
  large_files?: string[];
}

/** Catalog info for one missing model file (GET /api/workflows). */
export interface MissingModelInfo {
  filename: string;
  /** ComfyUI models/ subfolder the file belongs in (e.g. "diffusion_models"). */
  folder: string;
  /** True when a verified source URL exists — the in-app downloader can fetch it. */
  downloadable: boolean;
  /** Verified direct download URL (absent for community-sourced files). */
  source?: string;
  /** Human page to visit (model card / hub page). */
  page?: string;
  size_bytes?: number;
  license?: string;
  notes?: string;
}

/** One baked-in LoRA of an engine pack, with any user override applied. */
export interface EngineLoraRow {
  node: string;
  lora_name: string;
  strength: number;
  baked_strength: number;
  enabled: boolean;
  disabled_with_character: boolean;
}

export interface WorkflowManifest {
  id: string;
  name: string;
  kind: "image" | "video" | string;
  description?: string;
  parameters?: WorkflowParameter[];
  available?: boolean;
  missing_models?: string[];
  /** Catalog info per missing file: destination folder, verified link, size. */
  missing_models_info?: MissingModelInfo[];
  /** Node classes the graph uses that this engine doesn't have installed. */
  missing_nodes?: string[];
  /** Set by GET /api/workflows when the engine couldn't be reached at all. */
  error?: string;
  /** True for the engine used when a shot doesn't pick one (per kind). */
  default?: boolean;
  supports_characters?: boolean;
  /** True when extra LoRAs can be spliced into this pack (image or video). */
  supports_loras?: boolean;
  /** True when the still can anchor the END of the clip (video packs). */
  supports_frame_position?: boolean;
  /** Baked LoRA stack in chain order, user overrides applied. */
  loras?: EngineLoraRow[];
  /** User-appended LoRAs (from the engine_loras setting). */
  added_loras?: StyleLora[];
  /** True when the engine_loras setting customizes this pack. */
  loras_modified?: boolean;
  /** Swappable model slots with the effective choice applied. */
  models?: EngineModelSlot[];
  /** True when the engine_models setting customizes this pack. */
  models_modified?: boolean;
}

export type HealthPart =
  | string
  | boolean
  | { ok?: boolean; status?: string; detail?: string; [k: string]: unknown }
  | null;

export interface Health {
  comfy?: HealthPart;
  llm?: HealthPart;
  trainer?: HealthPart;
  ffmpeg?: HealthPart;
  [k: string]: HealthPart | undefined;
}

/** One GPU as reported by the engine (vram_gb null when it didn't say). */
export interface SetupGpu {
  name: string;
  vram_gb: number | null;
}

/** What the machine can do, derived from reported VRAM (see /api/setup/probe). */
export type CapabilityTier = "board" | "stills" | "video";

/** GET /api/setup/probe — deep probe for the setup wizard. */
export interface SetupProbe {
  comfy: { status: string; url: string; gpus: SetupGpu[]; tier: CapabilityTier };
  llm: { status: string; url: string; models: string[] };
  trainer: { status: string; dir: string };
  ffmpeg: string;
  /** Pack availability against the probed engine (empty unless comfy ok). */
  workflows: {
    id: string;
    name: string;
    kind: string;
    available: boolean;
    missing_models: string[];
  }[];
  tiers: { stills_min_vram_gb: number; video_min_vram_gb: number };
}

/** GET /api/settings response: DB overrides + effective values (env merged in).
 *  `effective.llm_api_key_set` is a boolean — the key itself is never echoed. */
export interface SettingsMap {
  overrides?: Record<string, string>;
  effective?: Record<string, string | boolean>;
}

/** One entry in the `style_loras` setting (persisted as a JSON string). */
export interface StyleLora {
  lora_name: string;
  strength: number;
  enabled: boolean;
}

// LLM breakdown draft
export interface DraftShot {
  description: string;
  shot_type?: string;
  camera?: string;
  dialogue?: string;
  duration_s?: number;
  characters?: string[];
}
export interface DraftScene {
  title: string;
  slugline?: string;
  shots: DraftShot[];
}
export interface BreakdownDraft {
  scenes: DraftScene[];
}

export interface TrainingInfo {
  report?: string | null;
  report_md?: string | null;
  samples?: string[];
  sample_paths?: string[];
  prep_job?: Job | null;
  train_job?: Job | null;
  shootout_job?: Job | null;
  jobs?: Job[];
  character?: Character;
  [k: string]: unknown;
}

/** One ranked row of a checkpoint shootout (lora_shootout result_json.results). */
export interface ShootoutRow {
  rank: number;
  /** exact file to load, e.g. "hero-v1_000002500.safetensors" */
  checkpoint: string;
  /** "step 2500" | "final" */
  label: string;
  strength: number;
  total: number;
  likeness: number;
  prompt_match: number;
  clean: number;
  no_face: number;
  cells: number;
}

export interface ExportEntry {
  file_path?: string;
  path?: string;
  created_at?: string;
  size?: number;
  [k: string]: unknown;
}

/** True when a health part reports healthy, tolerant of several shapes. */
export function healthOk(part: HealthPart | undefined): boolean {
  if (part == null) return false;
  if (typeof part === "boolean") return part;
  if (typeof part === "string") {
    // /api/health reports the bundled ffmpeg as its resolved path when present.
    if (part.includes("/") || part.includes("\\")) return true;
    return ["ok", "ready", "up", "online", "healthy", "configured"].includes(
      part.toLowerCase(),
    );
  }
  if (typeof part === "object") {
    if (typeof part.ok === "boolean") return part.ok;
    if (typeof part.status === "string")
      return ["ok", "ready", "up", "online", "healthy", "configured"].includes(
        part.status.toLowerCase(),
      );
  }
  return false;
}

/** Friendly copy for the backend's health-status vocabulary (see the
 *  /api/health contract). Unknown strings pass through untouched. */
const STATUS_LABELS: Record<string, string> = {
  ok: "ok",
  not_configured: "not set up",
  unreachable: "can't be reached",
  unrecognized: "something answered, but it doesn't look like the right service",
  unauthorized: "it wants an API key — check the key in Settings",
  error: "reachable, but reporting a server error",
  missing: "folder not found",
};

/** Human string for a health part. */
export function healthDetail(part: HealthPart | undefined): string {
  if (part == null) return "not set up";
  if (typeof part === "string") return STATUS_LABELS[part] ?? part;
  if (typeof part === "boolean") return part ? "ok" : "unavailable";
  if (typeof part === "object") {
    if (typeof part.detail === "string" && part.detail) return part.detail;
    if (typeof part.status === "string" && part.status)
      return STATUS_LABELS[part.status] ?? part.status;
    return part.ok ? "ok" : "unavailable";
  }
  return "unknown";
}
