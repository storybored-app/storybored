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
      config.py               # pydantic-settings, reads .env (see env vars below)
      db.py  models.py        # SQLModel engine + tables
      schemas.py              # API request/response pydantic models
      events.py               # in-process pub/sub + SSE endpoint /api/events
      api/                    # routers: projects.py scenes.py shots.py characters.py
                              #   jobs.py breakdown.py export.py settings_api.py
                              #   workflows_api.py training.py health.py media.py
      engine/                 # comfy_client.py graph.py registry.py image.py video.py
      jobs/                   # runner.py (DB-backed queue, GPU lane serialization)
      llm/                    # client.py breakdown.py  (OpenAI-compatible chat)
      training/               # lora_factory.py (trainer adapter) fetch.py (URL import)
      export/                 # animatic.py (imageio-ffmpeg)
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
  status str in draft|queued|generated|approved, picked_take_id nullable,
  video_take_id nullable
- **character**: id, name, handle unique (no @ stored), trigger, class_word="person",
  lora_name (ComfyUI dropdown name incl. subdir), lora_strength float=1.0,
  thumbnail_path nullable, notes="", status in ready|dataset|training|trained
- **shotcharacter**: shot_id, character_id (link table, refreshed from @mentions on save)
- **take**: id, shot_id FK, kind in image|video, status in pending|done|failed,
  file_path nullable, thumb_path nullable, workflow_id, params_json, seed int,
  error nullable, created_at
- **job**: id, type in image_gen|video_gen|animatic|dataset_prep|lora_train,
  status in queued|running|done|failed|cancelled, lane str ("gpu" for all v1 types),
  payload_json, result_json nullable, error nullable, progress float=0,
  detail str="" (human-readable current step), created_at, started_at, finished_at
- **setting**: key PK, value  (runtime-editable copies of LLM_* and workflow defaults;
  DB value wins over env when set)

Shot status transitions are automatic: create=draft; image_gen queued→queued;
first take done→generated; POST approve (requires picked take)→approved.

## API (all under /api, JSON; errors = {"detail": str} with proper status codes)

Projects/board:
- `GET/POST /api/projects`, `GET/PATCH/DELETE /api/projects/{id}`
  (GET single returns nested scenes+shots+takes summary — the board payload)
- `POST /api/projects/{id}/scenes` · `PATCH/DELETE /api/scenes/{id}`
- `POST /api/projects/{id}/scenes/reorder {scene_ids:[…]}`
- `POST /api/scenes/{id}/shots` · `GET/PATCH/DELETE /api/shots/{id}`
- `POST /api/scenes/{id}/shots/reorder {shot_ids:[…]}`

Generation:
- `POST /api/shots/{id}/generate {workflow_id?, n_takes?=1, params?{}}` → {job_id}
- `GET /api/shots/{id}/takes` · `POST /api/takes/{id}/pick` · `DELETE /api/takes/{id}`
- `POST /api/shots/{id}/approve` / `POST /api/shots/{id}/unapprove`
- `POST /api/shots/{id}/render-video {workflow_id?, motion_prompt?}` → {job_id}
- `POST /api/projects/{id}/render-videos {}` → queue video for every approved shot lacking one
- `POST /api/projects/{id}/animatic` → {job_id}; result_json.file_path = MP4 under DATA_DIR/exports
- `GET /api/projects/{id}/exports`

Characters:
- `GET/POST /api/characters` · `PATCH/DELETE /api/characters/{id}`
- `GET /api/characters/available-loras` → list from ComfyUI /object_info LoraLoader enum
- `POST /api/characters/import-lora` multipart (.safetensors → COMFY_LORAS_DIR) or {lora_name}
- Wizard: `POST /api/characters/wizard` multipart images[] and/or {image_urls:[…]} +
  {name, handle, trigger, class_word} → creates character(status=dataset) + dataset_prep job.
  `GET /api/training/{character_id}` → prep report (report.md text), sample paths, job states.
  `POST /api/training/{character_id}/train` → lora_train job (explicit user step after
  reviewing the prep report). On train completion: character.status=trained,
  lora_name=final checkpoint, strength=1.0 (user-adjustable).

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
  `missing_models: [str]` (validated against ComfyUI /object_info enums, cached 60s),
  plus `default: bool` (per kind, from default_image/video_workflow), the pack's baked
  `loras: [{node, lora_name, strength, baked_strength, enabled, disabled_with_character}]`
  in chain order with user overrides applied, `added_loras: [{lora_name, strength,
  enabled}]`, and `loras_modified: bool`
- `GET/PUT /api/settings` · `GET /api/health` → {comfy, llm, trainer, ffmpeg} statuses.
  JSON-valued settings, validated on PUT: `style_loras` (list of {lora_name, strength?,
  enabled?} layered into every image render) and `engine_loras` (object: pack id → list
  of baked-node overrides {node, strength?, enabled?} and/or appended {lora_name,
  strength?, enabled?})
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
  LoraLoaders (enabled:false → both strengths 0; unknown node ids ignored), then
  extra LoRAs splice at `character_injection.after_node` in call order
  characters → styles → engine additions, which yields the render chain
  **base stack → engine additions → style LoRAs → character LoRAs** (identity
  last). A malformed setting parses to empty — it must never sink a render.
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

- Single asyncio worker per lane; all v1 job types use lane "gpu" → strict serialization
  (a 3-hour lora_train naturally blocks gens; UI must make the queue visible).
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
- `/settings`: engine URL + status dot, LLM config + "test" button, trainer dir,
  workflow packs list w/ availability + missing models detail.
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
