# OWNED-BY: engine-agent
"""Model catalog: filename → where to get it and where it goes.

The data lives in ``workflows/catalog.json`` (shipped) merged with
``DATA_DIR/workflows/catalog.json`` (user additions win on the same filename).
An entry with an http(s) ``source`` URL was verified against the hosting
site — those are the ones the in-app downloader will fetch. ``source:
"community"`` means the file is community-published with no canonical home we
link to; the entry's ``notes`` say where to look, and placement is manual.

Also home to the big-model guardrail: when ``COMFY_MODELS_DIR`` is set we can
stat the files behind model-slot dropdowns and warn on very large ones.
"""

import json
import logging
from pathlib import Path

from storybored.config import Settings

log = logging.getLogger("storybored.engine")

#: repo-level catalog (…/storybored/workflows/catalog.json)
REPO_CATALOG = Path(__file__).resolve().parents[3] / "workflows" / "catalog.json"

#: loader class → the ComfyUI models/ subfolder it scans (docs/MODELS.md table)
LOADER_FOLDERS: dict[str, str] = {
    "UNETLoader": "diffusion_models",
    "CLIPLoader": "text_encoders",
    "DualCLIPLoader": "text_encoders",
    "VAELoader": "vae",
    "LoraLoader": "loras",
    "LoraLoaderModelOnly": "loras",
    "CheckpointLoaderSimple": "checkpoints",
}

#: folders with a legacy alias ComfyUI also scans
FOLDER_ALIASES: dict[str, tuple[str, ...]] = {
    "diffusion_models": ("diffusion_models", "unet"),
    "text_encoders": ("text_encoders", "clip"),
}

#: Files above this size get a warning in the model-slot dropdown. Grounded in
#: a real incident: an unquantized bf16 video UNET around 32 GB forced massive
#: VRAM offload on a 24 GB-class card (the ~20 GB quantized build of the same
#: model ran fully on-GPU). Not a hard limit — a warning.
LARGE_FILE_BYTES = 24 * 2**30


def load_catalog(settings: Settings) -> dict[str, dict]:
    """Merged catalog: shipped file + DATA_DIR/workflows/catalog.json on top."""
    merged: dict[str, dict] = {}
    for path in (REPO_CATALOG, settings.data_path / "workflows" / "catalog.json"):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("skipping model catalog %s: %s", path, exc)
            continue
        if not isinstance(data, dict):
            continue
        for filename, entry in data.items():
            if isinstance(entry, dict) and not filename.startswith("_"):
                merged[str(filename)] = entry
    return merged


def model_file_info(filename: str, class_type: str, catalog: dict[str, dict]) -> dict:
    """The API/UI row for one required file: destination + source when known.

    ``downloadable`` is true only for entries with a verified http(s) source
    URL and a resolvable destination folder.
    """
    entry = catalog.get(filename) or {}
    folder = str(entry.get("folder") or LOADER_FOLDERS.get(class_type, ""))
    source = str(entry.get("source") or "")
    info: dict = {
        "filename": filename,
        "folder": folder,
        "downloadable": source.startswith(("http://", "https://")) and bool(folder),
    }
    if source and source != "community":
        info["source"] = source
    if entry.get("page"):
        info["page"] = str(entry["page"])
    if isinstance(entry.get("size_bytes"), int):
        info["size_bytes"] = entry["size_bytes"]
    if entry.get("license"):
        info["license"] = str(entry["license"])
    if entry.get("notes"):
        info["notes"] = str(entry["notes"])
    return info


def local_model_size(models_dir: str, class_type: str, filename: str) -> int | None:
    """Size of a model file under the shared ComfyUI models dir, or None.

    Any stat problem (folder elsewhere, symlink loop, permissions, the dropdown
    name containing a subdir) is a silent None — this only feeds a warning.
    """
    folder = LOADER_FOLDERS.get(class_type)
    if not models_dir or not folder:
        return None
    base = Path(models_dir).expanduser()
    for candidate in FOLDER_ALIASES.get(folder, (folder,)):
        try:
            path = base / candidate / filename
            if path.is_file():
                return path.stat().st_size
        except OSError:
            continue
    return None
