# OWNED-BY: engine-agent
"""Hardware fit for engine packs: will this pack run *smoothly* on this GPU?

"Available" only means the files exist — a pack can be available and still
page itself into a crawl when its resident model set doesn't fit VRAM (found
the hard way: qwen-image-2512 "fully loads" on a desktop-sharing 32 GB card,
then renders at ~45 min/frame). This module models peak VRAM residency and
compares it against a measured budget from ComfyUI's /system_stats.

Precedence rule (documented for the UI): **measured beats modeled**. When an
engine has real completed-render timings on this machine, those are the truth
and the fit verdict is background information; the modeled verdict is the
headline only for engines that have never rendered here.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path

from storybored.engine.catalog import FOLDER_ALIASES, LOADER_FOLDERS

#: co-residency model: sequential-loading groups contribute their largest
#: file (Wan 2.2's two 14B experts swap, they never sit together), everything
#: else is summed — the pessimistic-but-observed case where ComfyUI keeps the
#: text encoder resident beside the UNET.
_SEQUENTIAL_FOLDERS = {"diffusion_models", "checkpoints"}

#: verdicts, in order of decreasing comfort
FIT_OK = "ok"
FIT_TIGHT = "tight"
FIT_EXCEEDS = "exceeds"
FIT_UNKNOWN = "unknown"

#: desktop/driver overhead assumed when /system_stats can't tell us (bytes)
_DEFAULT_OVERHEAD = 2 * 2**30
#: never attribute more than this to the desktop — beyond it we're probably
#: watching a render in flight, not the idle reserve
_MAX_OVERHEAD = 8 * 2**30

#: smallest overhead seen per engine URL (closest to the true idle reserve);
#: refreshed opportunistically, remembered for a day
_overhead_seen: dict[str, tuple[float, int]] = {}
_OVERHEAD_TTL_S = 24 * 3600


def file_size_bytes(
    filename: str,
    class_type: str,
    catalog: Mapping[str, Mapping],
    comfy_models_dir: str = "",
) -> int | None:
    """Size of one model file: the real on-disk size when the models dir is
    configured and the file is there, else the catalog's verified size, else
    None (unknown — the caller must degrade honestly, never guess)."""
    entry = catalog.get(filename) or {}
    folder = str(entry.get("folder") or LOADER_FOLDERS.get(class_type, ""))
    if comfy_models_dir and folder:
        for alias in FOLDER_ALIASES.get(folder, (folder,)):
            path = Path(comfy_models_dir) / alias / filename
            try:
                if path.is_file():
                    return path.stat().st_size
            except OSError:
                pass
    size = entry.get("size_bytes")
    return size if isinstance(size, int) and size > 0 else None


def pack_peak_bytes(
    required_models: Mapping[str, list[str]],
    catalog: Mapping[str, Mapping],
    comfy_models_dir: str = "",
) -> int | None:
    """Modeled peak VRAM residency of a pack's effective model set.

    None when any diffusion/text-encoder size is unknown — those dominate the
    total, so a guess would be worse than admitting ignorance. Unknown VAE or
    LoRA sizes degrade to 0 (they're small; an honest slight undercount).
    """
    sequential_max = 0
    resident_sum = 0
    for spec, files in required_models.items():
        class_type = str(spec).partition(".")[0]
        folder = LOADER_FOLDERS.get(class_type, "")
        for filename in files or []:
            size = file_size_bytes(filename, class_type, catalog, comfy_models_dir)
            if folder in _SEQUENTIAL_FOLDERS:
                if size is None:
                    return None
                sequential_max = max(sequential_max, size)
            elif folder == "text_encoders":
                if size is None:
                    return None
                resident_sum += size
            else:
                resident_sum += size or 0
    if sequential_max == 0 and resident_sum == 0:
        return None
    return sequential_max + resident_sum


def vram_budget(system_stats: Mapping | None, base_url: str = "") -> tuple[int, int] | None:
    """(vram_total, usable_budget) in bytes from a ComfyUI /system_stats
    payload — None when the engine didn't report usable numbers.

    The budget subtracts the *smallest* overhead ever observed on this engine
    (total - free at the most idle moment we've seen), clamped to sane bounds
    so a mid-render snapshot never poisons the estimate.
    """
    devices = (system_stats or {}).get("devices") or []
    best = None
    for dev in devices:
        total = dev.get("vram_total")
        if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
            if best is None or total > best[0]:
                free = dev.get("vram_free")
                free = (
                    int(free)
                    if isinstance(free, (int, float)) and not isinstance(free, bool)
                    else None
                )
                best = (int(total), free)
    if best is None:
        return None
    total, free = best
    observed = total - free if free is not None else None
    now = time.monotonic()
    cached = _overhead_seen.get(base_url)
    candidates = []
    if cached and now - cached[0] < _OVERHEAD_TTL_S:
        candidates.append(cached[1])
    if observed is not None and 0 <= observed <= _MAX_OVERHEAD:
        candidates.append(observed)
    overhead = min(candidates) if candidates else _DEFAULT_OVERHEAD
    _overhead_seen[base_url] = (now, overhead)
    return total, max(0, total - overhead)


def fit_verdict(peak: int | None, budget: tuple[int, int] | None) -> tuple[str, str]:
    """(fit, fit_detail): a verdict plus one honest sentence for the UI."""
    if peak is None or budget is None:
        return FIT_UNKNOWN, ""
    total, usable = budget
    peak_gb = peak / 2**30
    usable_gb = usable / 2**30
    if peak <= usable * 0.90:
        return FIT_OK, ""
    if peak <= total:
        return FIT_TIGHT, (
            f"~{peak_gb:.0f} GB of models vs ~{usable_gb:.0f} GB usable VRAM — "
            "it runs, but expect paging and slow renders on this card"
        )
    return FIT_EXCEEDS, (
        f"~{peak_gb:.0f} GB of models exceed this card's {total / 2**30:.0f} GB VRAM — "
        "renders will crawl or fail; a smaller engine is a better fit"
    )
