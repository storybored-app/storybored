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
  | "lora_train";
export type CharacterStatus = "ready" | "dataset" | "training" | "trained";

export interface Project {
  id: number;
  title: string;
  description: string;
  aspect_ratio: string;
  created_at?: string;
  updated_at?: string;
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
  jobs?: Job[];
  character?: Character;
  [k: string]: unknown;
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
