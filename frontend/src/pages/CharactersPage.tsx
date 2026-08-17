import { useMemo, useRef, useState, type DragEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  ArrowLeft,
  ArrowRight,
  AtSign,
  Dices,
  GraduationCap,
  ImagePlus,
  Plus,
  Trash2,
  Trophy,
  Upload,
  Users,
  X,
} from "lucide-react";
import { useHealth } from "../components/HealthBanner";
import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPostForm,
  apiPost,
  mediaUrl,
} from "../lib/api";
import { healthOk } from "../lib/types";
import type { Character, Job, ShootoutRow, TrainingInfo } from "../lib/types";
import { handleFromName, isValidHandle, suggestTrigger } from "../lib/format";
import { EmptyState, ErrorState, Skeleton } from "../components/EmptyState";
import { Modal } from "../components/Modal";
import { Badge, Button, Field, Input, ProgressBar, Select, Spinner, TextArea } from "../components/ui";
import { useToast } from "../lib/toast";

/* ---------------- status helpers ---------------- */

function statusBadge(c: Character) {
  switch (c.status) {
    case "trained":
      return <Badge tone="green">trained</Badge>;
    case "training":
      return (
        <Badge tone="amber" pulse>
          training
        </Badge>
      );
    case "dataset":
      return <Badge tone="blue">photos ready</Badge>;
    default:
      return <Badge tone="fog">ready</Badge>;
  }
}

/* ---------------- identity fields (shared by both flows) ---------------- */

interface Identity {
  name: string;
  handle: string;
  trigger: string;
  class_word: string;
}

function IdentityFields({
  value,
  onChange,
  showTriggerDice,
}: {
  value: Identity;
  onChange: (v: Identity) => void;
  showTriggerDice?: boolean;
}) {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3">
        <Field label="Name">
          <Input
            autoFocus
            value={value.name}
            placeholder="e.g. Captain Mara"
            onChange={(e) => {
              const name = e.target.value;
              onChange({
                ...value,
                name,
                handle:
                  value.handle === handleFromName(value.name) || !value.handle
                    ? handleFromName(name)
                    : value.handle,
              });
            }}
          />
        </Field>
        <Field label="Handle" hint="Type @handle in shot descriptions.">
          <Input
            value={value.handle}
            onChange={(e) => onChange({ ...value, handle: handleFromName(e.target.value) })}
            placeholder="captain_mara"
          />
        </Field>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Field
          label="Trigger word"
          hint="A rare made-up word the engine associates with this face."
        >
          <div className="flex gap-1.5">
            <Input
              value={value.trigger}
              onChange={(e) => onChange({ ...value, trigger: e.target.value })}
            />
            {showTriggerDice && (
              <Button
                type="button"
                size="md"
                onClick={() => onChange({ ...value, trigger: suggestTrigger(value.name) })}
                title="Suggest a rare word"
              >
                <Dices size={14} />
              </Button>
            )}
          </div>
        </Field>
        <Field label="Class word" hint='Usually "person", "man" or "woman".'>
          <Input
            value={value.class_word}
            onChange={(e) => onChange({ ...value, class_word: e.target.value })}
          />
        </Field>
      </div>
    </div>
  );
}

/* ---------------- import existing tab ---------------- */

function ImportTab({ onDone }: { onDone: () => void }) {
  const { toast } = useToast();
  const qc = useQueryClient();
  const [identity, setIdentity] = useState<Identity>({
    name: "",
    handle: "",
    trigger: "",
    class_word: "person",
  });
  const [source, setSource] = useState<"existing" | "upload">("existing");
  const [loraName, setLoraName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [strength, setStrength] = useState("1.0");

  const { data: available, isError: lorasError } = useQuery<string[]>({
    queryKey: ["available-loras"],
    queryFn: () => apiGet<string[]>("/api/characters/available-loras"),
    retry: 1,
  });

  const submit = useMutation({
    mutationFn: async () => {
      // Uploading a file first copies it where the engine can load it;
      // picking an existing entry uses its dropdown name directly.
      let finalLoraName = loraName;
      if (source === "upload" && file) {
        const form = new FormData();
        form.set("file", file);
        const res = await apiPostForm<{ lora_name: string }>(
          "/api/characters/import-lora",
          form,
        );
        finalLoraName = res.lora_name;
      }
      return apiPost<Character>("/api/characters", {
        name: identity.name.trim(),
        handle: identity.handle.trim(),
        trigger: identity.trigger.trim(),
        class_word: identity.class_word.trim() || "person",
        lora_name: finalLoraName,
        lora_strength: Number.parseFloat(strength) || 1.0,
        status: "ready",
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["characters"] });
      toast(`${identity.name} is ready to cast.`, "success");
      onDone();
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const valid =
    identity.name.trim() &&
    isValidHandle(identity.handle) &&
    (source === "upload" ? !!file : !!loraName);

  return (
    <div className="space-y-4">
      <IdentityFields value={identity} onChange={setIdentity} />
      <Field label="Character file">
        <div className="mb-2 flex gap-1.5">
          <Button
            size="sm"
            variant={source === "existing" ? "primary" : "outline"}
            onClick={() => setSource("existing")}
          >
            Already on the engine
          </Button>
          <Button
            size="sm"
            variant={source === "upload" ? "primary" : "outline"}
            onClick={() => setSource("upload")}
          >
            Upload a file
          </Button>
        </div>
        {source === "existing" ? (
          lorasError ? (
            <p className="rounded-md border border-line p-3 text-xs text-fog">
              Couldn't list character files — the engine may be offline.
            </p>
          ) : (
            <Select value={loraName} onChange={(e) => setLoraName(e.target.value)}>
              <option value="">Choose a character file…</option>
              {(available ?? []).map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </Select>
          )
        ) : (
          <label className="flex cursor-pointer items-center gap-2 rounded-md border border-dashed border-line px-3 py-2.5 text-sm text-fog hover:border-amber-450/40 hover:text-mist">
            <Upload size={14} />
            {file ? file.name : "Choose a .safetensors file"}
            <input
              type="file"
              accept=".safetensors"
              className="hidden"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </label>
        )}
      </Field>
      <Field label="Strength" hint="How strongly the character is applied (0.5–1.2 typical).">
        <Input
          type="number"
          step={0.05}
          min={0}
          max={2}
          value={strength}
          onChange={(e) => setStrength(e.target.value)}
        />
      </Field>
      <div className="flex justify-end pt-1">
        <Button variant="primary" disabled={!valid} busy={submit.isPending} onClick={() => submit.mutate()}>
          Add character
        </Button>
      </div>
    </div>
  );
}

/* ---------------- training wizard ---------------- */

function PhotoGrid({
  files,
  onAdd,
  onRemove,
}: {
  files: File[];
  onAdd: (fs: File[]) => void;
  onRemove: (i: number) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const urls = useMemo(() => files.map((f) => URL.createObjectURL(f)), [files]);

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    const fs = Array.from(e.dataTransfer.files).filter((f) => f.type.startsWith("image/"));
    if (fs.length) onAdd(fs);
  };

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
      className={`rounded-lg border border-dashed p-3 transition-colors ${
        over ? "border-amber-450/70 bg-amber-450/5" : "border-line"
      }`}
    >
      <div className="grid grid-cols-5 gap-2 sm:grid-cols-6">
        {files.map((f, i) => (
          <div key={`${f.name}-${i}`} className="group relative aspect-square overflow-hidden rounded-md border border-line">
            <img src={urls[i]} alt="" className="h-full w-full object-cover" />
            <button
              onClick={() => onRemove(i)}
              className="absolute right-0.5 top-0.5 rounded bg-ink-950/80 p-0.5 text-fog opacity-0 transition-opacity hover:text-status-failed group-hover:opacity-100"
              aria-label="Remove photo"
            >
              <X size={11} />
            </button>
          </div>
        ))}
        <button
          onClick={() => inputRef.current?.click()}
          className="flex aspect-square flex-col items-center justify-center gap-1 rounded-md border border-dashed border-line text-fog hover:border-amber-450/40 hover:text-amber-450"
        >
          <ImagePlus size={16} />
          <span className="text-[10px]">add</span>
        </button>
      </div>
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        multiple
        className="hidden"
        onChange={(e) => {
          if (e.target.files?.length) onAdd(Array.from(e.target.files));
          e.target.value = "";
        }}
      />
      <p className="mt-2 text-center text-xs text-fog">
        Drag photos here or click add — clear, well-lit faces from different angles work best.
      </p>
    </div>
  );
}

function jobOfType(info: TrainingInfo | undefined, type: Job["type"]): Job | null {
  if (!info) return null;
  if (type === "dataset_prep" && info.prep_job) return info.prep_job;
  if (type === "lora_train" && info.train_job) return info.train_job;
  if (type === "lora_shootout" && info.shootout_job) return info.shootout_job;
  const list = info.jobs ?? [];
  const matches = list.filter((j) => j.type === type);
  return matches.length ? matches[matches.length - 1] : null;
}

/** After training: optional checkpoint shootout — render every saved version,
 *  score likeness + quality, and point the character at the winner. */
function ShootoutPanel({
  characterId,
  info,
  onClose,
}: {
  characterId: number;
  info: TrainingInfo;
  onClose: () => void;
}) {
  const { toast } = useToast();
  const qc = useQueryClient();
  const character = info.character;
  const shootout = jobOfType(info, "lora_shootout");

  const [strengths, setStrengths] = useState("0.7,1.0");
  const [ckpts, setCkpts] = useState("1500,2000,2500,final");
  const [seeds, setSeeds] = useState("1");
  const [rerunOpen, setRerunOpen] = useState(false);

  const start = useMutation({
    mutationFn: () =>
      apiPost(`/api/training/${characterId}/shootout`, {
        strengths: strengths.trim(),
        ckpts: ckpts.trim(),
        seeds: Number.parseInt(seeds, 10) || 1,
      }),
    onSuccess: () => {
      setRerunOpen(false);
      toast("Shootout started — rendering test shots of each version.", "success");
      qc.invalidateQueries({ queryKey: ["training", characterId] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const apply = useMutation({
    mutationFn: (row: ShootoutRow) =>
      apiPost(`/api/training/${characterId}/shootout/apply`, {
        checkpoint: row.checkpoint,
        strength: row.strength,
      }),
    onSuccess: () => {
      toast("Character switched to that version.", "success");
      qc.invalidateQueries({ queryKey: ["training", characterId] });
      qc.invalidateQueries({ queryKey: ["characters"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const options = (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-3">
        <Field label="Strengths" hint="Comma list — one grid row per strength.">
          <Input value={strengths} onChange={(e) => setStrengths(e.target.value)} />
        </Field>
        <Field label="Checkpoints" hint='Step numbers and/or "final"; empty = all (slower).'>
          <Input value={ckpts} onChange={(e) => setCkpts(e.target.value)} placeholder="all" />
        </Field>
        <Field label="Shots per prompt" hint="More is fairer but slower (1–4).">
          <Input
            type="number"
            min={1}
            max={4}
            value={seeds}
            onChange={(e) => setSeeds(e.target.value)}
          />
        </Field>
      </div>
      <Button
        variant="primary"
        className="w-full"
        busy={start.isPending}
        onClick={() => start.mutate()}
      >
        <Trophy size={15} /> Start shootout (~10–20 min on the GPU)
      </Button>
    </div>
  );

  // running / queued
  if (shootout && (shootout.status === "running" || shootout.status === "queued")) {
    return (
      <div className="space-y-4">
        <div className="text-center">
          <p className="text-sm font-medium text-paper">Checkpoint shootout in progress</p>
          <p className="mt-1 text-xs text-fog">
            Rendering the same test shots with each saved version, then scoring them for
            likeness and image quality. You can close this window; it keeps running.
          </p>
        </div>
        <ProgressBar value={shootout.progress} />
        {shootout.detail && (
          <pre className="max-h-24 overflow-y-auto whitespace-pre-wrap rounded-md border border-line bg-ink-950 p-2 text-[11px] leading-relaxed text-fog">
            {shootout.detail}
          </pre>
        )}
        <div className="flex justify-center">
          <Button onClick={onClose}>Close — keep running</Button>
        </div>
      </div>
    );
  }

  // done → results
  if (shootout?.status === "done") {
    let results: ShootoutRow[] = [];
    let hasGrid = false;
    try {
      const parsed = JSON.parse(shootout.result_json ?? "{}");
      results = parsed.results ?? [];
      hasGrid = !!parsed.grid;
    } catch {
      /* fall through to the raw-grid fallback below */
    }
    const gridUrl = `/api/training/${characterId}/shootout/grid?v=${shootout.id}`;
    const inUse = (row: ShootoutRow) =>
      !!character?.lora_name?.endsWith(`/${row.checkpoint}`) &&
      Math.abs((character?.lora_strength ?? 1) - row.strength) < 0.001;

    return (
      <div className="space-y-4">
        <div>
          <p className="text-sm font-medium text-paper">Shootout results</p>
          <p className="mt-0.5 text-xs text-fog">
            Ranked by likeness to the training photos (60%), prompt match and cleanliness
            (20% each). Trust your eyes too — open the contact sheet before picking.
          </p>
        </div>
        {hasGrid && (
          <a href={gridUrl} target="_blank" rel="noreferrer" title="Open full size">
            <img
              src={gridUrl}
              alt="Checkpoint comparison contact sheet"
              className="max-h-56 w-full rounded-md border border-line object-contain"
            />
          </a>
        )}
        {results.length > 0 ? (
          <div className="overflow-x-auto rounded-md border border-line">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-line text-left text-fog">
                  <th className="p-2 font-medium">Version</th>
                  <th className="p-2 font-medium">Strength</th>
                  <th className="p-2 font-medium">Likeness</th>
                  <th className="p-2 font-medium">Prompt</th>
                  <th className="p-2 font-medium">Clean</th>
                  <th className="p-2 font-medium">Total</th>
                  <th className="p-2" />
                </tr>
              </thead>
              <tbody>
                {results.map((row) => (
                  <tr
                    key={`${row.checkpoint}@${row.strength}`}
                    className={`border-b border-line last:border-0 ${
                      row.rank === 1 ? "bg-amber-450/5" : ""
                    }`}
                  >
                    <td className="p-2 text-paper">
                      <span className="inline-flex items-center gap-1.5">
                        {row.rank === 1 && <Trophy size={12} className="text-amber-450" />}
                        {row.label}
                      </span>
                      {row.no_face > 0 && (
                        <span className="ml-1.5 text-[10px] text-status-failed">
                          {row.no_face}/{row.cells} no face
                        </span>
                      )}
                    </td>
                    <td className="p-2 text-mist">{row.strength}</td>
                    <td className="p-2 text-mist">{row.likeness.toFixed(1)}</td>
                    <td className="p-2 text-mist">{row.prompt_match.toFixed(1)}</td>
                    <td className="p-2 text-mist">{row.clean.toFixed(1)}</td>
                    <td className="p-2 font-semibold text-paper">{row.total.toFixed(2)}</td>
                    <td className="p-2 text-right">
                      {inUse(row) ? (
                        <Badge tone="green">in use</Badge>
                      ) : (
                        <Button
                          size="sm"
                          busy={apply.isPending}
                          onClick={() => apply.mutate(row)}
                        >
                          Use this
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="rounded-md border border-line p-3 text-xs text-fog">
            The scores couldn't be read — open the contact sheet above and pick by eye, then
            set the version manually in the character's edit dialog.
          </p>
        )}
        <div className="flex items-center justify-between">
          <Button variant="ghost" size="sm" onClick={() => setRerunOpen((v) => !v)}>
            Run again with different settings
          </Button>
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        </div>
        {rerunOpen && options}
      </div>
    );
  }

  // failed / never run → pitch + options
  return (
    <div className="space-y-4">
      <div className="text-center">
        <Trophy size={26} className="mx-auto text-amber-450" />
        <p className="mt-2 text-sm font-medium text-paper">
          {shootout?.status === "failed" ? "Shootout failed" : "Training complete"}
        </p>
        {shootout?.status === "failed" ? (
          <p className="mx-auto mt-1 max-w-md rounded-md border border-status-failed/30 bg-status-failed/10 p-2 text-xs text-status-failed">
            {shootout.error ?? "unknown error"}
          </p>
        ) : (
          <p className="mx-auto mt-1 max-w-md text-xs text-fog">
            This character is ready — mention them with @ in any shot. Optional quality pass:
            training saved several versions along the way, and the last one isn't always the
            best. The shootout renders test shots with each version, scores the likeness, and
            lets you pick the winner.
          </p>
        )}
      </div>
      {options}
      <div className="flex justify-center">
        <Button variant="ghost" onClick={onClose}>
          Skip — use the final version
        </Button>
      </div>
    </div>
  );
}

/** Steps 3+4: prep progress → report review → train → training progress. */
function TrainingProgressPanel({ characterId, onClose }: { characterId: number; onClose: () => void }) {
  const { toast } = useToast();
  const qc = useQueryClient();

  const { data: info, isError } = useQuery<TrainingInfo>({
    queryKey: ["training", characterId],
    queryFn: () => apiGet<TrainingInfo>(`/api/training/${characterId}`),
    refetchInterval: 5_000,
    retry: 1,
  });

  const startTrain = useMutation({
    mutationFn: () => apiPost(`/api/training/${characterId}/train`),
    onSuccess: () => {
      toast("Training started — this takes a few hours.", "success");
      qc.invalidateQueries({ queryKey: ["training", characterId] });
      qc.invalidateQueries({ queryKey: ["characters"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  if (isError)
    return (
      <p className="rounded-md border border-line p-4 text-sm text-fog">
        Couldn't load training status — the server may be busy. It will refresh automatically.
      </p>
    );
  if (!info)
    return (
      <div className="flex items-center justify-center gap-2 p-8 text-sm text-fog">
        <Spinner /> Loading…
      </div>
    );

  const prep = jobOfType(info, "dataset_prep");
  const train = jobOfType(info, "lora_train");
  const report = info.report ?? info.report_md ?? null;

  // Training finished (or already-trained character reopened) → shootout panel.
  // A queued/running retrain still wins below, so only route here when the
  // latest train is done or there is no train job at all.
  if (
    train?.status === "done" ||
    (train == null && info.character?.status === "trained")
  ) {
    return <ShootoutPanel characterId={characterId} info={info} onClose={onClose} />;
  }

  // Step 4: training running
  if (train && (train.status === "running" || train.status === "queued")) {
    return (
      <div className="space-y-4">
        <div className="text-center">
          <p className="text-sm font-medium text-paper">Training in progress</p>
          <p className="mt-1 text-xs text-fog">
            The GPU is busy training — image and video generations will wait in line until
            it finishes (usually ~3 hours). You can close this window; training continues.
          </p>
        </div>
        <ProgressBar value={train.progress} />
        {train.detail && <p className="truncate text-center text-xs text-fog">{train.detail}</p>}
        <div className="flex justify-center">
          <Button onClick={onClose}>Close — keep training</Button>
        </div>
      </div>
    );
  }

  if (train?.status === "failed") {
    return (
      <div className="space-y-3">
        <p className="rounded-md border border-status-failed/30 bg-status-failed/10 p-3 text-sm text-status-failed">
          Training failed: {train.error ?? "unknown error"}
        </p>
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Close</Button>
          <Button variant="primary" onClick={() => startTrain.mutate()} busy={startTrain.isPending}>
            Try again
          </Button>
        </div>
      </div>
    );
  }

  // Step 3: prep running / report review
  if (prep && (prep.status === "running" || prep.status === "queued")) {
    return (
      <div className="space-y-4">
        <div className="text-center">
          <p className="text-sm font-medium text-paper">Preparing photos…</p>
          <p className="mt-1 text-xs text-fog">
            The photos are being checked, cropped and captioned. This takes a few minutes.
          </p>
        </div>
        <ProgressBar value={prep.progress || 0.1} />
        {prep.detail && <p className="truncate text-center text-xs text-fog">{prep.detail}</p>}
      </div>
    );
  }

  if (prep?.status === "failed") {
    return (
      <div className="space-y-3">
        <p className="rounded-md border border-status-failed/30 bg-status-failed/10 p-3 text-sm text-status-failed">
          Photo prep failed: {prep.error ?? "unknown error"}
        </p>
        <div className="flex justify-end">
          <Button onClick={onClose}>Close</Button>
        </div>
      </div>
    );
  }

  // Prep done → report review + big train button
  return (
    <div className="space-y-4">
      <div>
        <p className="text-sm font-medium text-paper">Photos are prepped</p>
        <p className="mt-0.5 text-xs text-fog">
          Review the summary below, then start training when it looks right.
        </p>
      </div>
      {report ? (
        <pre className="max-h-64 overflow-y-auto whitespace-pre-wrap rounded-md border border-line bg-ink-950 p-3 text-xs leading-relaxed text-mist">
          {report}
        </pre>
      ) : (
        <p className="rounded-md border border-line p-3 text-xs text-fog">
          No prep report available yet.
        </p>
      )}
      {(info.samples ?? info.sample_paths ?? []).length > 0 && (
        <div className="grid grid-cols-6 gap-1.5">
          {(info.samples ?? info.sample_paths ?? []).slice(0, 12).map((p) => {
            // GET /api/training/{id} returns `samples` as bare filenames living
            // under DATA_DIR/training/{handle}/raw — build the served path from
            // the handle. `sample_paths`, if a backend ever sends them, are
            // already DATA_DIR-relative and used as-is.
            const handle = info.character?.handle;
            const src =
              info.samples && handle
                ? mediaUrl(`training/${handle}/raw/${p}`)
                : mediaUrl(p);
            return (
              <img
                key={p}
                src={src}
                alt=""
                className="aspect-square rounded border border-line object-cover"
              />
            );
          })}
        </div>
      )}
      <Button
        variant="primary"
        className="h-11 w-full text-base"
        onClick={() => startTrain.mutate()}
        busy={startTrain.isPending}
      >
        <GraduationCap size={16} /> Start training (~3 hours)
      </Button>
    </div>
  );
}

function TrainWizard({ onDone }: { onDone: () => void }) {
  const { toast } = useToast();
  const qc = useQueryClient();
  const [step, setStep] = useState(1);
  const [identity, setIdentity] = useState<Identity>({
    name: "",
    handle: "",
    trigger: suggestTrigger(""),
    class_word: "person",
  });
  const [files, setFiles] = useState<File[]>([]);
  const [urlsText, setUrlsText] = useState("");
  const [characterId, setCharacterId] = useState<number | null>(null);

  const urlList = urlsText
    .split(/\n+/)
    .map((s) => s.trim())
    .filter((s) => /^https?:\/\//i.test(s));
  const total = files.length + urlList.length;

  const submit = useMutation({
    mutationFn: () => {
      const form = new FormData();
      form.set("name", identity.name.trim());
      form.set("handle", identity.handle.trim());
      form.set("trigger", identity.trigger.trim());
      form.set("class_word", identity.class_word.trim() || "person");
      for (const f of files) form.append("images", f);
      if (urlList.length) form.set("image_urls", JSON.stringify(urlList));
      return apiPostForm<{ character: Character; job_id: number }>(
        "/api/characters/wizard",
        form,
      );
    },
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["characters"] });
      setCharacterId(res.character.id);
      setStep(3);
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const steps = ["Who", "Photos", "Prep", "Train"];

  return (
    <div>
      {/* step indicator */}
      <div className="mb-5 flex items-center gap-1.5">
        {steps.map((label, i) => (
          <div key={label} className="flex flex-1 items-center gap-1.5">
            <span
              className={`flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold ${
                i + 1 <= step ? "bg-amber-450 text-ink-950" : "bg-ink-700 text-fog"
              }`}
            >
              {i + 1}
            </span>
            <span className={`text-xs ${i + 1 <= step ? "text-paper" : "text-fog"}`}>{label}</span>
            {i < steps.length - 1 && <span className="h-px flex-1 bg-line" />}
          </div>
        ))}
      </div>

      {step === 1 && (
        <div className="space-y-4">
          <IdentityFields value={identity} onChange={setIdentity} showTriggerDice />
          <div className="flex justify-end">
            <Button
              variant="primary"
              disabled={
                !identity.name.trim() ||
                !isValidHandle(identity.handle) ||
                !identity.trigger.trim()
              }
              onClick={() => setStep(2)}
            >
              Next: photos <ArrowRight size={14} />
            </Button>
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="space-y-4">
          <div className="flex items-baseline justify-between">
            <p className="text-sm text-mist">Add 20–40 photos of {identity.name || "your character"}.</p>
            <span
              className={`text-xs font-semibold ${
                total >= 20 && total <= 40
                  ? "text-status-approved"
                  : total > 0
                    ? "text-amber-450"
                    : "text-fog"
              }`}
            >
              {total} photo{total === 1 ? "" : "s"}
            </span>
          </div>
          <PhotoGrid
            files={files}
            onAdd={(fs) => setFiles((prev) => [...prev, ...fs].slice(0, 60))}
            onRemove={(i) => setFiles((prev) => prev.filter((_, j) => j !== i))}
          />
          <Field label="Or paste image links" hint="One link per line.">
            <TextArea rows={3} value={urlsText} onChange={(e) => setUrlsText(e.target.value)} placeholder="https://…" />
          </Field>
          {total > 0 && total < 20 && (
            <p className="text-xs text-amber-450">
              {total} is a start — 20 or more gives a much more faithful character.
            </p>
          )}
          <div className="flex justify-between">
            <Button variant="ghost" onClick={() => setStep(1)}>
              <ArrowLeft size={14} /> Back
            </Button>
            <Button
              variant="primary"
              disabled={total === 0}
              busy={submit.isPending}
              onClick={() => submit.mutate()}
            >
              Upload & prep photos <ArrowRight size={14} />
            </Button>
          </div>
        </div>
      )}

      {(step === 3 || step === 4) && characterId != null && (
        <TrainingProgressPanel characterId={characterId} onClose={onDone} />
      )}
    </div>
  );
}

/* ---------------- new character modal ---------------- */

/** Shown in place of the wizard when no trainer is configured — the wizard
 *  would otherwise let you upload 40 photos before failing on submit. */
function TrainerNotConfigured({ onClose }: { onClose: () => void }) {
  return (
    <div className="rounded-lg border border-line bg-ink-900 p-5 text-sm">
      <p className="font-medium text-paper">Training isn't set up yet</p>
      <p className="mt-1.5 text-xs leading-relaxed text-fog">
        Training a character from photos drives an external trainer on this
        machine, and none is configured. Point StoryBored at one in{" "}
        <Link
          to="/settings"
          onClick={onClose}
          className="text-amber-450 hover:text-amber-350"
        >
          Settings
        </Link>{" "}
        (docs/TRAINING.md walks through the trainer setup) — or skip training
        entirely and use the <em>Import existing</em> tab with a character file
        you already have.
      </p>
    </div>
  );
}

function NewCharacterModal({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<"import" | "train">("import");
  const { data: health } = useHealth();
  const trainerOk = healthOk(health?.trainer);
  return (
    <Modal title="New character" onClose={onClose} wide>
      <div className="mb-5 flex gap-1 rounded-lg border border-line p-1">
        <button
          onClick={() => setTab("import")}
          className={`flex-1 rounded-md py-1.5 text-sm font-medium transition-colors ${
            tab === "import" ? "bg-ink-700 text-paper" : "text-fog hover:text-mist"
          }`}
        >
          Import existing
        </button>
        <button
          onClick={() => setTab("train")}
          className={`flex-1 rounded-md py-1.5 text-sm font-medium transition-colors ${
            tab === "train" ? "bg-ink-700 text-paper" : "text-fog hover:text-mist"
          }`}
        >
          Train from photos
        </button>
      </div>
      {tab === "import" ? (
        <ImportTab onDone={onClose} />
      ) : trainerOk ? (
        <TrainWizard onDone={onClose} />
      ) : (
        <TrainerNotConfigured onClose={onClose} />
      )}
    </Modal>
  );
}

/* ---------------- edit modal ---------------- */

function EditCharacterModal({
  character,
  onClose,
  onShootout,
}: {
  character: Character;
  onClose: () => void;
  onShootout?: () => void;
}) {
  const { toast } = useToast();
  const qc = useQueryClient();
  const [trigger, setTrigger] = useState(character.trigger);
  const [strength, setStrength] = useState(String(character.lora_strength));
  const [notes, setNotes] = useState(character.notes ?? "");

  const save = useMutation({
    mutationFn: () =>
      apiPatch(`/api/characters/${character.id}`, {
        trigger,
        lora_strength: parseFloat(strength) || 1.0,
        notes,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["characters"] });
      onClose();
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const genThumb = useMutation({
    mutationFn: () =>
      apiPost(`/api/characters/${character.id}/generate-thumbnail`, {}),
    onSuccess: () => {
      toast(
        `Rendering a portrait of ${character.name} — the card updates when it's done.`,
        "success",
      );
      onClose();
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  return (
    <Modal title={`Edit ${character.name}`} onClose={onClose}>
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Trigger word">
            <Input value={trigger} onChange={(e) => setTrigger(e.target.value)} />
          </Field>
          <Field label="Strength">
            <Input
              type="number"
              step={0.05}
              min={0}
              max={2}
              value={strength}
              onChange={(e) => setStrength(e.target.value)}
            />
          </Field>
        </div>
        <Field label="Notes">
          <TextArea rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </Field>
        <div className="flex items-center justify-between gap-2">
          <div className="flex gap-2">
            <Button
              busy={genThumb.isPending}
              disabled={!character.lora_name}
              onClick={() => genThumb.mutate()}
              title={
                character.lora_name
                  ? "Render a fresh portrait with this character's LoRA and use it as the card thumbnail"
                  : "Needs a LoRA — train or import one first"
              }
            >
              <ImagePlus size={14} /> Generate thumbnail
            </Button>
            {character.status === "trained" && onShootout && (
              <Button
                onClick={onShootout}
                title="Compare the training checkpoints and pick the best version"
              >
                <Trophy size={14} /> Shootout
              </Button>
            )}
          </div>
          <div className="flex gap-2">
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button variant="primary" busy={save.isPending} onClick={() => save.mutate()}>
              Save
            </Button>
          </div>
        </div>
      </div>
    </Modal>
  );
}

/* ---------------- page ---------------- */

export function CharactersPage() {
  const { toast } = useToast();
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Character | null>(null);
  const [viewTrainingId, setViewTrainingId] = useState<number | null>(null);

  const { data, isLoading, isError, refetch } = useQuery<Character[]>({
    queryKey: ["characters"],
    queryFn: () => apiGet<Character[]>("/api/characters"),
  });

  const remove = useMutation({
    mutationFn: (id: number) => apiDelete(`/api/characters/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["characters"] }),
    onError: (e: Error) => toast(e.message, "error"),
  });

  return (
    <div>
      <div className="mb-7 flex items-end justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Characters</h1>
          <p className="mt-1 text-sm text-fog">
            Your cast — mention them with @ in any shot description.
          </p>
        </div>
        <Button variant="primary" onClick={() => setCreating(true)}>
          <Plus size={15} /> New character
        </Button>
      </div>

      {isLoading && (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          {[...Array(4)].map((_, i) => (
            <Skeleton key={i} className="h-52" />
          ))}
        </div>
      )}
      {isError && <ErrorState onRetry={() => refetch()} />}

      {data && data.length === 0 && (
        <EmptyState
          icon={Users}
          title="No characters yet"
          body="Bring a character in from an existing file, or train a new one from 20–40 photos. Then cast them in shots by typing @their_handle."
          action={
            <Button variant="primary" onClick={() => setCreating(true)}>
              <Plus size={15} /> Create your first character
            </Button>
          }
        />
      )}

      {data && data.length > 0 && (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          {data.map((c) => (
            <div
              key={c.id}
              className="group cursor-pointer overflow-hidden rounded-xl border border-line bg-ink-900 transition-colors hover:border-line-bright"
              onClick={() =>
                c.status === "dataset" || c.status === "training"
                  ? setViewTrainingId(c.id)
                  : setEditing(c)
              }
            >
              <div className="relative aspect-square w-full bg-ink-850">
                {c.thumbnail_path ? (
                  <img
                    src={mediaUrl(c.thumbnail_path)}
                    alt={c.name}
                    className="h-full w-full object-cover"
                  />
                ) : (
                  <div className="flex h-full w-full items-center justify-center">
                    <AtSign size={26} className="text-ink-600" strokeWidth={1.5} />
                  </div>
                )}
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    if (window.confirm(`Remove ${c.name} from the cast?`)) remove.mutate(c.id);
                  }}
                  className="absolute right-1.5 top-1.5 rounded bg-ink-950/70 p-1 text-transparent transition-colors hover:!text-status-failed group-hover:text-fog"
                  title="Delete character"
                >
                  <Trash2 size={13} />
                </button>
              </div>
              <div className="p-3">
                <div className="flex items-center justify-between gap-2">
                  <h2 className="truncate text-sm font-semibold text-paper">{c.name}</h2>
                  {statusBadge(c)}
                </div>
                <p className="mt-0.5 truncate text-xs text-fog">@{c.handle}</p>
              </div>
            </div>
          ))}
        </div>
      )}

      {creating && <NewCharacterModal onClose={() => setCreating(false)} />}
      {editing && (
        <EditCharacterModal
          character={editing}
          onClose={() => setEditing(null)}
          onShootout={() => {
            setViewTrainingId(editing.id);
            setEditing(null);
          }}
        />
      )}
      {viewTrainingId != null && (
        <Modal title="Character training" onClose={() => setViewTrainingId(null)} wide>
          <TrainingProgressPanel
            characterId={viewTrainingId}
            onClose={() => setViewTrainingId(null)}
          />
        </Modal>
      )}
    </div>
  );
}
