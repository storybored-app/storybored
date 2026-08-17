# StoryBored — build contract (v1)

Single source of truth for the design of StoryBored. Contributors (human or
coding agent) follow this exactly: it is the binding spec for the v1 API,
data model, and module boundaries.

## What StoryBored is

Open-source storyboarding software for filmmakers. ComfyUI is the invisible engine;
users see a visual board, never a node graph. Projects → scenes → shots; shots
generate stills (multiple takes, pick one, approve); approved shots render to video
(image-to-video); the whole board exports as an animatic MP4. Characters are trained
LoRAs referenced by `@handle` in shot descriptions. Optional LLM drafts the shot list
from script text. Everything modular: engines are "workflow packs" (ComfyUI graph +
manifest), trainers and LLMs are pluggable adapters, all machine config in `.env`.

## Repo layout (fixed — do not invent other top-level dirs)

```
storybored/
  README.md  LICENSE(MIT)  CONTRIBUTING.md  ARCHITECTURE.md  .env.example  .gitignore
  backend/
    pyproject.toml            # project name "storybored", python >=3.11
    storybored/
      main.py                 # FastAPI app, includes ALL routers, serves ../frontend/dist statically at /
                              #   (dist missing → a small "run npm build" help page, not a silent 404)
      config.py               # pydantic-settings, reads .env (see env vars below)
      db.py  models.py        # SQLModel engine + tables
      schemas.py              # API request/response pydantic models
      events.py               # in-process pub/sub + SSE endpoint /api/events
      api/                    # routers: projects.py scenes.py shots.py characters.py
                              #   jobs.py breakdown.py export.py settings_api.py
                              #   workflows_api.py training.py health.py media.py
                              #   lifecycle.py (.storybored export/import/download)
      engine/                 # comfy_client.py graph.py registry.py image.py video.py
      jobs/                   # runner.py (DB-backed queue, GPU lane serialization)
      llm/                    # client.py breakdown.py  (OpenAI-compatible chat)
      training/               # lora_factory.py (trainer adapter) fetch.py (URL import)
      export/                 # animatic.py (imageio-ffmpeg) + archive.py (.storybored)
      seed/                   # demo.py + demo script text (original content, written fresh)
    tests/                    # pytest; fake ComfyUI + fake LLM via local aiohttp/starlette test servers
  frontend/                   # Vite + React 18 + TypeScript + Tailwind v4
  workflows/                  # engine packs, see manifest spec
    krea2-basic/       manifest.json graph.json
    krea2-realism/     manifest.json graph.json
    minimax-h3-i2v/    manifest.json graph.json
  docs/CONTRACT.md  docs/WORKFLOWS.md  docs/TRAINING.md
  scripts/dev.sh              # runs backend (reload) + vite dev concurrently
```

`.gitignore`: `data/`, `.env`, `node_modules/`, `dist/`, `__pycache__/`, `.venv/`, `*.safetensors`.

## Env vars (.env.example, all with these exact names)

```
STORYBORED_PORT=8600
DATA_DIR=./data                      # sqlite db + media + exports live here
COMFYUI_URL=http://127.0.0.1:8188
COMFY_LORAS_DIR=                     # optional: where to copy imported character LoRAs
COMFY_MODELS_DIR=                    # optional: ComfyUI's models/ dir (shared filesystem
                                     # only) — enables the in-app model downloader +
                                     # big-model size warnings; expanduser'd
COMFY_MODE_IMAGE_CMD=                # optional shell cmd before image jobs (profile switchers)
COMFY_MODE_VIDEO_CMD=                # optional shell cmd before video jobs
COMFY_FLUSH_CMD=                     # optional shell cmd between model-family switches
LLM_BASE_URL=                        # OpenAI-compatible, e.g. http://127.0.0.1:11434/v1
LLM_API_KEY=
LLM_MODEL=
LORA_FACTORY_DIR=                    # optional: path to a lora-factory checkout
```
Unset optional vars = feature gracefully degrades (UI shows "not configured", never crashes).

## Data model (SQLModel; sqlite at DATA_DIR/storybored.db)

- **project**: id, title, description="", aspect_ratio="16:9", created_at, updated_at
- **scene**: id, project_id FK, idx int, title, slugline="", description=""
- **shot**: id, scene_id FK, idx int, description="", shot_type="" (free text: WIDE, MED, CU…),
  camera="", dialogue="", duration_s float=4.0, motion_prompt="" (for video pass),
  frame_position str="first" (first|last — whether the picked still opens or closes
  the video clip), status str in draft|queued|generated|approved,
  picked_take_id nullable, video_take_id nullable
- **character**: id, name, handle unique (no @ stored), trigger, class_word="person",
  lora_name (ComfyUI dropdown name incl. subdir), lora_strength float=1.0,
  thumbnail_path nullable, notes="", status in ready|dataset|training|trained
- **shotcharacter**: shot_id, character_id (link table, refreshed from @mentions on save)
- **take**: id, shot_id FK, kind in image|video, status in pending|done|failed,
  file_path nullable, thumb_path nullable, workflow_id, params_json, seed int,
  error nullable, created_at
- **job**: id, type in image_gen|video_gen|animatic|dataset_prep|lora_train|lora_shootout
  |project_export|model_download, status in queued|running|done|failed|cancelled,
  lane str ("gpu" for every GPU type — the single-GPU-lane invariant; "io" for
  project_export and model_download, so archive writes and multi-GB fetches never
  block renders), project_id nullable (set at enqueue whenever the job belongs to
  a project — image_gen/video_gen/animatic/project_export; character + training
  jobs stay null; soft reference, no FK), payload_json, result_json nullable,
  error nullable, progress float=0, detail str="" (human-readable current step),
  created_at, started_at, finished_at

Foreign keys are enforced (`PRAGMA foreign_keys=ON` on every sqlite connection):
scene→project, shot→scene, take→shot and shotcharacter links cannot outlive
their parents. Deletes remove children before parents.

project/scene/shot/take use SQLite AUTOINCREMENT so ids are never reused after
a delete — media paths embed these ids, and a reused id would silently adopt
any file left behind for its predecessor (external backups, pre-cleanup
installs). Applies to databases created after this change.
- **setting**: key PK, value  (runtime-editable copies of LLM_* and workflow defaults;
  DB value wins over env when set)

Shot status transitions are automatic: create=draft; image_gen queued→queued;
first take done→generated; POST approve (requires picked take)→approved.

## API (all under /api, JSON; errors = {"detail": str} with proper status codes)

Projects/board:
- `GET/POST /api/projects`, `GET/PATCH/DELETE /api/projects/{id}`
  (GET single returns nested scenes+shots+takes summary — the board payload)
- DELETE semantics (no orphans on disk): deleting a **take** unlinks its
  files; deleting a **shot** or **scene** unlinks all its takes' files;
  deleting a **project** additionally cancels the project's queued/running
  jobs (via job.project_id), deletes its job rows, removes the whole
  `media/{id}` and `exports/{id}` trees, and clears any character thumbnail
  that pointed into them. Characters are global and never deleted with a
  project. All unlinks resolve under DATA_DIR (is_relative_to guard).
- `POST /api/projects/{id}/scenes` · `PATCH/DELETE /api/scenes/{id}`
- `POST /api/projects/{id}/scenes/reorder {scene_ids:[…]}`
- `POST /api/scenes/{id}/shots` · `GET/PATCH/DELETE /api/shots/{id}`
- `POST /api/scenes/{id}/shots/reorder {shot_ids:[…]}`

Generation:
- `POST /api/shots/{id}/generate {workflow_id?, n_takes?=1, params?{}}` → {job_id}
- `GET /api/shots/{id}/takes` · `POST /api/takes/{id}/pick` · `DELETE /api/takes/{id}`
- `POST /api/shots/{id}/approve` / `POST /api/shots/{id}/unapprove`
- `POST /api/shots/{id}/render-video {workflow_id?, motion_prompt?, frame_position?}` →
  {job_id}; motion_prompt/frame_position persist onto the shot before the job queues.
  Pre-flights the resolved video pack exactly like the image path: 503 when the
  engine is unreachable, 409 listing missing models/nodes — never enqueues blind.
  The pack resolves as explicit id → `default_video_workflow` setting → first
  video pack by id (no hardcoded default). `render-videos` runs the same gate
  once for the whole batch.
- `POST /api/projects/{id}/render-videos {}` → queue video for every approved shot lacking one
- `POST /api/projects/{id}/animatic` → {job_id}; result_json.file_path = MP4 under DATA_DIR/exports
- `GET /api/projects/{id}/exports`

Project archives (.storybored — full spec in the section below):
- `POST /api/projects/{id}/export` → {job_id} — a `project_export` job on lane
  `"io"` (non-GPU: runs alongside renders, never queues behind training).
  Writes `DATA_DIR/exports/{id}/project-{id}.storybored`; result_json carries
  file_path, size_bytes and download_url. Progress/detail like any job.
- `GET /api/projects/{id}/export/download` → the archive as a file download
  (resolve + is_relative_to traversal guard, same idiom as /api/media).
- `POST /api/projects/import` multipart `{file, mode?=merge|rename}` → 201
  `{project, warnings: [str], characters: {linked: [handle…], created:
  [handle…], renamed: {old: new}}}`. Creates a brand-new project (never
  overwrites); all IDs remapped, media re-homed under the new ids,
  picked/video take pointers patched to the new take rows.
  Character handling: **merge** (default) links existing characters by
  case-insensitive handle and creates missing ones from the manifest;
  **rename** keeps imported characters separate — colliding handles get a
  numeric suffix (`ava` → `ava2`) and `@mentions` in shot descriptions and
  motion prompts are rewritten. Missing LoRA files or engine packs are
  returned as `warnings`, never failures. 400 on: bad zip, missing/invalid
  manifest, newer schema_version, unsafe member paths (zip-slip).

Characters:
- `GET/POST /api/characters` · `PATCH/DELETE /api/characters/{id}`
- `GET /api/characters/available-loras` → list from ComfyUI /object_info LoraLoader enum
- `POST /api/characters/import-lora` multipart (.safetensors → COMFY_LORAS_DIR) or {lora_name}
- `POST /api/characters/{id}/generate-thumbnail {workflow_id?, prompt?}` → {job_id};
  requires lora_name; character_thumb job renders one square (1024²) portrait through
  the full LoRA pipeline (default prompt: head-and-shoulders, concrete wardrobe stated)
  and sets character.thumbnail_path (media/characters/{id}/portrait_{job}.png + _thumb)
- Wizard: `POST /api/characters/wizard` multipart images[] and/or {image_urls:[…]} +
  {name, handle, trigger, class_word} → creates character(status=dataset) + dataset_prep job.
  `GET /api/training/{character_id}` → prep report (report.md text), sample paths, job states.
  `POST /api/training/{character_id}/train` → lora_train job (explicit user step after
  reviewing the prep report). On train completion: character.status=trained,
  lora_name=final checkpoint, strength=1.0 (user-adjustable).
- Checkpoint shootout (optional post-train quality pass, character must be trained):
  `POST /api/training/{character_id}/shootout {strengths?, ckpts?, seeds?}` → {job_id}
  (409 while one is queued/running or when no checkpoints exist; 400 on malformed knobs).
  `GET /api/training/{character_id}/shootout/grid` → the comparison contact sheet (jpeg).
  `POST /api/training/{character_id}/shootout/apply {checkpoint, strength}` → repoints
  character.lora_name at `lorafactory_<job>/<checkpoint>` + sets strength (filename must
  be one of the job's own checkpoint files; 0 < strength ≤ 2). `GET /api/training/{id}`
  also returns `shootout_job`.

LLM / PromptSmith (results land in visible editor fields; nothing persisted here):
- `POST /api/shots/{id}/enhance {description?, shot_type?, camera?}` →
  {description} — rough notes → one polished image prompt (@handles preserved,
  one nudge retry, then 502)
- `POST /api/shots/{id}/generate-motion {description?, shot_type?, camera?,
  dialogue?, motion_prompt?, frame_position?}` → {motion_prompt} — everything the
  shot knows → one MiniMax i2v motion prompt ending in an "Audio:" line;
  frame-position-aware (still opens the clip vs. clip arrives at the still);
  @handles from the author's own rough motion notes survive or 502

LLM / breakdown:
- `POST /api/breakdown {project_id, script_text}` (synchronous, timeout 300s) →
  `{scenes:[{title, slugline, shots:[{description, shot_type, camera, dialogue,
  duration_s, characters:[handle…]}]}]}` — a DRAFT, nothing persisted.
- `POST /api/projects/{id}/apply-breakdown {draft}` → creates scenes/shots (appends).

Infra:
- `GET /api/jobs?status=` · `GET /api/jobs/{id}` · `POST /api/jobs/{id}/cancel`
- `GET /api/events` — SSE. Event types: `job` (full job row on any change),
  `shot` (shot row on status/take change), `take`, `character`. data = JSON row.
- `GET /api/workflows` → registry with per-workflow `available: bool` +
  `missing_models: [str]` + `missing_nodes: [str]` (validated against ComfyUI
  /object_info, cached 60s; `?refresh=true` drops the cache first — the Settings
  "Refresh" button). Availability checks the **effective** model set: an
  `engine_models` slot swap replaces the baked filename in the required set, and
  a baked LoRA toggled off via `engine_loras` drops out of it. `missing_nodes`
  lists graph node classes (plus manifest `required_nodes` extras) the engine
  doesn't have — a missing custom node pack, distinct from missing model files.
  Each missing file also appears in `missing_models_info: [{filename, folder,
  downloadable, source?, page?, size_bytes?, license?, notes?}]`, enriched from the
  model catalog (`workflows/catalog.json`, merged with `DATA_DIR/workflows/
  catalog.json` — user entries win per filename): destination ComfyUI folder
  (from the loader class), verified download URL + byte size + license when the
  catalog has them, honest search guidance (`notes`, no URL) for
  community-sourced files. When `comfy_models_dir` is set, each `models` slot row
  additionally carries `large_files: [str]` — dropdown options whose on-disk size
  exceeds 24 GB (the documented offload lesson; stat failures are silently skipped).
  Also per workflow: `default: bool` (per kind, from default_image/video_workflow), the pack's baked
  `loras: [{node, lora_name, strength, baked_strength, enabled, disabled_with_character}]`
  in chain order with user overrides applied, `added_loras: [{lora_name, strength,
  enabled}]`, `loras_modified: bool`, the pack's swappable
  `models: [{key, label, node, input, value, baked, options}]` (options = the engine's
  dropdown enum for that loader input) with `models_modified: bool`, and capability
  flags `supports_loras` (pack declares a LoRA splice point) +
  `supports_frame_position` (video pack can anchor the still as the LAST frame)
- `POST /api/workflows/{id}/download-models {filenames?: [str]}` →
  `{job_ids, queued, skipped}` — enqueue one `model_download` job (lane "io") per
  missing file that has a verified catalog source, streamed into
  `{comfy_models_dir}/{folder}/{filename}` (`.part` staging, size verified when the
  catalog knows it, /object_info cache flushed on completion). Files without a
  verified source come back in `skipped`. Filenames already queued/running are not
  double-queued. 409 when `comfy_models_dir` is unset, 404 unknown pack, 503 when
  the engine is unreachable (can't compute what's missing).
- `GET/PUT /api/settings` · `GET /api/health` → {comfy, llm, trainer, ffmpeg} statuses.
  Probes are STRICT — "ok" means the response looked like the right service, not
  merely that something answered: comfy requires /system_stats to return 200 with
  JSON containing a "system" key; llm requires {base}/models to return 200 JSON.
  Status vocabulary: `ok` | `unreachable` (connect failed) | `unrecognized`
  (answered, but not the expected service) | `error` (5xx) | `not_configured`;
  trainer: `ok` | `missing` | `not_configured` (path is ~-expanded); ffmpeg: the
  resolved binary path | `missing`.
  Runtime-editable (DB wins over env) keys: comfyui_url (PUT flushes the
  /object_info cache), llm_base_url, llm_api_key, llm_model, lora_factory_dir,
  comfy_loras_dir (where imported character LoRA uploads are copied; ~-expanded),
  comfy_models_dir (base ComfyUI models directory; enables in-app downloads +
  size warnings), default_image_workflow, default_video_workflow, style_loras,
  engine_loras, engine_models, plus `setup_complete` (no env twin; "1" once the
  first-run setup wizard finished — the UI stops auto-offering the wizard after
  that).
- `GET /api/setup/probe` — one-shot deep probe for the setup wizard. Optional
  query params `comfy_url` / `llm_url` / `trainer_dir` probe CANDIDATE values
  without persisting anything (omitted → effective settings). Returns
  `{comfy: {status, url, gpus: [{name, vram_gb|null}], tier},
  llm: {status, url, models: [id…]}, trainer: {status, dir}, ffmpeg,
  workflows: [{id, name, kind, available, missing_models}] (only when comfy ok),
  tiers: {stills_min_vram_gb: 16, video_min_vram_gb: 24}}`.
  GPU rows come straight from ComfyUI /system_stats (never invented; unknown
  VRAM → null). `tier` ∈ board|stills|video: best-GPU VRAM rounded to whole GiB,
  ≥24 → video (video engines + training-class), ≥16 → stills, else/no GPU/engine
  down → board (board, script breakdown and animatic assembly still work).
  JSON-valued settings, validated on PUT: `style_loras` (list of {lora_name, strength?,
  enabled?} layered into every image render), `engine_loras` (object: pack id → list
  of baked-node overrides {node, strength?, enabled?} and/or appended {lora_name,
  strength?, enabled?}), and `engine_models` (object: pack id → {slot key: model
  filename} written onto the pack's `model_slots` loader inputs at render time)
- `GET /api/media/{path}` — serves files under DATA_DIR (path-traversal-safe!)

## Workflow packs (the modularity story — docs/WORKFLOWS.md explains for users)

`workflows/<id>/manifest.json`:
```json
{
  "id": "krea2-realism", "name": "Krea 2 — Realism stack", "kind": "image",
  "description": "…", "graph": "graph.json",
  "parameters": [
    {"key":"prompt","label":"Prompt","type":"prompt","node":"6","input":"text"},
    {"key":"seed","type":"seed","node":"3","input":"seed"},
    {"key":"width","type":"int","node":"5","input":"width","default":1728},
    {"key":"height","type":"int","node":"5","input":"height","default":1152}
  ],
  "output_node": "9",
  "character_injection": {"after_node":"lora_7", "disable_nodes":["lora_3","lora_4"]},
  "required_models": {"UNETLoader.unet_name":["krea2_raw_fp8_scaled.safetensors"], "...": []}
}
```
- graph.json = ComfyUI **API format** graph (the kind POST /prompt accepts).
- Video manifests add `{"key":"first_frame","type":"image","node":"5","input":"image"}`
  and `{"key":"length","type":"int","node":"6","input":"length"}`.
- Engine applies params by writing `graph[node]["inputs"][input] = value`.
- **Character injection** (engine/graph.py): for each shot character, insert a
  `LoraLoader` node after `after_node`: new node's model/clip inputs ← after_node
  outputs 0/1; then every OTHER node that referenced `[after_node,0]`/`[after_node,1]`
  is rewired to the new node. Chain multiple characters in sequence.
  `disable_nodes`: set strength_model/strength_clip to 0 while a character LoRA is
  active (style LoRAs that fight identity). Prompt text: replace each `@handle` with
  `"{trigger} {class_word}"` before writing the prompt param.
- **Runtime LoRA layers** (engine/graph.py, all DB-settings-driven so pack files
  stay pristine): `engine_loras` node overrides are written onto the baked
  LoRA loaders (enabled:false → strengths 0; unknown node ids ignored), then
  extra LoRAs splice at `character_injection.after_node` in call order
  characters → styles → engine additions, which yields the render chain
  **base stack → engine additions → style LoRAs → character LoRAs** (identity
  last). A malformed setting parses to empty — it must never sink a render.
  Video packs declare `lora_injection {after_node, class_type?}` instead
  (minimax uses `LoraLoaderModelOnly` after the UNET loader — no clip path);
  `engine_loras` appends/overrides apply there the same way.
- **Model slots** (engine/graph.py): manifest `model_slots [{key, label, node,
  input}]` names swappable loader inputs (e.g. the UNET); the `engine_models`
  setting ({pack id → {key: filename}}) is written onto those inputs at render
  time in image_gen, character_thumb and video_gen. Unknown keys ignored.
- **Frame conditioning** (video packs): manifest `frame_conditioning {node,
  first, last}`; shot.frame_position="last" moves the sampler's first-frame
  image input onto the last-frame input so the clip ends on the still.
- Users add engines by dropping a folder into `workflows/` (also scanned:
  `DATA_DIR/workflows/`). Registry validates required_models against /object_info
  and flags unavailable packs in the UI instead of hiding them.

## ComfyUI client (engine/comfy_client.py)

- `POST {COMFYUI_URL}/prompt` with `{"prompt": graph, "client_id": uuid}`.
- Track completion by polling `GET /history/{prompt_id}` (1s interval); progress via
  `GET /queue` position. (No websocket dependency — simplest robust path.)
- Fetch outputs: `GET /view?filename=&subfolder=&type=output`, save under
  `DATA_DIR/media/{project}/{shot}/take_{id}.png|.mp4`; make ~384px thumbs (Pillow;
  for video grab first frame via imageio).
- Upload input images (video first-frame): `POST /upload/image` multipart.
- Set each graph's SaveImage/SaveVideo `filename_prefix` to `storybored/take_{take_id}`
  so history outputs are unambiguous.
- Errors: /history node_errors or exception → take.status=failed with the message.

## Job runner (jobs/runner.py)

- Single asyncio worker per lane; every ComfyUI/trainer job type uses lane "gpu" →
  strict serialization (a 3-hour lora_train naturally blocks gens; UI must make the
  queue visible). Lane "io" runs local-disk jobs (`project_export`) that must never
  queue behind GPU work. New lanes are allowed for non-GPU work only.
- Jobs persisted in DB; on startup, `running` jobs → `failed` ("interrupted by restart"),
  `queued` jobs resume. Cancellation: cooperative flag checked between steps; for
  subprocess jobs also terminate the process group; for comfy jobs POST /queue delete +
  /interrupt.
- Mode switching: runner tracks last job family (image/video). On switch, run
  COMFY_FLUSH_CMD then COMFY_MODE_{IMAGE|VIDEO}_CMD (if set) via
  `asyncio.create_subprocess_shell`, log output into job.detail. After a mode switch,
  poll ComfyUI /system_stats until reachable (max 120s) before submitting.
- Every job state change publishes to events bus → SSE.

## Animatic (export/animatic.py)

imageio-ffmpeg static binary (never assume system ffmpeg; expose resolved path in
/api/health). For each shot in board order: video take if present else picked image
take else SKIP (log in result). Normalize: scale+pad to project resolution (16:9 →
1920×1080), 24fps, yuv420p. Respect shot.duration_s: trim longer clips; freeze last
frame to pad shorter ones; stills hold for duration_s. Keep clip audio; stills get
silence. Concat (filter_complex or intermediate TS segments — implementer's choice),
write `DATA_DIR/exports/{project_id}/animatic_{timestamp}.mp4`, store as result.

## Project archives (.storybored format)

A `.storybored` file is a plain zip (schema_version **1**):

```
manifest.json          # see below
media/{pid}/{sid}/...  # the project's takes (stills/clips/thumbs), stored
                       # under their DATA_DIR-relative paths at export time
exports/{pid}/*.mp4    # finished animatics
workflows/{id}/...     # bundled USER engine packs (DATA_DIR/workflows only)
```

manifest.json keys:
- `format`: `"storybored-project"` · `schema_version`: int, currently 1 —
  import refuses anything newer than it supports, older versions must stay
  importable · `app_version`: exporting StoryBored version · `exported_at`
- `project`: the full nested board payload (project → scenes → shots → takes),
  exactly what `GET /api/projects/{id}` returns
- `characters`: **soft references** for every character cast in the project
  (via shotcharacter): name, handle, trigger, class_word, lora_name,
  lora_strength, notes, status, thumbnail_path. LoRA **weight files are never
  bundled** — lora_name is an external reference the importing machine must
  have installed in its engine. Thumbnails are machine-local media, also not
  bundled.
- `workflow_packs`: `{builtin: [id…], bundled: [id…], not_installed: [id…]}` —
  packs referenced by the project's takes. Repo-shipped packs are noted by id
  only; user packs (from DATA_DIR/workflows) are bundled under `workflows/`
  in the zip and extracted on import **only if** that pack id isn't already
  installed.

Import is two-pass: insert project/scenes/shots, then takes, then patch each
shot's picked_take_id/video_take_id onto the new take ids. Every extracted
member resolves under DATA_DIR (resolve + is_relative_to — the same guard as
media serving; a traversal member aborts the import with 400 and the
half-imported trees are removed).

## LLM breakdown (llm/)

OpenAI-compatible `POST {LLM_BASE_URL}/chat/completions`. System prompt: professional
1st AD breaking a script into scenes/shots; MUST return only JSON matching the draft
schema (include the schema in the prompt); known characters list (handles) passed in so
it can tag them. Parse defensively: strip code fences, json.loads, on failure one retry
with "return only valid JSON". Temperature 0.3. If LLM_BASE_URL unset → 503 with clear
detail; UI hides the feature behind a "configure in Settings" hint.

## Trainer adapter (training/lora_factory.py)

Wraps an external lora-factory checkout (LORA_FACTORY_DIR); absent → trainer features
degrade gracefully. Dataset images: uploads + `fetch.py` URL downloads (10MB cap each,
content-type check, max 60) → `{LORA_FACTORY_DIR}/../inbox/{handle}` equivalent under
DATA_DIR staging, then:
- dataset_prep job: `./prep.sh <staging_dir> --name <handle>-v1 --trigger <trigger>
  --class-word <class_word>` (cwd=LORA_FACTORY_DIR, stream stdout tail into job.detail).
  On done: read `jobs/<job>/report.md` for the wizard's review screen.
- lora_train job: `tmux`-free: `bash train.sh <job>` as subprocess (survives via job
  runner process; note in docs that closing StoryBored kills training v1 limitation —
  UNLESS trivially avoidable via start_new_session=True + pidfile reattach, then do that).
  Progress: parse step counts from stdout lines when present (`progress = step/3000`).
  On success: `lora_name = "lorafactory_<job>/<job>.safetensors"` (final checkpoint),
  character.status=trained + `character` SSE event.
- lora_shootout job: `{factory}/.venv/bin/python compare.py <job> --strengths …
  [--ckpts …] [--seeds N]` (renders every requested checkpoint through the image engine
  → `output/<job>/comparison/grid.jpg`; `[n/total]` stdout lines drive progress), then
  `score.py <job>` (facenet likeness vs the training set + local VLM judge →
  `comparison/scores.md`). The ranked table is parsed into result_json
  `{results: [{rank, checkpoint, label, strength, total, likeness, prompt_match, clean,
  no_face, cells}], scores_md, grid}` so the wizard offers one-click apply. Runs in the
  "image" ComfyUI family (runner mode-switches first); user cancel kills the process
  group. The final checkpoint stays wired until the user applies a different one.

## Frontend (the product — invest here)

Stack: Vite, React 18, TypeScript, Tailwind v4, @tanstack/react-query, react-router,
@dnd-kit (drag reorder), native EventSource for SSE (one hook invalidating query keys).
Dev proxy `/api` → localhost:8600. Build output frontend/dist served by backend.

Look: dark cinematic UI ("editing suite at midnight") — near-black charcoal surfaces,
one warm accent (amber/gold), clean sans (Inter via @fontsource), generous spacing,
subtle borders not shadows, film-slate iconography (lucide-react). Polished empty
states with one-line explanations — non-technical users must never see jargon
(no "ComfyUI", "LoRA", "workflow" in primary UI copy; say "engine", "character",
"style"). Toasts for errors, skeletons while loading.

Routes:
- `/` Projects: card grid + New Project (title, aspect). Health banner if engine down.
- `/p/:id` **Board**: vertical scene sections, each a horizontal strip of shot cards
  (16:9 thumbnail of picked/latest take, shot number "1A", shot_type chip, status
  ring: gray draft / pulsing amber queued / blue generated / green approved, duration).
  Drag to reorder shots within+across scenes and scenes themselves. "+ shot"/"+ scene".
  Header: project title, Import Script, Render Videos, Export Animatic, job tray.
- Shot drawer (right panel, opens on card click): description textarea with @mention
  autocomplete (popup listing characters w/ thumbnails), shot_type/camera/dialogue/
  duration fields (autosave, debounced), engine select + params (from manifest),
  takes: Generate (n takes stepper), gallery grid, lightbox, pick ★, Approve button,
  video tab: motion_prompt + Render Video + video player of video take.
- `/characters`: card grid (thumb, name, @handle, status badge). "New character":
  tab A "Import existing LoRA" (pick from available-loras or upload file, set trigger/
  strength, upload thumbnail); tab B "Train from photos" wizard: step 1 name/handle/
  trigger(auto-suggest rare token)/class word; step 2 add 20–40 photos (drag-drop grid
  + "add from URLs" textarea, live count + guidance text); step 3 prep progress →
  report summary (kept/rejected counts) → big "Start training (~3h)" button;
  step 4 training progress bar + "GPU is busy training — generations will queue".
- `/p/:id/script`: paste script textarea → "Break it down" → editable draft preview
  (scenes/shots table, checkboxes) → "Add to board". Unconfigured LLM → friendly
  pointer to Settings.
- `/p/:id/export`: approved-shots checklist w/ video status, per-shot render buttons,
  Render All, then Animatic section: Export button, job progress, download link +
  inline player when done.
- `/settings`: engine URL + LoRA folder + status dots, LLM config + "test"
  button, trainer dir + "test", workflow packs list w/ availability + missing
  models detail, link to the setup wizard.
- `/setup`: first-run **setup wizard** — auto-offered (once per app load) when
  `setup_complete` is unset AND the engine health isn't ok; always reachable
  from Settings and the health banner. Steps: path choice ("I have an engine" /
  "I need to install one" / "no GPU — boards only") → engine URL + Test
  (GPU/VRAM/tier + pack availability via /api/setup/probe with candidate
  params) → LLM (model dropdown from the probe; skippable) → trainer dir
  (skippable) → summary; Finish PUTs only the settings the user actually set,
  plus `setup_complete=1`.
- Feature gating matches what /api/health reports: the train-from-photos tab
  shows a "configure in Settings" panel when the trainer isn't ok (never a
  wizard that 503s after photo upload), and Enhance / motion-draft buttons are
  disabled with an explanatory tooltip when the LLM isn't ok.
- Global: bottom-right **job tray** (SSE-live): queued/running jobs, progress bars,
  cancel buttons; collapses to a pill with count.

## Tests / quality bar

- Backend: pytest, in-memory sqlite; fake ComfyUI server fixture (starlette app:
  /prompt, /history, /view, /object_info, /upload/image) — CRUD, board payload,
  mention parsing + graph splice (assert rewired links), image job e2e against fake,
  animatic with 2 tiny generated clips (ffmpeg color source), breakdown against fake
  LLM, path traversal rejected on /api/media.
- Frontend: `tsc --noEmit` + `vite build` clean; that's the bar (no component tests v1).
- Lint: ruff (backend). Keep pyproject deps minimal: fastapi, uvicorn[standard],
  sqlmodel, pydantic-settings, httpx, pillow, imageio-ffmpeg, imageio, python-multipart,
  sse-starlette. Dev: pytest, pytest-asyncio, ruff, respx (optional).

## Seed / demo

`storybored --demo` (or first-run button "Load demo project"): creates "The Last
Lighthouse" demo — an ORIGINAL two-scene mini-script (written fresh for this repo,
~6 shots, no copyrighted text) with shot types/durations filled, no characters, no
generated media. Seed code in backend/storybored/seed/demo.py.

## README (repo front door)

Hero one-liner, screenshot placeholder block, feature list, quickstart (clone →
cp .env.example .env → point COMFYUI_URL at any ComfyUI → `pip install -e backend`
→ `npm --prefix frontend i && npm --prefix frontend run build` → `python -m
storybored` → open :8600), workflow-pack authoring pointer, training pointer,
hardware expectations table, roadmap (cloud/Runpod templates, more engines), MIT.
CONTRIBUTING.md + ARCHITECTURE.md written for both humans and coding agents
(hivemind style: explicit file map, invariants, how to add an engine/adapter).
