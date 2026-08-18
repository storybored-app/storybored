# Getting the model files

StoryBored ships engine **definitions**, not model weights. An engine pack is a
small ComfyUI graph plus a manifest (see [WORKFLOWS.md](WORKFLOWS.md)) — a few
kilobytes. The actual generation is done by multi-gigabyte model files that live
in **your ComfyUI**, and those are what you download separately.

## What can my GPU do?

The setup wizard reads your GPU's VRAM from the engine, sorts you into a
**tier**, and recommends the verified engine for it — this table is that
logic on paper. Every recommendation is a preselection, never a lock: any
pack your ComfyUI can run stays selectable. Speed numbers are sourced
community/vendor measurements on the named card, not promises.

| VRAM | Tier | Recommended stills engine | Recommended video engine | Downloads | License | Measured speed |
| --- | --- | --- | --- | --- | --- | --- |
| under 6 GB | `board` | — | — | — | — | boards, script AI and animatic export work without a GPU |
| 6–11 GB | `stills-lite` | **Z-Image Turbo** (`z-image-turbo`) | — | ~12.2 GB | Apache 2.0 | ~15–20 s per 1024² still (RTX 4060, with offloading at 6–8 GB) |
| 12–15 GB | `stills` | **Z-Image Turbo** (`z-image-turbo`) | **Wan 2.2 5B** (`wan22-ti2v-5b`) | ~12.2 GB + ~18.1 GB | Apache 2.0 | stills under 10 s (RTX 3060 12 GB) |
| 16–23 GB | `stills-hd` | **Krea 2** (`krea2-basic`) | **Wan 2.2 5B** (`wan22-ti2v-5b`) | ~19.1 GB + ~18.1 GB | Krea 2 Community (see notice) | ~13 s per 1024² still at 8 steps (RTX 3090) |
| 24 GB+ | `studio` | **Qwen-Image 2512** (`qwen-image-2512`) | **Wan 2.2 14B** (`wan22-i2v-14b`) | ~30.9 GB + ~38.0 GB | Apache 2.0 | stills ~34–55 s with the Lightning LoRA (RTX 4090D, official Comfy numbers) |

Notes the table can't hold:

- **Z-Image Turbo** is also the *fast, license-clean* alternative at 16 GB+ —
  roughly twice the speed of Krea 2 per still (~2.3 s on an RTX 4090), Apache
  licensed, with the largest post-2025 LoRA training ecosystem. Its bf16
  build (12.3 GB) is a catalog alternate for 16 GB cards.
- **Wan 2.2 video is silent** — no audio track. The MiniMax H3 pack generates
  synchronized audio but carries a serious license notice (US/EU/UK/South
  Korea territory exclusion) and its default file runs only on RTX 50-series
  cards; it remains available as a clearly labeled power option.
- **Wan 2.2 5B on 8–11 GB cards**: its official floor is "fits on 8 GB with
  native offloading". The wizard only *recommends* it from 12 GB up to avoid
  over-promising — on an 8 GB card, try it manually.
- **Wan 2.2 14B at ~20 steps** takes ~4 m 20 s per 5 s clip on an RTX 4090;
  the shipped pack bakes the official lightx2v 4-step LoRAs, which cut the
  step count 20 → 4 (toggle them off in Settings to run the slow path — then
  raise steps to 20 and cfg to 3.5).
- **Character training** wants a 24 GB-class card regardless of tier.
- Licensing philosophy: recommendations are **safe-by-default** — Apache 2.0
  wherever a tier has an Apache winner. Engines with license caveats stay
  available but wear a visible notice (`license_note`) in Settings and the
  wizard.

### Writing-assistant (LLM) sizing

The wizard also suggests an Ollama model for the writing assistant, by the
same VRAM reading (the LLM shares the GPU with renders — set
`llm_keep_alive` to `0` in Settings to free its VRAM immediately after each
call):

| Best GPU VRAM | Suggested tag | Download | Notes |
| --- | --- | --- | --- |
| under 12 GB (or no GPU) | `qwen3.5:4b` | 3.4 GB | 256K context; fine on CPU |
| 12–31 GB | `qwen3.5:9b` | 6.6 GB | 256K context; the default |
| 32 GB+ | `qwen3.5:35b-a3b` | 24 GB | MoE, 3B active — fast decode |

**Settings is your shopping list.** If a pack references a file your ComfyUI
can't see, StoryBored marks the pack **unavailable** and lists each missing file
in **Settings** with everything the catalog knows about it: the destination
folder, the size, the license, and — for files with a verified source — a
direct link. Two ways to get a file installed:

- **In-app download.** Set the **engine models folder** in Settings (the
  `COMFY_MODELS_DIR` env var — ComfyUI's `models/` directory; only possible
  when StoryBored runs on the same machine/filesystem as ComfyUI). Every
  missing file with a verified source then gets a **Download** button, and
  "Download all missing" fetches the lot. Downloads run on their own job lane,
  so they never block renders, and each file's size is verified against the
  catalog.
- **Manual placement.** Follow the source link (or the search guidance for
  community files), download the file yourself, and drop it into the folder
  shown. Keep the filename **exactly** as listed — availability matches by
  filename.

Either way: once the file is in place, restart or refresh ComfyUI so it
re-scans, then hit **Refresh** on the Settings engines panel — the pack flips
to available.

## Where ComfyUI keeps each kind of file

The manifests key each requirement by the ComfyUI node that loads it. That node
tells you which standard ComfyUI models folder the file belongs in:

| Loader node in the manifest | ComfyUI folder |
| --- | --- |
| `UNETLoader` | `models/diffusion_models` (a.k.a. `models/unet`) |
| `CLIPLoader` | `models/text_encoders` (a.k.a. `models/clip`) |
| `VAELoader` | `models/vae` |
| `LoraLoader` / `LoraLoaderModelOnly` | `models/loras` |
| `CheckpointLoaderSimple` | `models/checkpoints` |

ComfyUI's own documentation is the authority on installing models and on where
each folder lives: <https://docs.comfy.org/>.

## The catalog

`workflows/catalog.json` maps each filename the shipped packs need to its
source. Entries with a URL were **verified against the hosting site** (exact
path, byte size, license) — those power the in-app downloader. Entries marked
*community* are community-published files with no canonical home; we don't
link them, and the notes say where to look. You can extend or override the
catalog by dropping a `catalog.json` with the same shape into
`DATA_DIR/workflows/` (useful for your own packs — same filename wins).

The tables below summarize the shipped catalog. Sizes are the verified byte
counts, rounded.

### Krea 2 image packs (`krea2-basic`, `krea2-realism`)

Both packs build on the same base + turbo-distill LoRA. `krea2-basic` needs
only the first four rows; `krea2-realism` adds the community realism stack.

**Base files** — verified, hosted by the **Comfy-Org** organization on Hugging
Face in [`Comfy-Org/Krea-2`](https://huggingface.co/Comfy-Org/Krea-2), all
under the Krea 2 Community License:

| File | Folder | Size |
| --- | --- | --- |
| `krea2_raw_fp8_scaled.safetensors` | `diffusion_models` | 13.1 GB |
| `qwen3vl_4b_fp8_scaled.safetensors` | `text_encoders` | 5.2 GB |
| `qwen_image_vae.safetensors` | `vae` | 254 MB |

**Turbo distill LoRA** (both packs) — community, but with a known page:
`(Krea 2) 8-Step Turbo Distill Rank 64 V2026.1.safetensors` (~469 MB,
inherits the Krea 2 Community License) is the "Krea-2 Turbo 8-Step
Distillation LoRA (SVD Extract)" [on Civitai](https://civitai.com/models/2746698),
version V2026.1. Civitai downloads need a (free) logged-in account, so this
one is manual — keep the filename exactly as-is, parentheses and spaces
included. Folder: `loras`.

**Realism LoRA stack** (`krea2-realism` only) — all community files, folder
`loras`. These circulate on Hugging Face, Civitai and Tensor.Art under exactly
these filenames; we can't point at a canonical, licensed home for any of them,
so search the hubs yourself and sanity-check what you grab (a real LoRA is
megabytes to gigabytes — beware byte-sized placeholder reuploads):

| File | Known origin |
| --- | --- |
| `krea2filterbypass3.safetensors` | none found — search hubs; 160-byte stub reuploads exist under this name |
| `Krea2-realism-V2.safetensors` | reuploads on Hugging Face |
| `Detailer-KREA2.safetensors` | none found — search hubs |
| `bloomgirls-ultrarealism-krea2_4k.safetensors` | Tensor.Art ("[KR2]BloomGirls UltraRealism") |
| `lenovo_krea2.safetensors` | Civitai ("Lenovo Ultrareal", Krea 2 build) |
| `RealisticSnapshotKrea2.safetensors` | Civitai ("Realistic Snapshot", Krea 2 build) |
| `realism_engine_krea2_v3.1.safetensors` | reuploads on Hugging Face |

If you'd rather not chase the stack, start with `krea2-basic` — and remember
you can toggle individual realism LoRAs off in Settings; a LoRA you've
disabled is no longer required for the pack to count as available.

### Z-Image Turbo (`z-image-turbo`)

All verified, hosted by **Comfy-Org** in
[`Comfy-Org/z_image_turbo`](https://huggingface.co/Comfy-Org/z_image_turbo),
all **Apache 2.0** — the whole pack is one-click downloadable:

| File | Folder | Size |
| --- | --- | --- |
| `z_image_turbo_int8_convrot.safetensors` | `diffusion_models` | 6.2 GB |
| `qwen_3_4b_fp8_mixed.safetensors` | `text_encoders` | 5.6 GB |
| `ae.safetensors` | `vae` | 335 MB |

Catalog alternates (swap via the pack's *Base model* slot in Settings):
`z_image_turbo_bf16.safetensors` (12.3 GB, full precision, for 16 GB+ cards)
and `z_image_turbo_nvfp4.safetensors` (4.5 GB, **RTX 50-series/Blackwell
only**). Needs ComfyUI ≥ 0.3.75 (Z-Image nodes are core since then).

### Qwen-Image 2512 (`qwen-image-2512`)

All verified: model files in
[`Comfy-Org/Qwen-Image_ComfyUI`](https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI),
the Lightning LoRA in
[`lightx2v/Qwen-Image-Lightning`](https://huggingface.co/lightx2v/Qwen-Image-Lightning);
the entire stack is **Apache 2.0**:

| File | Folder | Size |
| --- | --- | --- |
| `qwen_image_2512_fp8_e4m3fn.safetensors` | `diffusion_models` | 20.4 GB |
| `qwen_2.5_vl_7b_fp8_scaled.safetensors` | `text_encoders` | 9.4 GB |
| `qwen_image_vae.safetensors` | `vae` | 254 MB — **same file the Krea 2 packs use**, one download serves both |
| `Qwen-Image-Lightning-8steps-V2.0-bf16.safetensors` | `loras` | 850 MB |

Needs ComfyUI ≥ 0.3.49 (Qwen-Image nodes are core since then). In practice
fp8 model + fp8 text encoder sit around 86% of a 24 GB card.

### Wan 2.2 video packs (`wan22-ti2v-5b`, `wan22-i2v-14b`)

All verified, hosted by **Comfy-Org** in
[`Comfy-Org/Wan_2.2_ComfyUI_Repackaged`](https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged),
all **Apache 2.0**. Both packs share the text encoder; the VAEs differ
(that's a Wan quirk, not a mistake):

| File | Folder | Size | Used by |
| --- | --- | --- | --- |
| `wan2.2_ti2v_5B_fp16.safetensors` | `diffusion_models` | 10.0 GB | 5B |
| `wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors` | `diffusion_models` | 14.3 GB | 14B |
| `wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors` | `diffusion_models` | 14.3 GB | 14B |
| `wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors` | `loras` | 1.2 GB | 14B |
| `wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors` | `loras` | 1.2 GB | 14B |
| `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | `text_encoders` | 6.7 GB | both |
| `wan2.2_vae.safetensors` | `vae` | 1.4 GB | 5B |
| `wan_2.1_vae.safetensors` | `vae` | 254 MB | 14B |

The 14B pack loads its two expert models **sequentially** (high-noise for
the early steps, low-noise for the late ones), so a 24 GB card never holds
both at once. Wan 2.2 output is **silent** — the animatic exporter gives
those clips silence, like stills. Wan nodes are ComfyUI core (summer 2025+).

### MiniMax H3 video pack (`minimax-h3-i2v`)

Animates an approved still into a short clip with generated audio. Text
encoder and VAEs are verified in the official
[`Comfy-Org/MiniMax-H3`](https://huggingface.co/Comfy-Org/MiniMax-H3) Hugging
Face repo; all four files are under the MiniMax H3 Community License
Agreement:

| File | Folder | Size | Source |
| --- | --- | --- | --- |
| `minimax_h3_fl2va_pruned_nvfp4.safetensors` | `diffusion_models` | 12.5 GB | community NVFP4 repo (verified, see note) |
| `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | `text_encoders` | 15.7 GB | Comfy-Org/MiniMax-H3 |
| `minimax_h3_video_vae_fp16.safetensors` | `vae` | 5.2 GB | Comfy-Org/MiniMax-H3 |
| `minimax_h3_audio_vae_fp32.safetensors` | `vae` | 605 MB | Comfy-Org/MiniMax-H3 |

**Note on the video UNET:** the official Comfy-Org repo ships bf16 /
fp8_scaled / int8_convrot variants under *different* filenames; the exact
NVFP4 file this pack references lives in community quantization repos on
Hugging Face (the catalog links a size-verified one; byte-identical mirrors
exist). If you use a different variant, swap the pack's video model in
Settings — availability follows your choice.

**Prefer quantized builds.** This is a hard-won lesson, not a guess: an
unquantized bf16 video UNET around 32 GB forces massive VRAM offload on a
24 GB-class card, while the quantized build of the same model runs fully
on-GPU. When `COMFY_MODELS_DIR` is set, StoryBored stats the files behind the
model dropdowns and warns on anything over 24 GB.

## VRAM expectations

See [What can my GPU do?](#what-can-my-gpu-do) at the top — that matrix *is*
the sizing guide, and the setup wizard applies it automatically. The board,
UI and animatic export need no GPU at all.

## Custom nodes are a separate requirement

Model files aren't the only thing a pack can miss: the graphs also use node
classes your ComfyUI may not have (a custom node pack, or a core node newer
than your install). Settings reports those separately as *missing custom
nodes* — see [WORKFLOWS.md](WORKFLOWS.md#custom-nodes-what-availability-checks-and-what-the-shipped-packs-need)
for what the shipped packs assume and which node packs provide it.

## Bring your own models

None of these files are special to StoryBored — they're ordinary ComfyUI model
files, and the default packs are just one possible setup. If you already have a
ComfyUI you generate with, the fastest path is to **wrap your own workflow as a
pack**: run `python -m storybored validate-pack --write` to derive its
`required_models` from the graph, and skip the downloads above entirely.
[WORKFLOWS.md](WORKFLOWS.md) walks through authoring a pack (including a plain
SDXL example) step by step. Add a `DATA_DIR/workflows/catalog.json` entry per
file if you want your own packs' missing-model lists to carry links and sizes
too. StoryBored will run any pack whose referenced files are present in your
ComfyUI — nothing more.
