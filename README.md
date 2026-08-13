# StoryBored

> **Storyboard your film with AI — no node graphs, no jargon.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://img.shields.io/badge/CI-GitHub_Actions-blue.svg)](.github/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](backend/pyproject.toml)

StoryBored is open-source storyboarding software for filmmakers. ComfyUI is the
invisible engine under the hood — you see a visual board of scenes and shots,
never a node graph. Write shot descriptions, generate stills, pick your favorite
takes, approve shots, render them to video, and export the whole board as an
animatic MP4.

<!-- screenshot: board -->
<!--
  Replace this block with a screenshot of the Board view:
  vertical scene sections, horizontal shot strips, status rings, job tray.
  ![The StoryBored board](docs/img/board.png)
-->

## Features

- **Visual board** — projects → scenes → shots. Drag to reorder shots and scenes.
  Every shot card shows its latest still, shot number, type, and approval status.
- **Takes, not gambles** — generate multiple takes per shot, compare them in a
  gallery, pick one, approve the shot. Nothing is locked in until you say so.
- **Image → video** — approved shots render to short video clips
  (image-to-video, with an optional motion prompt per shot).
- **Animatic export** — one click turns the board into a single MP4 in board
  order, honoring each shot's duration, with clip audio kept and stills held.
- **Characters as `@handles`** — train a character once (or import an existing
  LoRA), then just type `@sam` in any shot description. StoryBored injects the
  character into the generation automatically.
- **Script breakdown (optional)** — paste script text and let any
  OpenAI-compatible LLM draft the scene/shot list for you. Review and edit the
  draft before anything touches your board.
- **Engine packs** — generation engines are drop-in folders (a ComfyUI graph +
  a small manifest). Add your own workflow without touching StoryBored's code.
  See [docs/WORKFLOWS.md](docs/WORKFLOWS.md).
- **LoRAs without JSON surgery** — every engine's built-in LoRA stack is
  visible and editable in Settings (toggle, re-strength, append, one-click
  reset to pack defaults), you can layer global *style LoRAs* over every
  render, and pick the default engine — all stored as settings, never
  touching pack files.
- **Graceful degradation** — no LLM configured? No trainer installed? Those
  features show a friendly "configure in Settings" hint instead of crashing.

<!-- screenshot: shot-drawer -->
<!--
  Replace this block with a screenshot of the shot drawer:
  description with @mention autocomplete, takes gallery, Approve button.
  ![Shot drawer](docs/img/shot-drawer.png)
-->

## Quickstart

You need: Python 3.11+, Node 20+, and a running [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
instance (local or on another machine you can reach).

```bash
git clone https://github.com/storybored-app/storybored.git
cd storybored

# 1. Configure — point COMFYUI_URL at any ComfyUI instance
cp .env.example .env
$EDITOR .env

# 2. Backend — install into an isolated virtualenv.
#    Modern Debian/Ubuntu and Homebrew macOS mark the system Python
#    "externally managed" and refuse a bare `pip install`, so make a venv first.
python3 -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e backend

# 3. Frontend (built once, served by the backend)
npm --prefix frontend i && npm --prefix frontend run build

# 4. Run (from the same shell, with the venv still activated)
python3 -m storybored
```

Every command above runs on stock Debian/Ubuntu and macOS. The venv keeps
StoryBored's dependencies out of your system Python; activate it (`.
.venv/bin/activate`) in any new shell before running `python3 -m storybored`
again. Prefer [uv](https://docs.astral.sh/uv/)? `uv venv && uv pip install -e
backend` does the same thing.

Open <http://localhost:8600>. First run? The Projects screen starts empty —
click **Load demo project** to get "The Last Lighthouse", a small two-scene
board you can play with immediately.

> **Running on your network.** StoryBored listens on `127.0.0.1` (localhost)
> only by default, so it is reachable just from the machine it runs on. It has
> **no login and no authentication** — anyone who can reach the server can drive
> your GPU, read your media, and change settings. Only set
> `STORYBORED_HOST=0.0.0.0` (to open it to other devices) on a network you
> trust, and never expose it directly to the public internet.

## Hardware expectations

StoryBored itself is lightweight — the GPU requirements come from the engine
packs you run in ComfyUI. StoryBored ships engine *definitions*, not the
multi-gigabyte model files they load; before your first shot, make sure your
ComfyUI has the files each pack needs. **[docs/MODELS.md](docs/MODELS.md)**
explains what to download and where — and Settings lists any missing models per
pack so you know exactly what's absent.

| What you're doing                  | GPU                                        | Notes                                    |
| ---------------------------------- | ------------------------------------------ | ---------------------------------------- |
| Stills (default Krea 2 engine)     | NVIDIA, **16 GB+ VRAM**                    | 8-step distilled sampling                |
| Video (MiniMax H3 image-to-video)  | NVIDIA, more VRAM than stills — 24 GB class recommended | ~5 s clips with audio       |
| Character training (LoRA)          | NVIDIA, 24 GB class recommended            | A multi-hour job; queues behind gens     |
| Board / UI / animatic export only  | none                                       | ffmpeg is bundled via `imageio-ffmpeg`   |

ComfyUI can run on a different machine than StoryBored — set `COMFYUI_URL`
accordingly. All jobs share a single GPU lane, so a long training run simply
queues generations behind it (the UI shows you the queue).

## Going further

- **Get the model files a pack needs** — [docs/MODELS.md](docs/MODELS.md)
- **Author your own engine pack** — [docs/WORKFLOWS.md](docs/WORKFLOWS.md)
- **Train a character from photos** — [docs/TRAINING.md](docs/TRAINING.md)
- **The binding v1 spec** — [docs/CONTRACT.md](docs/CONTRACT.md)
- **Codebase tour & invariants** — [ARCHITECTURE.md](ARCHITECTURE.md)
- **Contributing (humans and coding agents)** — [CONTRIBUTING.md](CONTRIBUTING.md)

## Roadmap

- **Cloud / Runpod one-click** — templates that spin up ComfyUI + StoryBored
  together for people without a local GPU.
- **More engine packs** — additional image and video model families as drop-in
  workflow packs.
- **Collaborative boards** — multiple people on the same board at the same time.

## License

[MIT](LICENSE).
