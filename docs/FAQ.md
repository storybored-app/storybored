# FAQ

Honest answers to the questions we expect (and the skepticism we'd have too).

## Is this just a ComfyUI wrapper?

ComfyUI does the generating; StoryBored does the filmmaking — the board, takes,
approvals, characters, continuity, and the animatic at the end. Whether that's
"just a wrapper" is for you to judge, but it's not a black box:

- Every engine is a plain ComfyUI graph plus a small manifest, sitting in a
  folder you can read ([WORKFLOWS.md](WORKFLOWS.md)).
- You can import your own ComfyUI workflow through the Settings wizard and it
  becomes an engine.
- You can export any engine **back out** as standard ComfyUI API JSON — with
  your model swaps and LoRA edits applied — and drop it straight into ComfyUI.

If you outgrow StoryBored, you leave with working graphs, not lock-in.

## How is this different from Comfy MCP / driving ComfyUI with an AI agent?

Comfy MCP is great, and frankly it validates the premise: Comfy themselves are
saying node graphs shouldn't be the interface. But MCP is a *control plane* —
you bring an agent client (and in practice, usually a hosted frontier model),
and each session is a conversation.

StoryBored is a *place your film lives*. Projects → scenes → shots → takes →
picks → approvals is state you come back to for weeks, not a chat log.
Characters are a product feature (`@handle` casting, train-from-photos, scored
checkpoint shootouts), continuity is machinery (per-scene looks, master
plates), and the output is an actual animatic MP4. It also runs entirely
offline with a local LLM — no agent subscription anywhere in the loop.

Short version: MCP answers "how do I run a workflow without the graph."
StoryBored answers "where does my film live." An MCP surface for StoryBored's
own API is an obvious future step — the two compose rather than compete.

## Does anything leave my machine?

No. No account, no telemetry, no cloud calls. Generation happens in your
ComfyUI, the writing assistant is any OpenAI-compatible endpoint (the README
steers you to local [Ollama](https://ollama.com)), and the server binds to
`127.0.0.1` unless you opt out. The only network traffic is model files *you*
choose to download from the verified catalog.

## Do I need an expensive GPU?

No. The tiers are honest ([MODELS.md](MODELS.md)):

- **No GPU / under 6 GB** — boards, script breakdown, and animatic export
  still work. Generation doesn't.
- **6–11 GB** — Apache-licensed stills (Z-Image Turbo, with offloading).
- **12 GB+** — stills fast, plus silent video (Wan 2.2 5B).
- **16–24 GB+** — the full menu, including video with audio as a labeled
  power option.

The setup wizard reads your actual VRAM and recommends accordingly; the app
shows *measured* render speed per engine on your machine, and flags engines
that would page themselves to death instead of letting you find out the hard
way.

## Do I need an API key or a subscription?

No. MIT-licensed app, your GPU, a free local LLM. If you'd rather point the
writing assistant at a hosted API you can, but nothing requires it.

## Can I use my own models and workflows?

Yes — that's most of the Settings screen. Swap the base model per engine,
toggle or re-strength every baked LoRA, layer your own style LoRAs, import a
ComfyUI workflow export as a new engine. Pack files on disk are never
modified, so your edits survive updates.

## What's the deal with model licenses?

We verify and surface; you choose. Every catalog entry records its source and
license, tier defaults prefer Apache 2.0 wherever a tier has an Apache-clean
winner, and the exceptions are labeled in the app itself — Krea 2's community
license (revenue cap) and MiniMax H3's territory exclusions (US/EU/UK/South
Korea) are stated where you select them, not buried here.

## Can ComfyUI live on a different machine?

Yes. Point `COMFYUI_URL` (or the Settings field) at any reachable ComfyUI — a
LAN box, a rented GPU. StoryBored itself is light and runs anywhere Python
does.

## Does it run on Windows?

Yes, natively — board, rendering, script AI, animatics. The one exception is
the train-a-character-from-photos pipeline, which shells out to bash: run
StoryBored inside WSL2 if you want training, or stay native and import
ready-made LoRAs (that works fine).

## How does character consistency actually work?

Type `@sam` in a shot description. StoryBored substitutes the character's
trigger phrase into the prompt and splices their LoRA into the ComfyUI graph
at render time. Get the LoRA by importing one you already have, or by training
from 20–40 photos in the wizard — after training, a *shootout* renders the
saved checkpoints at several strengths, scores them for likeness, and lets you
apply the winner with one click. Details in [TRAINING.md](TRAINING.md).

## Is this replacing storyboard artists?

For productions that hire boards artists — no, and it shouldn't. StoryBored is
previz for the far larger group who were never going to commission boards:
shorts, student films, pitches, music videos, one-person crews. The realistic
alternative to StoryBored isn't a hired artist; it's a shot list in a
spreadsheet and no boards at all.

## Can I contribute?

Please — see [CONTRIBUTING.md](../CONTRIBUTING.md). Engine packs for models we
haven't covered are especially welcome: they're data, not code, so you can
contribute one without touching Python.
