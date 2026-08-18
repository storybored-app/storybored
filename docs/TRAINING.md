# Training characters

A **character** in StoryBored is a LoRA plus a little metadata: a display name,
an `@handle` you type in shot descriptions, a **trigger token** the LoRA
answers to, a class word ("person", "dog", "robot"…), and a strength. You can
get one two ways:

- **Import an existing LoRA** — Characters → New character → *Import existing
  LoRA*. Pick one your ComfyUI already knows about, or upload a
  `.safetensors` file (requires a LoRA folder — Settings → "LoRA folder", or
  `COMFY_LORAS_DIR` in `.env` — so StoryBored can copy it where ComfyUI
  looks). Set the trigger yourself — it's whatever the LoRA was trained with.
- **Train from photos** — the wizard described below. Requires a trainer
  (see [Prerequisites](#prerequisites)).

## Consent first

If you're training on a **real person**, get their explicit consent before you
collect a single photo — and honor it if they later change their mind (delete
the dataset *and* the trained LoRA). Don't train on photos of strangers,
public figures, or anyone who hasn't agreed. For fictional characters, use
images you have the rights to. StoryBored won't police this for you; it's on
you.

## Model families

A character LoRA is **bound to the model family it was trained on**: one
trained for Krea 2 is inert on Z-Image or Qwen-Image (different transformer,
different weight shapes — the file loads, the face never appears). StoryBored
tracks the family end to end so you find that out before a render, not after:

- **Engines declare a family.** Image packs may carry a `lora_family` id in
  their manifest ([WORKFLOWS.md](WORKFLOWS.md#manifestjson-field-by-field));
  the shipped packs declare `krea2` (both Krea 2 packs), `z-image` and
  `qwen-image`. A pack without the key is family-agnostic — no checks.
- **Characters record a family.** Training stamps it automatically (below);
  imported characters get a "Made for engine" picker that defaults to your
  default image engine's family, with "not sure — any engine" allowed.
  Characters created before families existed read as *unknown* and are never
  blocked or warned about.
- **Training targets your default image engine.** The wizard resolves that
  engine's `lora_family` and tells the trainer to produce a LoRA of that
  family (the `--family` argument of the trainer contract), stamping it on
  the character at train time. If the default engine declares no family,
  training falls back to `krea2` — the historical behavior of the
  lora-factory pipeline.
- **Compatibility guard.** Generating a shot whose `@characters` carry a
  family different from the selected engine's is refused with a clear 409 —
  *"@mari was trained for Krea 2 — this engine renders with Z-Image; switch
  engines or remove the mention"* — and the shot drawer marks incompatible
  engines *before* you hit Generate. Unknown/agnostic on either side never
  blocks.

Hardware and time per family (sourced measurements, not promises):

| Family | GPU class | Recipe | Time | Sources |
| --- | --- | --- | --- | --- |
| Krea 2 (`krea2`) | 24 GB-class | 3000 steps, 1024/1280 buckets, qfloat8 | ~2.5–4 h (~4 s/it) | lora-factory README (recipe tuned on a 32 GB card) |
| Z-Image (`z-image`) | 12 GB-class proven | 2000 steps, 512/768 buckets (skip 1024), float8 + memory optimization | ~1–2 h (~2–3 s/it); good results from as few as 6–25 images | [ai-toolkit #550](https://github.com/ostris/ai-toolkit/issues/550), [Z-Image #36](https://github.com/Tongyi-MAI/Z-Image/issues/36), [neurocanvas guide](https://neurocanvas.net/blog/zimage-lora-training-guide/), [HF community blog](https://huggingface.co/blog/content-and-code/training-a-lora-for-z-image-turbo) |
| Qwen-Image (`qwen-image`) | — | **experimental**: ai-toolkit `qwen_image` arch, 2000-step example config | no verified VRAM/time numbers | [ai-toolkit example config](https://github.com/ostris/ai-toolkit/blob/main/config/examples/train_lora_qwen_image_24gb.yaml) |

Below 12 GB, no training recipe is established for any shipped family — the
practical answer at that tier is **importing ready-made LoRAs** (community
hubs carry thousands of Z-Image character LoRAs). The setup wizard's trainer
step tells you which of these applies to your GPU.

## Prerequisites

Character training wraps an external **lora-factory-style trainer** — a
separate, local project that handles dataset prep, captioning, and the actual
training run. StoryBored does not bundle a trainer; it drives whatever checkout
you point `LORA_FACTORY_DIR` at, and expects that checkout to expose the
prep/train scripts described in [WORKFLOWS.md](WORKFLOWS.md). Any trainer that
follows that contract works.

This feature degrades gracefully: with no trainer configured, everything else
in StoryBored keeps working and the "Train from photos" tab simply shows a
"configure in Settings" hint. You can also skip training entirely and **import
an existing LoRA** (see the top of this page).

To enable training:

1. Set up a lora-factory-style trainer on the machine with the GPU and follow
   its own README.
2. Point StoryBored at it — either in Settings (the setup wizard's trainer
   step, or Settings → "Character trainer"; takes effect immediately) or in
   `.env` (needs a restart):

   ```
   LORA_FACTORY_DIR=/path/to/lora-factory
   ```

3. Settings → trainer should show a green status.

If `LORA_FACTORY_DIR` is unset or wrong, the "Train from photos" tab shows a
friendly "configure in Settings" hint instead of the wizard — nothing breaks.

## The dataset: 20–40 diverse photos

Quality of the dataset decides quality of the character. Aim for **20–40
photos** with real variety:

- **Vary everything except the subject**: different angles, distances
  (close-up through full body), lighting (indoor, outdoor, golden hour),
  expressions, outfits, and backgrounds.
- **Sharp and well-lit** beats high-resolution. Skip blurry, heavily filtered,
  or tiny images.
- **One subject per photo.** Group shots confuse training.
- **Avoid repetition** — 30 near-identical selfies teach the model one pose,
  not one person. Ten diverse photos beat forty clones.
- The prep step will reject unusable images and tell you why, so when in
  doubt, include the photo and let prep decide.

## Trigger tokens

The **trigger** is a rare token the LoRA binds the identity to — you never
type it yourself after setup. When you write `@sam` in a shot description,
StoryBored substitutes `"{trigger} {class_word}"` (e.g. `zxsam person`) into
the prompt at generation time.

Rules of thumb (the wizard auto-suggests one that follows them):

- Short, lowercase, and **not a real word** — `zxsam`, `ohwxa`, `kestrl`.
- Unique per character. Reusing a trigger across characters cross-contaminates.
- Don't change it after training — it's baked into the LoRA.

## The wizard, step by step

Characters → New character → **Train from photos**.

1. **Identity** — name, `@handle`, trigger (auto-suggested; editable), class
   word. The class word is the generic noun the trigger attaches to — usually
   `person`.
2. **Photos** — drag-drop 20–40 images and/or paste image URLs (downloads are
   capped at 10 MB each, 60 images max). A live counter and guidance text keep
   you in the healthy range.
3. **Prep & review** — StoryBored runs the trainer's dataset prep as a job
   (crop, filter, caption). When it finishes you get a **prep report**:
   how many images were kept vs. rejected and why. Review it — if too few
   survived, add better photos and re-run prep rather than training on scraps.
   Then hit **Start training (~3h)**.
4. **Training** — a progress bar tracks the run. Training owns the GPU, so
   generations queue behind it (the UI says so). When it completes, the
   character flips to **trained**, wired to the final checkpoint at strength
   1.0 — adjust strength on the character card if the identity is too strong
   or too weak in generations.
5. **Shootout (optional, recommended)** — training saves a checkpoint every
   250 steps, and the last one isn't always the best (overtraining is real).
   The panel that appears after training — also reachable later via the
   character's edit dialog → **Shootout** — renders the same test shots with
   each selected checkpoint × strength, builds a labeled contact sheet, and
   scores every combo (face likeness against your training photos weighs 60%,
   prompt match and artifact-cleanliness 20% each). Ranked results come with a
   one-click **Use this** per row; the contact sheet opens full-size so you can
   overrule the judges with your eyes. Defaults test steps 1500/2000/2500 and
   final at strengths 0.7 and 1.0 (~10–20 min on the GPU); clear the
   checkpoints field to test every saved version.

Time expectations: prep is minutes; training is **hours**, and how many
depends on the target family (see [Model families](#model-families)): roughly
2.5–4 h for a Krea 2 run on a 24 GB-class GPU, ~1–2 h for a Z-Image run on a
12 GB-class one — start it before bed, not before a review meeting.

> **Restart survival:** the training subprocess is started detached with a
> pidfile under `DATA_DIR/training/pids/`, so restarting the StoryBored server
> mid-run does **not** lose the training — on startup the server re-attaches to
> the live process and the job keeps reporting progress. One deployment caveat:
> if you run StoryBored under **systemd**, the default `KillMode=control-group`
> kills every process in the service's cgroup on restart, including the
> detached trainer. Add `KillMode=process` to the `[Service]` section so only
> the server itself is stopped. If the trainer dies anyway, the job fails
> cleanly and the character returns to **dataset** — just press
> *Start training* again (cached prep artifacts make the rerun cheaper).

## Using a trained character

Type `@` in any shot description and pick the character from the autocomplete.
That's it — every take for that shot gets the character injected (the engine
splices the LoRA into the generation graph and swaps `@handle` for the trigger
phrase automatically; packs can also disable conflicting style LoRAs while a
character is active — see [WORKFLOWS.md](WORKFLOWS.md#character-injection-explained)).

Multiple characters in one shot work: mention both handles and they're chained
in sequence. Expect identity fidelity to drop a little with each additional
character — that's a property of stacked LoRAs, not a StoryBored setting.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| "Train from photos" tab says trainer not configured | `LORA_FACTORY_DIR` unset or wrong; check Settings → health. |
| Prep rejected most photos | Dataset too uniform, blurry, or multi-subject — read the report's per-image reasons, add better photos. |
| Character doesn't look right in generations | Too few/too-similar photos; or strength too low — try raising toward 1.0 before retraining. |
| Character overpowers every shot (same pose/outfit bleeding in) | Dataset too repetitive; lower strength, and retrain with more variety. |
| Generations queued forever | A training job holds the GPU lane — that's by design. Check the job tray; cancel training if you need the GPU now. |
| Generate refused: "@x was trained for … — this engine renders with …" | The character's LoRA family doesn't match the selected engine (see [Model families](#model-families)). Switch to an engine of the character's family, remove the mention, or retrain the character with the new engine set as default. |
