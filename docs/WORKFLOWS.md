# Authoring engine packs

An **engine pack** is how StoryBored talks to ComfyUI without users ever seeing
a node graph. A pack is a folder containing exactly two files:

```
workflows/<pack-id>/
  manifest.json   # what StoryBored needs to know about the graph
  graph.json      # a ComfyUI graph in API format
```

StoryBored scans two places for packs:

- `workflows/` inside the repo (the packs that ship with StoryBored), and
- `DATA_DIR/workflows/` (your own packs — survive upgrades, no fork needed).

Drop a folder into either location and it appears in the engine selector.
If a pack references models your ComfyUI doesn't have, StoryBored **flags it as
unavailable and lists the missing models** in Settings — it never hides the
pack or crashes.

## graph.json: the API-format export

ComfyUI has two graph formats. Packs use the **API format** — the JSON that
`POST /prompt` accepts — *not* the format from "Save" / drag-and-drop.

To export it: in ComfyUI, enable **dev mode** (Settings → "Enable Dev mode
Options"), then use **Save (API Format)**. You get a JSON object keyed by node
id:

```json
{
  "3": {
    "class_type": "KSampler",
    "inputs": {
      "seed": 42,
      "steps": 8,
      "model": ["4", 0],
      "positive": ["6", 0],
      "latent_image": ["5", 0]
    }
  },
  "6": {
    "class_type": "CLIPTextEncode",
    "inputs": { "text": "a lighthouse at dusk", "clip": ["4", 1] }
  }
}
```

Two things to internalize:

- **Node ids are the top-level keys** (`"3"`, `"6"`, …). They are strings, they
  are arbitrary, and they are what the manifest points at.
- **Links are `[node_id, output_index]` pairs.** `"model": ["4", 0]` means
  "input `model` comes from output 0 of node 4". This is how StoryBored splices
  character LoRAs into your graph (see below).

### Finding the node ids you need

Open your exported `graph.json` and search by `class_type`:

- the **prompt** usually lives on a `CLIPTextEncode` node, input `text`;
- the **seed** on your sampler (`KSampler` → `seed`, or `RandomNoise` →
  `noise_seed` in advanced-sampler graphs);
- **width/height** on `EmptyLatentImage` (or the video node itself);
- the **output** is your `SaveImage` / `SaveVideo` node.

Tip: give nodes memorable titles in the ComfyUI editor before exporting — the
API export keeps only ids, so keep the editor-format `.json` around as your
"source" and re-export after edits.

## manifest.json, field by field

```json
{
  "id": "krea2-realism",
  "name": "Krea 2 — Realism stack",
  "kind": "image",
  "description": "Photoreal stills, 8-step distilled sampling.",
  "graph": "graph.json",
  "parameters": [
    {"key": "prompt", "label": "Prompt", "type": "prompt", "node": "6", "input": "text"},
    {"key": "seed",   "type": "seed", "node": "3", "input": "seed"},
    {"key": "width",  "type": "int",  "node": "5", "input": "width",  "default": 1728},
    {"key": "height", "type": "int",  "node": "5", "input": "height", "default": 1152}
  ],
  "output_node": "9",
  "character_injection": {"after_node": "lora_7", "disable_nodes": ["lora_3", "lora_4"]},
  "required_models": {
    "UNETLoader.unet_name": ["krea2_raw_fp8_scaled.safetensors"]
  }
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Unique slug. Must match the folder name. Stored on takes (`take.workflow_id`) so renames orphan history — pick it once. |
| `name` | yes | Human label shown in the engine selector. |
| `kind` | yes | `"image"` or `"video"`. Determines which UI offers the pack and which GPU mode command runs before the job (`COMFY_MODE_IMAGE_CMD` / `COMFY_MODE_VIDEO_CMD`). |
| `description` | no | One or two sentences shown under the name. |
| `graph` | yes | Filename of the API-format graph, relative to the pack folder. Conventionally `graph.json`. |
| `parameters` | yes | The knobs StoryBored is allowed to turn. Each entry maps a UI parameter onto one node input: the engine literally does `graph[node]["inputs"][input] = value`. Fields per entry: `key` (unique within the pack), optional `label` (UI text; defaults to the key), `type` (see below), `node` (node id string), `input` (input name on that node), optional `default`. |
| `output_node` | yes | Id of the `SaveImage` / `SaveVideo` node. StoryBored rewrites its `filename_prefix` per take and reads results from ComfyUI history for this node. |
| `character_injection` | no | Where `@character` LoRAs get spliced in (image packs; see next section). Omit it and the pack simply ignores characters. |
| `lora_injection` | no | Where user-appended LoRAs splice in when the pack has no `character_injection` (video packs): `{"after_node": "1", "class_type": "LoraLoaderModelOnly"}`. `class_type` defaults to `LoraLoader`; model-only loaders take over only the model path (no clip). |
| `model_slots` | no | Loader inputs users may swap from Settings: `[{"key": "unet", "label": "Base model", "node": "1", "input": "unet_name"}]`. The UI lists the engine's dropdown enum for each slot; choices are stored in the `engine_models` setting and written onto the input at render time. |
| `frame_conditioning` | no | Video packs whose sampler accepts both a first- and a last-frame image: `{"node": "6", "first": "first_frame", "last": "last_frame"}`. Enables the shot-level "still anchors first/last frame" toggle — "last" moves whatever feeds the first input onto the last input. |
| `required_models` | no | Map of `"<ClassType>.<input_name>"` → list of model filenames the graph needs. Validated against ComfyUI `/object_info` enums (cached 60 s); misses mark the pack unavailable with the missing names listed. |

### Parameter types

| `type` | UI | Notes |
| --- | --- | --- |
| `prompt` | the shot's description textarea | `@handles` are replaced with each character's `"{trigger} {class_word}"` before the value is written into the graph. Every image pack should have exactly one. |
| `seed` | hidden | StoryBored assigns a fresh random seed per take and stores it on the take, so takes are reproducible. |
| `int` / `float` | number field | Use `default` to pre-fill. |
| `string` | text field | Free text (e.g. a style suffix). |
| `image` | hidden (video packs) | StoryBored uploads the shot's approved still via `POST /upload/image` and writes the uploaded name here. |

### Video packs

A `kind: "video"` manifest adds two conventional parameters:

```json
{"key": "first_frame", "type": "image", "node": "5", "input": "image"},
{"key": "length",      "type": "int",   "node": "6", "input": "length"}
```

`first_frame` receives the approved still; `length` is the clip length in
frames. The shot's `motion_prompt` feeds the pack's `prompt` parameter. See
`workflows/minimax-h3-i2v/` for the shipped example (LoadImage →
image-to-video node → sampler → video+audio decode → `SaveVideo`).

Video packs can also declare `lora_injection` (users append video LoRAs from
Settings), `model_slots` (swap the video UNET for a finetune), and
`frame_conditioning` (let the still anchor the END of the clip instead of the
start) — see the manifest field table above and the shipped minimax pack,
which uses all three.

## Character injection, explained

Characters in StoryBored are LoRAs. When a shot mentions `@sam`, the engine
splices Sam's LoRA into your graph at generation time — your pack just declares
*where*:

```json
"character_injection": {"after_node": "lora_7", "disable_nodes": ["lora_3", "lora_4"]}
```

- **`after_node`** — the node whose MODEL/CLIP outputs the character LoRA
  should hang off. Usually the *last* `LoraLoader` in your style chain (or the
  checkpoint/UNET loader if you have no chain). Mechanically, for each shot
  character the engine:
  1. inserts a new `LoraLoader` node whose `model`/`clip` inputs point at
     `[after_node, 0]` / `[after_node, 1]`, with the character's `lora_name`
     and `lora_strength`;
  2. rewires every *other* node that referenced `[after_node, 0]` or
     `[after_node, 1]` to read from the new node instead;
  3. chains additional characters in sequence (character 2 hangs off
     character 1's node, and so on).
- **`disable_nodes`** — node ids whose `strength_model`/`strength_clip` are set
  to `0` while any character LoRA is active. Use this for style/aesthetic LoRAs
  that fight trained identities (in the shipped `krea2-realism` pack, two
  "beauty" LoRAs are disabled whenever a character is in the shot). The nodes
  stay in the graph, so shots *without* characters keep the full style stack.

Prompt side: `@sam` in the description becomes Sam's `"{trigger} {class_word}"`
(e.g. `"zxsam person"`) before the prompt is written into the graph — the
trigger token is what the LoRA was trained to respond to.

## Runtime LoRA layers: what users can change without touching your pack

Your pack's files are never edited by the app. Instead, Settings stores three
JSON settings that are applied to the graph at render time, so users can tune
an engine from the UI and always get back to your defaults with one click:

- **Per-engine edits** (Settings → Engines → expand a row): every `LoraLoader`
  in your graph is listed in chain order with its strength. Users can toggle
  any of them off (both strengths forced to 0 — render-identical to removing
  the node), change strengths, or **append** extra LoRAs to the engine. Stored
  in the `engine_loras` setting keyed by pack id; overrides reference your
  node ids, so renaming a node in a pack update simply makes stale overrides
  inert (unknown ids are ignored, never fatal).
- **Style LoRAs** (Settings → Style LoRAs): a global list layered into *every*
  image render regardless of engine, each with its own on/off toggle and
  strength. Stored in the `style_loras` setting.
- **Model swaps** (Settings → Engines → expand a row → Model): each
  `model_slots` entry becomes a dropdown of the engine's installed files, so
  users can run your pack on a different base model or finetune. Stored in the
  `engine_models` setting keyed by pack id; unknown slot keys are ignored,
  never fatal.

Both kinds of extra LoRA splice into the graph at your
`character_injection.after_node` — the same seam characters use (packs without
characters, e.g. video packs, declare the seam as `lora_injection` instead).
The final chain order is always:

```
your baked stack → engine additions → style LoRAs → character LoRAs → sampler
```

Character LoRAs come last on purpose: identity wins over style. Two
consequences for pack authors:

1. `after_node` is the seam for *all* runtime LoRA insertion, not just
   characters — put it after the last LoRA you ship, even in a pack that
   doesn't target characters.
2. A pack without `character_injection` still gets per-node strength/off
   overrides, but users can't append LoRAs to it unless it declares a
   `lora_injection` seam of its own.

The Engines list also marks which pack is the **default** per kind (used
whenever a shot doesn't pick an engine explicitly) and lets users change it —
backed by the `default_image_workflow` / `default_video_workflow` settings.

## Worked example: packaging your own txt2img workflow

Say you have a plain SDXL-style workflow you like. Export it in API format:

```json
{
  "1": {"class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "my_model.safetensors"}},
  "2": {"class_type": "CLIPTextEncode",
        "inputs": {"text": "PROMPT HERE", "clip": ["1", 1]}},
  "3": {"class_type": "CLIPTextEncode",
        "inputs": {"text": "blurry, low quality", "clip": ["1", 1]}},
  "4": {"class_type": "EmptyLatentImage",
        "inputs": {"width": 1216, "height": 832, "batch_size": 1}},
  "5": {"class_type": "KSampler",
        "inputs": {"seed": 0, "steps": 25, "cfg": 6.5,
                   "sampler_name": "euler", "scheduler": "normal", "denoise": 1,
                   "model": ["1", 0], "positive": ["2", 0],
                   "negative": ["3", 0], "latent_image": ["4", 0]}},
  "6": {"class_type": "VAEDecode",
        "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
  "7": {"class_type": "SaveImage",
        "inputs": {"filename_prefix": "storybored", "images": ["6", 0]}}
}
```

Reading the graph: prompt = node `"2"` input `text`; seed = node `"5"` input
`seed`; size = node `"4"`; output = node `"7"`; characters should hang off the
checkpoint loader, node `"1"`. So:

```json
{
  "id": "my-sdxl",
  "name": "My SDXL look",
  "kind": "image",
  "description": "My favorite general-purpose look.",
  "graph": "graph.json",
  "parameters": [
    {"key": "prompt", "label": "Prompt", "type": "prompt", "node": "2", "input": "text"},
    {"key": "seed",   "type": "seed", "node": "5", "input": "seed"},
    {"key": "width",  "type": "int",  "node": "4", "input": "width",  "default": 1216},
    {"key": "height", "type": "int",  "node": "4", "input": "height", "default": 832}
  ],
  "output_node": "7",
  "character_injection": {"after_node": "1"},
  "required_models": {
    "CheckpointLoaderSimple.ckpt_name": ["my_model.safetensors"]
  }
}
```

Put both files in `DATA_DIR/workflows/my-sdxl/`, refresh Settings → the pack
shows up, availability-checked against your ComfyUI. That's the whole process.

## Checklist before sharing a pack

- [ ] `graph.json` is **API format** (top-level keys are node ids, not a
      `nodes` array).
- [ ] Manifest `id` matches the folder name; every `parameters[].node` /
      `input` exists in the graph.
- [ ] `output_node` is the save node; leave its `filename_prefix` as-is
      (StoryBored overwrites it per take).
- [ ] `required_models` covers every model file the graph loads — this is what
      gives users a useful "missing: …" message instead of a cryptic ComfyUI
      error.
- [ ] `character_injection.after_node` points at the *last* LoRA in your chain —
      it's the seam for characters, style LoRAs, and user-appended LoRAs alike
      (see "Runtime LoRA layers" above).
- [ ] No absolute paths, hostnames, or personal LoRA names in either file.
