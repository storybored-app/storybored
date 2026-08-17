import type { Job, Shot, Take } from "./types";

/** Shot label like "1A": scene number + shot letter. */
export function shotLabel(sceneIndex: number, shotIndex: number): string {
  let letters = "";
  let n = shotIndex;
  do {
    letters = String.fromCharCode(65 + (n % 26)) + letters;
    n = Math.floor(n / 26) - 1;
  } while (n >= 0);
  return `${sceneIndex + 1}${letters}`;
}

export function formatDuration(s: number): string {
  if (!Number.isFinite(s)) return "";
  return `${Math.round(s * 10) / 10}s`;
}

/** "13.1 GB" / "242 MB" style size for the model shopping list. */
export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "";
  if (n >= 2 ** 30) return `${(n / 2 ** 30).toFixed(1)} GB`;
  if (n >= 2 ** 20) return `${Math.round(n / 2 ** 20)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}

export function jobTypeLabel(t: Job["type"]): string {
  switch (t) {
    case "image_gen":
      return "Generating stills";
    case "model_download":
      return "Downloading model";
    case "character_thumb":
      return "Rendering portrait";
    case "video_gen":
      return "Rendering video";
    case "animatic":
      return "Exporting animatic";
    case "dataset_prep":
      return "Preparing photos";
    case "lora_train":
      return "Training character";
    case "lora_shootout":
      return "Comparing checkpoints";
    default:
      return t;
  }
}

/** Best take to show as a shot's thumbnail: picked, else latest done image. */
export function shotThumbTake(shot: Shot): Take | undefined {
  const takes = shot.takes ?? [];
  if (shot.picked_take_id != null) {
    const picked = takes.find((t) => t.id === shot.picked_take_id);
    if (picked) return picked;
  }
  const doneImages = takes.filter(
    (t) => t.kind === "image" && t.status === "done" && (t.thumb_path || t.file_path),
  );
  return doneImages.length ? doneImages[doneImages.length - 1] : undefined;
}

export function videoTake(shot: Shot): Take | undefined {
  const takes = shot.takes ?? [];
  if (shot.video_take_id != null) {
    const v = takes.find((t) => t.id === shot.video_take_id);
    if (v) return v;
  }
  const vids = takes.filter((t) => t.kind === "video" && t.status === "done");
  return vids.length ? vids[vids.length - 1] : undefined;
}

/** Suggest a rare trigger token for a new character, e.g. "zwx4 kelb". */
export function suggestTrigger(name: string): string {
  const consonants = "bcdfghjklmnpqrstvwxz";
  const rand = (s: string) => s[Math.floor(Math.random() * s.length)];
  const stem = name
    .toLowerCase()
    .replace(/[^a-z]/g, "")
    .slice(0, 3);
  return `${rand(consonants)}${rand(consonants)}${rand("aeiou")}${Math.floor(
    Math.random() * 9,
  )}${stem}`;
}

/** The handle rule the backend actually enforces (training wizard):
 *  lowercase, starts with a letter, then letters/digits/underscores, ≤32 chars. */
export const HANDLE_RE = /^[a-z][a-z0-9_]{0,31}$/;

export function isValidHandle(handle: string): boolean {
  return HANDLE_RE.test(handle);
}

/** Auto-suggest a handle from a name. Always produces a value that satisfies
 *  HANDLE_RE (lowercase, letter-start, underscores) so the wizard never rejects
 *  its own suggestion. Returns "" when the name has no usable letters. */
export function handleFromName(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "_") // spaces/punctuation → underscore
    .replace(/^[^a-z]+/, "") // must start with a letter
    .replace(/_+/g, "_") // collapse repeats
    .replace(/^_+|_+$/g, "") // trim underscores
    .slice(0, 32);
}

/** The queued/running video render job for a shot, if one exists in the jobs
 *  query. Lets the UI show in-flight state and block double-queuing. */
export function activeVideoJob(
  jobs: Job[] | undefined,
  shotId: number,
): Job | undefined {
  return (jobs ?? []).find((j) => {
    if (j.type !== "video_gen") return false;
    if (j.status !== "queued" && j.status !== "running") return false;
    try {
      return JSON.parse(j.payload_json || "{}").shot_id === shotId;
    } catch {
      return false;
    }
  });
}
