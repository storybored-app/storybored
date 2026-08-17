import { useRef, useState, type DragEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, FileJson, Upload } from "lucide-react";
import { apiPost } from "../lib/api";
import type {
  AnalyzeCandidate,
  ImportedWorkflowInfo,
  WorkflowAnalysis,
} from "../lib/types";
import { Badge, Button, Field, Input, Select, TextArea } from "./ui";
import { Modal } from "./Modal";
import { useToast } from "../lib/toast";

type Step = "file" | "map" | "name" | "done";

/** Wizard roles rendered as node dropdowns, in display order. */
const ROLE_ROWS: {
  role: string;
  label: string;
  hint: string;
  required?: boolean;
  videoOnly?: boolean;
}[] = [
  {
    role: "prompt",
    label: "Prompt",
    hint: "Where the shot description is written.",
    required: true,
  },
  { role: "seed", label: "Seed", hint: "Randomized per take so takes are reproducible." },
  { role: "size", label: "Size", hint: "The node holding width and height." },
  {
    role: "output",
    label: "Output",
    hint: "The save node StoryBored reads results from.",
    required: true,
  },
  {
    role: "image",
    label: "Still input",
    hint: "Receives the shot's approved still.",
    videoOnly: true,
  },
  { role: "length", label: "Clip length", hint: "Frames per clip.", videoOnly: true },
];

function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

function candidateLabel(c: AnalyzeCandidate): string {
  const bits = [`node ${c.node}`, c.class_type ?? ""];
  if (c.role) bits.push(c.role);
  if (c.lora_name) bits.push(c.lora_name);
  if (c.preview) bits.push(`“${c.preview.slice(0, 48)}…”`);
  if (c.width != null && c.height != null) bits.push(`${c.width}×${c.height}`);
  return bits.filter(Boolean).join(" — ");
}

export function ImportWorkflowWizard({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const inputRef = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [step, setStep] = useState<Step>("file");
  const [fileError, setFileError] = useState("");
  const [graph, setGraph] = useState<Record<string, unknown> | null>(null);
  const [analysis, setAnalysis] = useState<WorkflowAnalysis | null>(null);
  /** role → chosen node id ("" = not used). Seam/slot store node ids too. */
  const [picks, setPicks] = useState<Record<string, string>>({});
  const [name, setName] = useState("");
  const [id, setId] = useState("");
  const [idDirty, setIdDirty] = useState(false);
  const [description, setDescription] = useState("");
  const [created, setCreated] = useState<ImportedWorkflowInfo | null>(null);

  const analyze = useMutation({
    mutationFn: (g: Record<string, unknown>) =>
      apiPost<WorkflowAnalysis>("/api/workflows/analyze", { graph: g }),
    onSuccess: (a, g) => {
      setGraph(g);
      setAnalysis(a);
      const next: Record<string, string> = {};
      for (const { role } of ROLE_ROWS) next[role] = a.roles[role]?.suggested?.node ?? "";
      next.seam = a.roles.seam?.suggested?.after_node ?? "";
      next.slot = a.model_slots[0]?.node ?? "";
      setPicks(next);
      setStep("map");
    },
    onError: (e: Error) => setFileError(e.message),
  });

  const readFile = (file: File) => {
    setFileError("");
    file.text().then((text) => {
      try {
        analyze.mutate(JSON.parse(text));
      } catch {
        setFileError("That file isn't JSON — export the workflow again and retry.");
      }
    });
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    const file = e.dataTransfer.files[0];
    if (file) readFile(file);
  };

  const create = useMutation({
    mutationFn: () => {
      if (!analysis || !graph) throw new Error("no analyzed workflow");
      const kind = analysis.kind;
      const cand = (role: string): AnalyzeCandidate | undefined =>
        analysis.roles[role]?.candidates.find((c) => c.node === picks[role]);
      const parameters: Record<string, unknown>[] = [];
      const prompt = cand("prompt");
      if (prompt) {
        parameters.push({
          key: "prompt",
          label: kind === "video" ? "Motion prompt" : "Prompt",
          type: "prompt",
          node: prompt.node,
          input: prompt.input,
        });
      }
      const seed = cand("seed");
      if (seed) {
        parameters.push({ key: "seed", type: "seed", node: seed.node, input: seed.input });
      }
      const size = cand("size");
      if (size) {
        parameters.push(
          { key: "width", label: "Width", type: "int", node: size.node, input: "width", default: size.width },
          { key: "height", label: "Height", type: "int", node: size.node, input: "height", default: size.height },
        );
      }
      if (kind === "video") {
        const image = cand("image");
        if (image) {
          parameters.push({
            key: "first_frame",
            label: "First frame",
            type: "image",
            node: image.node,
            input: image.input,
          });
        }
        const length = cand("length");
        if (length) {
          parameters.push({
            key: "length",
            label: "Length (frames)",
            type: "int",
            node: length.node,
            input: length.input,
            default: length.value,
          });
        }
      }
      const body: Record<string, unknown> = {
        id,
        name: name.trim(),
        kind,
        description: description.trim(),
        graph,
        parameters,
        output_node: picks.output,
      };
      if (picks.seam) {
        if (kind === "video") {
          body.lora_injection = {
            after_node: picks.seam,
            class_type: analysis.roles.seam?.suggested?.class_type ?? "LoraLoaderModelOnly",
          };
        } else {
          body.character_injection = { after_node: picks.seam };
        }
      }
      const slot = analysis.model_slots.find((s) => s.node === picks.slot);
      if (slot) {
        body.model_slots = [
          {
            key: slot.key,
            label: kind === "video" ? "Video model" : "Base model",
            node: slot.node,
            input: slot.input,
          },
        ];
      }
      if (kind === "video" && analysis.frame_conditioning) {
        body.frame_conditioning = analysis.frame_conditioning;
      }
      return apiPost<ImportedWorkflowInfo>("/api/workflows/import", body);
    },
    onSuccess: (info) => {
      setCreated(info);
      setStep("done");
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });

  const idOk = /^[a-z0-9][a-z0-9-]{0,63}$/.test(id);
  const seamCands = analysis?.roles.seam?.candidates ?? [];

  return (
    <Modal title="Import workflow" onClose={onClose} wide>
      {step === "file" && (
        <div>
          <p className="mb-3 text-sm text-fog">
            Turn your own ComfyUI workflow into a StoryBored engine. Upload the
            workflow's <span className="font-medium text-paper">API-format</span>{" "}
            export — in ComfyUI, enable dev mode (Settings → "Enable Dev mode
            Options"), then use "Save (API Format)".
          </p>
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setOver(true);
            }}
            onDragLeave={() => setOver(false)}
            onDrop={onDrop}
            className={`flex cursor-pointer flex-col items-center gap-2 rounded-lg border border-dashed p-8 text-fog transition-colors ${
              over ? "border-amber-450/70 bg-amber-450/5" : "border-line hover:border-fog/50"
            }`}
            onClick={() => inputRef.current?.click()}
          >
            <FileJson size={22} />
            <span className="text-sm">Drop the exported .json here, or click to pick it</span>
          </div>
          <input
            ref={inputRef}
            type="file"
            accept=".json,application/json"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) readFile(file);
              e.target.value = "";
            }}
          />
          {analyze.isPending && <p className="mt-3 text-xs text-fog">Reading the workflow…</p>}
          {fileError && <p className="mt-3 text-xs text-status-failed">{fileError}</p>}
        </div>
      )}

      {step === "map" && analysis && (
        <div>
          <div className="mb-3 flex items-center gap-2">
            <p className="flex-1 text-sm text-fog">
              StoryBored read {analysis.node_count} nodes and guessed the important
              ones — confirm or correct each mapping.
            </p>
            <Badge tone="fog">{analysis.kind}</Badge>
          </div>
          {analysis.warnings.length > 0 && (
            <ul className="mb-3 space-y-1">
              {analysis.warnings.map((w) => (
                <li key={w} className="text-xs text-amber-450/90">
                  {w}
                </li>
              ))}
            </ul>
          )}
          <div className="space-y-3">
            {ROLE_ROWS.filter((r) => !r.videoOnly || analysis.kind === "video").map(
              ({ role, label, hint, required }) => {
                const cands = analysis.roles[role]?.candidates ?? [];
                return (
                  <Field key={role} label={label} hint={hint}>
                    <Select
                      value={picks[role] ?? ""}
                      onChange={(e) => setPicks({ ...picks, [role]: e.target.value })}
                    >
                      <option value="">
                        {required
                          ? cands.length
                            ? "Choose a node…"
                            : "No matching node found"
                          : "Not used"}
                      </option>
                      {cands.map((c) => (
                        <option key={`${c.node}-${c.input ?? ""}`} value={c.node}>
                          {candidateLabel(c)}
                        </option>
                      ))}
                    </Select>
                  </Field>
                );
              },
            )}
            <Field
              label="Style & character attachment"
              hint="Where characters and extra style LoRAs hook into the graph — usually the last LoRA in your chain, or the model loader."
            >
              <Select
                value={picks.seam ?? ""}
                onChange={(e) => setPicks({ ...picks, seam: e.target.value })}
              >
                <option value="">Not used — characters and styles stay off</option>
                {seamCands.map((c) => (
                  <option key={c.node} value={c.node}>
                    {candidateLabel(c)}
                  </option>
                ))}
              </Select>
            </Field>
            {analysis.model_slots.length > 0 && (
              <Field
                label="Swappable model"
                hint="Lets you swap this engine's base model from Settings later."
              >
                <Select
                  value={picks.slot ?? ""}
                  onChange={(e) => setPicks({ ...picks, slot: e.target.value })}
                >
                  <option value="">Not swappable</option>
                  {analysis.model_slots.map((s) => (
                    <option key={s.node} value={s.node}>
                      node {s.node} — {s.class_type} — {s.value}
                    </option>
                  ))}
                </Select>
              </Field>
            )}
          </div>
          <div className="mt-4 flex justify-between">
            <Button variant="ghost" onClick={() => setStep("file")}>
              Back
            </Button>
            <Button
              variant="primary"
              disabled={!picks.prompt || !picks.output}
              onClick={() => setStep("name")}
            >
              Next
            </Button>
          </div>
        </div>
      )}

      {step === "name" && analysis && (
        <div className="space-y-3.5">
          <Field label="Name" hint="Shown in the engine selector.">
            <Input
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                if (!idDirty) setId(slugify(e.target.value));
              }}
              placeholder="My photoreal look"
              autoFocus
            />
          </Field>
          <Field
            label="Id"
            hint="Lowercase letters, digits and hyphens. Pick it once — it's stored on every take rendered with this engine."
          >
            <Input
              value={id}
              onChange={(e) => {
                setId(e.target.value);
                setIdDirty(true);
              }}
              placeholder="my-photoreal-look"
            />
          </Field>
          <Field label="Description (optional)">
            <TextArea
              rows={2}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="One or two sentences about the look."
            />
          </Field>
          {!idOk && id !== "" && (
            <p className="text-xs text-status-failed">
              The id must be a lowercase slug, e.g. "my-photoreal-look".
            </p>
          )}
          <div className="flex justify-between pt-1">
            <Button variant="ghost" onClick={() => setStep("map")}>
              Back
            </Button>
            <Button
              variant="primary"
              disabled={!name.trim() || !idOk}
              busy={create.isPending}
              onClick={() => create.mutate()}
            >
              <Upload size={14} /> Create engine
            </Button>
          </div>
        </div>
      )}

      {step === "done" && created && (
        <div>
          <div className="mb-3 flex items-center gap-2">
            <CheckCircle2 size={18} className="text-status-approved" />
            <span className="flex-1 text-sm font-medium text-paper">
              {created.name} is installed
            </span>
            {created.available ? (
              <Badge tone="green">ready</Badge>
            ) : created.error ? (
              <Badge tone="amber">engine offline</Badge>
            ) : created.missing_models.length > 0 ? (
              <Badge tone="red">missing models</Badge>
            ) : (
              <Badge tone="red">missing nodes</Badge>
            )}
          </div>
          {created.available ? (
            <p className="text-sm text-fog">
              Everything it needs is installed — it's ready to render.
            </p>
          ) : created.error ? (
            <p className="text-sm text-fog">
              The rendering engine couldn't be reached, so availability isn't
              checked yet — it will be the next time the engine is up.
            </p>
          ) : (
            <div>
              {created.missing_models.length > 0 && (
                <>
                  <p className="mb-1.5 text-sm text-fog">
                    It needs model files that aren't installed yet:
                  </p>
                  <ul className="mb-2 space-y-1">
                    {created.missing_models.map((f) => (
                      <li key={f} className="font-mono text-xs text-status-failed/90">
                        {f}
                      </li>
                    ))}
                  </ul>
                </>
              )}
              {created.missing_nodes.length > 0 && (
                <>
                  <p className="mb-1.5 text-sm text-fog">
                    It uses nodes your rendering engine doesn't have:
                  </p>
                  <ul className="mb-2 space-y-1">
                    {created.missing_nodes.map((n) => (
                      <li key={n} className="font-mono text-xs text-status-failed/90">
                        {n}
                      </li>
                    ))}
                  </ul>
                </>
              )}
              <p className="text-xs text-fog">
                Expand the engine's row in the list below for download links and
                folder guidance — same as any other engine with missing pieces.
              </p>
            </div>
          )}
          <p className="mt-3 text-xs text-fog">
            The engine now appears in the Engines list here and in the shot
            drawer's engine menu.
          </p>
          <div className="mt-4 flex justify-end">
            <Button variant="primary" onClick={onClose}>
              Done
            </Button>
          </div>
        </div>
      )}
    </Modal>
  );
}
