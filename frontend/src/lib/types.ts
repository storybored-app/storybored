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
  | "lora_shootout";
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

/** Human string for a health part. */
export function healthDetail(part: HealthPart | undefined): string {
  if (part == null) return "not configured";
  if (typeof part === "string") return part;
  if (typeof part === "boolean") return part ? "ok" : "unavailable";
  if (typeof part === "object") {
    if (typeof part.detail === "string" && part.detail) return part.detail;
    if (typeof part.status === "string" && part.status) return part.status;
    return part.ok ? "ok" : "unavailable";
  }
  return "unknown";
}
