# Training characters

A **character** in StoryBored is a LoRA plus a little metadata: a display name,
an `@handle` you type in shot descriptions, a **trigger token** the LoRA
answers to, a class word ("person", "dog", "robot"…), and a strength. You can
get one two ways:

- **Import an existing LoRA** — Characters → New character → *Import existing
  LoRA*. Pick one your ComfyUI already knows about, or upload a
  `.safetensors` file (requires `COMFY_LORAS_DIR` to be set so StoryBored can
  copy it where ComfyUI looks). Set the trigger yourself — it's whatever the
  LoRA was trained with.
- **Train from photos** — the wizard described below. Requires a trainer
  (see [Prerequisites](#prerequisites)).

## Consent first

If you're training on a **real person**, get their explicit consent before you
collect a single photo — and honor it if they later change their mind (delete
the dataset *and* the trained LoRA). Don't train on photos of strangers,
public figures, or anyone who hasn't agreed. For fictional characters, use
images you have the rights to. StoryBored won't police this for you; it's on
you.

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
2. Point StoryBored at it in `.env`:

   ```
   LORA_FACTORY_DIR=/path/to/lora-factory
   ```

3. Restart StoryBored. Settings → trainer should show a green status.

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

Time expectations: prep is minutes; training is **hours** (roughly 2.5–4 h for
a full run on a modern 24 GB-class GPU — start it before bed, not before a
review meeting).

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
