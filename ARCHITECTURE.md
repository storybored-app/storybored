# StoryBored architecture

Audience: humans **and** coding agents. This file is the map; the binding spec
for API shapes, data model, and behavior is [docs/CONTRACT.md](docs/CONTRACT.md)
— when this file and the contract disagree, the contract wins.

## The system in one paragraph

A FastAPI backend owns a SQLite database (projects → scenes → shots →
takes), a DB-backed job queue with a **single GPU lane**, and adapters to
three external things: **ComfyUI** (generation), an **OpenAI-compatible LLM**
(script breakdown), and a **lora-factory checkout** (character training). A
React SPA (built to `frontend/dist`, served statically by the backend) renders
the board and stays live via one SSE stream. Generation engines are data, not
code: "workflow packs" under `workflows/` pair a ComfyUI API-format graph with
a manifest that names the tunable inputs.

## File map

```
storybored/
├── README.md                    # front door; quickstart
├── CONTRIBUTING.md              # how to work on this repo (humans + agents)
├── ARCHITECTURE.md              # this file
├── LICENSE                      # MIT
├── .env.example                 # every env var, documented; copy to .env
├── scripts/dev.sh               # backend (reload) + vite dev, concurrently
├── docs/
│   ├── CONTRACT.md              # ★ binding v1 spec (API, data model, behavior)
│   ├── WORKFLOWS.md             # engine-pack authoring guide
│   └── TRAINING.md              # character training guide
├── workflows/                   # shipped engine packs (also scanned: DATA_DIR/workflows/)
│   ├── catalog.json             #   model catalog: filename → verified source/size/license
│   ├── krea2-basic/             #   image: UNET + distill lora only
│   ├── krea2-realism/           #   image: full realism lora stack
│   └── minimax-h3-i2v/          #   video: image-to-video + audio
│       ├── manifest.json        #   parameters, output node, character_injection
│       └── graph.json           #   ComfyUI API-format graph
├── backend/
│   ├── pyproject.toml           # project "storybored", py>=3.11; dev extra = pytest/ruff
│   ├── storybored/
│   │   ├── main.py              # FastAPI app; includes ALL routers; serves frontend/dist at /
│   │   ├── __main__.py          # `python -m storybored` / `storybored` entrypoint (+ --demo)
│   │   ├── config.py            # pydantic-settings; reads .env (see .env.example)
│   │   ├── db.py                # SQLModel engine/session; sqlite at DATA_DIR/storybored.db
│   │   ├── models.py            # tables: project scene shot character shotcharacter take job setting
│   │   ├── schemas.py           # API request/response models
│   │   ├── events.py            # in-process pub/sub + SSE endpoint /api/events
│   │   ├── settings_store.py    # DB-override-over-env setting resolution (import-light, no FastAPI)
│   │   ├── casting.py           # @mention → shotcharacter sync, shared by api/engine/import
│   │   ├── api/                 # one router per resource, all under /api
│   │   │   ├── projects.py scenes.py shots.py characters.py
│   │   │   ├── jobs.py generate.py breakdown.py render.py settings_api.py
│   │   │   ├── workflows_api.py training.py health.py media.py
│   │   │   ├── lifecycle.py     # .storybored archive export/import/download
│   │   │   └── preflight.py     # shared availability gate for the generate endpoints
│   │   ├── engine/
│   │   │   ├── comfy_client.py  # POST /prompt, poll /history, fetch /view, upload images
│   │   │   ├── graph.py         # param application + character LoRA splice (pure functions)
│   │   │   ├── registry.py      # pack discovery + effective-availability validation
│   │   │   ├── catalog.py       # model catalog loading + big-model guardrail
│   │   │   ├── download.py      # model_download job (io lane, streams into COMFY_MODELS_DIR)
│   │   │   ├── validate.py      # `python -m storybored validate-pack` offline linter
│   │   │   ├── image.py         # image_gen job logic (takes, thumbs)
│   │   │   └── video.py         # video_gen job logic (first-frame upload)
│   │   ├── jobs/                # runner.py (DB-backed queue, one asyncio worker per lane)
│   │   │                        # + registry.py (job-type → handler registration)
│   │   ├── llm/                 # client.py (OpenAI-compat chat) + breakdown.py (prompt/parse)
│   │   ├── training/            # lora_factory.py (subprocess adapter) + fetch.py (URL import)
│   │   ├── export/
│   │   │   ├── animatic.py      # imageio-ffmpeg concat; DATA_DIR/exports output
│   │   │   └── archive.py       # .storybored zip: export job (lane "io") + import
│   │   └── seed/demo.py         # "The Last Lighthouse" demo project (original content)
│   └── tests/                   # pytest; FAKE ComfyUI + FAKE LLM as local test servers
└── frontend/                    # Vite + React 18 + TS + Tailwind v4; build output frontend/dist
```

Runtime state lives under `DATA_DIR` (default `./data`, gitignored): the
SQLite DB, `media/{project}/{shot}/take_*.png|.mp4` + thumbs, `exports/`,
training staging, and user workflow packs.

## Invariants — do not break these

1. **Single GPU lane.** Every GPU job type (`image_gen`, `video_gen`,
   `animatic`, `dataset_prep`, `lora_train`) runs on lane `"gpu"`, one at a
   time, strictly serialized by `jobs/runner.py`. This is the concurrency
   model for the whole app — a 3-hour training run *should* block generations.
   Never add a second GPU consumer or a bypass path; add new lanes only for
   non-GPU work (the `io` lane exists for exactly that: model downloads and
   other network/disk jobs run there without touching the GPU queue).
2. **The workflow-pack contract is the only engine coupling.** Backend code
   never hardcodes node ids, model names, or graph shapes. Everything
   graph-specific comes from `manifest.json` (`parameters`, `output_node`,
   `character_injection`, `required_models`). New engine = new folder, zero
   code changes.
3. **No machine-specific facts in the repo.** No hostnames, IPs, usernames,
   personal LoRA/model names, or site-local paths — in code, docs, tests,
   graphs, or commit messages. Machine config enters exclusively via `.env`
   (documented in `.env.example`).
4. **Adapters degrade gracefully.** Unset `LLM_BASE_URL` / `LORA_FACTORY_DIR` /
   unreachable ComfyUI must never crash the app or hide the rest of the UI:
   API returns 503 with a clear `detail`, `/api/health` reports the component
   as unconfigured/down, UI shows a "configure in Settings" hint. Packs with
   missing models are flagged unavailable, never hidden.
5. **Tests never touch the network.** ComfyUI and the LLM are faked by local
   test servers in `backend/tests/`. Anything that would call a real service
   gets a fake first.
6. **State changes publish events.** Any mutation of jobs/shots/takes/
   characters publishes to `events.py` → SSE; the frontend relies on this
   instead of polling.
7. **Media serving is path-traversal-safe.** `/api/media/{path}` must resolve
   inside `DATA_DIR` — keep the tests for this green.
8. **Shot status transitions are automatic** (draft → queued → generated →
   approved per the contract). Don't add manual status mutation endpoints.

## Key flows (where to look)

- **Generate takes**: `api/shots.py` → job row (`image_gen`) → `jobs/runner.py`
  picks it up → `engine/image.py` → `engine/graph.py` (apply params, splice
  characters, substitute `@handle` → trigger phrase) → `engine/comfy_client.py`
  (submit, poll history, download, thumbnail) → take rows + SSE.
- **Mode switching**: runner tracks last job family (image/video); on change
  runs `COMFY_FLUSH_CMD` then `COMFY_MODE_{IMAGE|VIDEO}_CMD` (if set), then
  waits for ComfyUI `/system_stats` before submitting.
- **Script → board**: `api/breakdown.py` → `llm/breakdown.py` (schema-in-prompt,
  defensive JSON parse, one retry) → draft returned, nothing persisted →
  user edits → `apply-breakdown` appends scenes/shots.
- **Training**: `api/characters.py` wizard → staging under `DATA_DIR` →
  `training/lora_factory.py` runs `prep.sh` / `train.sh` as subprocesses in
  `LORA_FACTORY_DIR`, streaming stdout into `job.detail`, parsing step counts
  into `job.progress`.
- **Animatic**: `export/animatic.py`, ffmpeg binary from `imageio-ffmpeg`
  (never the system ffmpeg); board order, per-shot duration honored.

## How to extend

### Add an engine (no code)

Drop `manifest.json` + API-format `graph.json` into `workflows/<id>/` (or
`DATA_DIR/workflows/<id>/`). Full guide: [docs/WORKFLOWS.md](docs/WORKFLOWS.md).
The registry picks it up and validates `required_models` against ComfyUI.

### Add a trainer adapter

`training/lora_factory.py` is the reference: an adapter exposes dataset-prep
and train as subprocess-backed job handlers, reports availability into
`/api/health`, and degrades to "not configured" when its env var is unset.
A new trainer = a sibling module with the same surface, selected via config —
keep job types (`dataset_prep`, `lora_train`) and their status/progress
semantics identical so the wizard UI works unchanged.

### Add / swap the LLM

`llm/client.py` speaks the OpenAI-compatible `/chat/completions` API, so most
local and hosted providers already work by setting `LLM_BASE_URL` /
`LLM_API_KEY` / `LLM_MODEL` (runtime-editable in Settings; DB value wins over
env). A genuinely different protocol means a new client behind the same
`chat(messages) -> str` surface used by `llm/breakdown.py`.

### Add an API endpoint

Router module in `api/`, request/response models in `schemas.py`, include the
router in `main.py`, error shape `{"detail": str}`, publish SSE events on
mutations, add a test against the fakes. Check the contract first — if the
endpoint changes the v1 surface, it's a contract change (see CONTRIBUTING.md).
