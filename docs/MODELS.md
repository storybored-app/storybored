# Getting the model files

StoryBored ships engine **definitions**, not model weights. An engine pack is a
small ComfyUI graph plus a manifest (see [WORKFLOWS.md](WORKFLOWS.md)) — a few
kilobytes. The actual generation is done by multi-gigabyte model files that live
in **your ComfyUI**, and those are what you download separately. StoryBored
never bundles or fetches them; it just asks your ComfyUI to run the graph, and
your ComfyUI needs the referenced files present.

**You do not need paths.** StoryBored refers to models only by filename. Your
job is to place each file in the folder ComfyUI already scans for that kind of
model. If a pack references a file your ComfyUI can't see, StoryBored marks the
pack **unavailable** and lists the exact missing filenames in **Settings** —
that list is your shopping list.

## Where ComfyUI keeps each kind of file

The manifests key each requirement by the ComfyUI node that loads it. That node
tells you which standard ComfyUI models folder the file belongs in:

| Loader node in the manifest | ComfyUI folder |
| --- | --- |
| `UNETLoader` | `models/diffusion_models` (a.k.a. `models/unet`) |
| `CLIPLoader` | `models/text_encoders` (a.k.a. `models/clip`) |
| `VAELoader` | `models/vae` |
| `LoraLoader` | `models/loras` |

ComfyUI's own documentation is the authority on installing models and on where
each folder lives: <https://docs.comfy.org/>. Once a file is in the right
folder, restart or refresh ComfyUI so it re-scans, then refresh Settings in
StoryBored — the pack flips to available.

## The default packs and what they load

The packs that ship in `workflows/` target two setups. Every filename below is
copied verbatim from that pack's `manifest.json` `required_models` — search for
these exact names on the model sources noted, and keep the filename unchanged so
the manifest matches.

### Krea 2 image packs (`krea2-basic`, `krea2-realism`)

Both packs build on a Krea 2 base plus a turbo-distill LoRA. `krea2-realism`
adds a community realism LoRA stack on top.

- **Base + encoders + VAE** (shared by both packs):
  - UNET: `krea2_raw_fp8_scaled.safetensors`
  - CLIP / text encoder: `qwen3vl_4b_fp8_scaled.safetensors`
  - VAE: `qwen_image_vae.safetensors`
- **Turbo distill LoRA** (both packs):
  - `(Krea 2) 8-Step Turbo Distill Rank 64 V2026.1.safetensors`
- **Realism LoRA stack** (`krea2-realism` only):
  - `krea2filterbypass3.safetensors`
  - `Krea2-realism-V2.safetensors`
  - `Detailer-KREA2.safetensors`
  - `bloomgirls-ultrarealism-krea2_4k.safetensors`
  - `lenovo_krea2.safetensors`
  - `RealisticSnapshotKrea2.safetensors`
  - `realism_engine_krea2_v3.1.safetensors`

The base model, text encoder, and VAE are packaged for ComfyUI by the
**Comfy-Org** organization on Hugging Face (<https://huggingface.co/Comfy-Org>)
— the same weights ComfyUI's own Krea/Qwen-image example pages point to. The
turbo-distill and realism LoRAs are **community files**: they come from model
hubs like Hugging Face and Civitai, published by the community, and you source
them yourself. If you'd rather not chase the whole realism stack, start with
`krea2-basic` — it needs only the base three files plus the one turbo-distill
LoRA — and add the realism LoRAs later.

### MiniMax H3 video pack (`minimax-h3-i2v`)

Animates an approved still into a short clip with generated audio.

- UNET: `minimax_h3_fl2va_pruned_nvfp4.safetensors`
- CLIP / text encoder: `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`
- VAE (two files — video and audio): `minimax_h3_video_vae_fp16.safetensors`,
  `minimax_h3_audio_vae_fp32.safetensors`

These MiniMax H3 weights are distributed for ComfyUI through the **Comfy-Org**
Hugging Face organization and the MiniMax H3 community repositories; follow
ComfyUI's MiniMax H3 example page for the current download links. Video needs
noticeably more VRAM than stills (see the hardware table in the
[README](../README.md#hardware-expectations)).

## Bring your own models

None of these files are special to StoryBored — they're ordinary ComfyUI model
files, and the default packs are just one possible setup. If you already have a
ComfyUI you generate with, the fastest path is to **wrap your own workflow as a
pack**: point its `required_models` at the models you already have, and skip the
downloads above entirely. [WORKFLOWS.md](WORKFLOWS.md) walks through authoring a
pack (including a plain SDXL example) step by step. StoryBored will run any pack
whose referenced files are present in your ComfyUI — nothing more.
