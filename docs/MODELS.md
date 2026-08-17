# Getting the model files

StoryBored ships engine **definitions**, not model weights. An engine pack is a
small ComfyUI graph plus a manifest (see [WORKFLOWS.md](WORKFLOWS.md)) — a few
kilobytes. The actual generation is done by multi-gigabyte model files that live
in **your ComfyUI**, and those are what you download separately.

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

Unchanged from the [README](../README.md#hardware-expectations): stills on the
default Krea 2 engine want an NVIDIA card with **16 GB+ VRAM**; MiniMax H3
video wants more — **24 GB class recommended**; character training likewise
24 GB class. The board, UI and animatic export need no GPU at all.

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
