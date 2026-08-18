"""GET /api/health — status of the engine, LLM, trainer and bundled ffmpeg.

Probes are strict: a component only reports "ok" when the response actually
looks like the service we expect (a random web server answering 200/404 on
the right port must not pass). Status vocabulary:

- ``ok``             — reachable and recognizably the right service
- ``unreachable``    — connection refused / timed out
- ``unrecognized``   — something answered, but it doesn't look like the
                       expected service (wrong port, wrong app)
- ``unauthorized``   — it answered 401/403: an API key is required (or the
                       saved one was rejected)
- ``error``          — the service answered with a 5xx
- ``not_configured`` — no URL/path set
- ``missing``        — (trainer/ffmpeg) configured path doesn't exist

The LLM probe authenticates: when an API key is configured it is sent as
``Authorization: Bearer …`` so hosted providers report ``ok`` instead of
``unrecognized``. Keys are NEVER accepted as query params — candidate-URL
probes use the saved key, and only when the candidate points at the same
host as the configured base URL (same spirit as the key-exfil guard in
llm/client.py).
"""

from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session

from storybored.api.settings_api import effective_setting
from storybored.config import Settings
from storybored.db import get_session
from storybored.engine import catalog
from storybored.engine.comfy_client import ComfyClient
from storybored.engine.registry import load_packs, pack_availability
from storybored.llm.client import LLMNotConfiguredError, get_llm_config

router = APIRouter(prefix="/api", tags=["health"])

PROBE_TIMEOUT_S = 3.0

#: Capability tiers derived from the engine's reported VRAM (GiB, rounded),
#: best tier first. "board" (below every floor) = no usable GPU: the board,
#: script breakdown and animatic assembly still work; rendering doesn't.
#: The floors track the verified engine recommendations in docs/MODELS.md:
#:   studio     ≥24 — every engine incl. Qwen-Image, 14B video and training
#:   stills-hd  ≥16 — the Krea 2 default fits comfortably; 5B video
#:   stills     ≥12 — Z-Image Turbo runs fully on-GPU; 5B video with offload
#:   stills-lite ≥6 — Z-Image Turbo with text-encoder offloading
TIER_FLOORS: list[tuple[str, int]] = [
    ("studio", 24),
    ("stills-hd", 16),
    ("stills", 12),
    ("stills-lite", 6),
]

#: Per-tier engine recommendations (the research-verified winners documented
#: in docs/MODELS.md). None = no pack of that kind is honestly recommendable
#: at the tier: video below 12 GB would over-promise (the 5B engine's floor
#: is 8 GB *with* offloading), and board can't render at all.
TIER_PACKS: dict[str, dict[str, str | None]] = {
    "board": {"image": None, "video": None},
    "stills-lite": {"image": "z-image-turbo", "video": None},
    "stills": {"image": "z-image-turbo", "video": "wan22-ti2v-5b"},
    "stills-hd": {"image": "krea2-basic", "video": "wan22-ti2v-5b"},
    "studio": {"image": "qwen-image-2512", "video": "wan22-i2v-14b"},
}

#: Ollama model tag suggested for the writing assistant, by best-GPU VRAM
#: (GiB, rounded), largest floor first. The LLM shares the GPU with the
#: render engine, so each tag leaves headroom: qwen3.5:4b is a 3.4 GB
#: download, qwen3.5:9b 6.6 GB, qwen3.5:35b-a3b 24 GB (MoE, 3B active).
#: No/unknown GPU falls through to the smallest (fine on CPU).
LLM_TAG_FLOORS: list[tuple[int, str]] = [
    (32, "qwen3.5:35b-a3b"),
    (12, "qwen3.5:9b"),
    (0, "qwen3.5:4b"),
]


def _get_json(url: str, headers: dict | None = None) -> tuple[str, object]:
    """GET url → (status, parsed body). Status per the module vocabulary."""
    try:
        resp = httpx.get(url, headers=headers or {}, timeout=PROBE_TIMEOUT_S)
    except httpx.HTTPError:
        return "unreachable", None
    if resp.status_code >= 500:
        return "error", None
    if resp.status_code in (401, 403):
        return "unauthorized", None
    if resp.status_code != 200:
        return "unrecognized", None
    try:
        return "ok", resp.json()
    except ValueError:
        return "unrecognized", None


def probe_comfy(url: str) -> tuple[str, dict]:
    """Probe a ComfyUI base URL. "ok" requires /system_stats to return 200
    with a JSON object that looks like ComfyUI's payload (a "system" key).
    Returns (status, system_stats dict — empty unless ok)."""
    status, data = _get_json(f"{url.rstrip('/')}/system_stats")
    if status == "ok":
        if not (isinstance(data, dict) and "system" in data):
            return "unrecognized", {}
        return "ok", data
    return status, {}


def llm_probe_key(session: Session, settings: Settings, target_url: str) -> str:
    """The saved LLM API key for probing ``target_url`` — or "".

    The key is only released when the probed URL points at the same host as
    the *configured* base URL, so a candidate address typed into the wizard
    can never siphon the stored secret (same spirit as the key-exfil guard
    in llm/client.py, which this also inherits: an env key is already
    stripped there when the base URL is a runtime override). Probe endpoints
    never accept a key as a query param.
    """
    try:
        config = get_llm_config(session, settings)
    except LLMNotConfiguredError:
        return ""
    if not config.api_key:
        return ""
    if urlsplit(target_url.strip()).netloc != urlsplit(config.base_url).netloc:
        return ""
    return config.api_key


def probe_llm(base_url: str, api_key: str = "") -> tuple[str, list[str]]:
    """Probe an OpenAI-compatible base URL. "ok" requires {base}/models to
    return 200 JSON. ``api_key`` (when given) is sent as a Bearer token so
    key-protected hosted providers can answer instead of 401ing into an
    "unauthorized"/"unrecognized" verdict.
    Returns (status, model ids when the shape is standard)."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    status, data = _get_json(f"{base_url.rstrip('/')}/models", headers)
    if status != "ok":
        return status, []
    models: list[str] = []
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        models = [
            str(m["id"]) for m in data["data"] if isinstance(m, dict) and m.get("id")
        ]
    return "ok", models


def probe_trainer(trainer_dir: str) -> str:
    if not trainer_dir:
        return "not_configured"
    return "ok" if Path(trainer_dir).expanduser().is_dir() else "missing"


def ffmpeg_status() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - health must never raise
        return "missing"


def parse_gpus(stats: dict) -> list[dict]:
    """GPU rows from a ComfyUI /system_stats payload: {name, vram_gb}.

    Only reports what the engine actually said — a device without a usable
    ``vram_total`` gets ``vram_gb: None``, never a guessed number."""
    gpus: list[dict] = []
    for device in stats.get("devices") or []:
        if not isinstance(device, dict) or device.get("type") == "cpu":
            continue
        name = str(device.get("name", "")).strip()
        # ComfyUI appends the allocator after " : " ("cuda:0 <card> : cudaMallocAsync")
        if " : " in name:
            name = name.split(" : ")[0].strip()
        vram = device.get("vram_total")
        vram_gb = (
            round(vram / 2**30, 1)
            if isinstance(vram, (int, float)) and not isinstance(vram, bool) and vram > 0
            else None
        )
        gpus.append({"name": name, "vram_gb": vram_gb})
    return gpus


def best_vram_gb(gpus: list[dict]) -> float | None:
    """The largest reported VRAM among the engine's GPUs, or None."""
    return max((g["vram_gb"] for g in gpus if g["vram_gb"] is not None), default=None)


def derive_tier(gpus: list[dict]) -> str:
    """Capability tier from the best GPU with known VRAM (see TIER_FLOORS).

    Rounded to whole GiB before comparing so a "16 GB" card that reports
    15.99 GiB still clears the stills-hd bar."""
    best = best_vram_gb(gpus)
    if best is None:
        return "board"
    for name, floor in TIER_FLOORS:
        if round(best) >= floor:
            return name
    return "board"


def recommended_llm_tag(vram_gb: float | None) -> str:
    """Ollama tag for the writing assistant, by VRAM (see LLM_TAG_FLOORS)."""
    for floor, tag in LLM_TAG_FLOORS:
        if vram_gb is not None and round(vram_gb) >= floor:
            return tag
    return LLM_TAG_FLOORS[-1][1]


def _recommended_pack(
    pack_id: str | None, workflows: list[dict], file_catalog: dict[str, dict]
) -> dict | None:
    """The `recommended.image` / `.video` row for the setup probe.

    Enriches the tier's pack pick with what the wizard needs to offer a
    one-click install: availability, the missing files, their total download
    size from the catalog, and whether every missing file has a verified
    source (i.e. "Download missing" can fetch the lot)."""
    if pack_id is None:
        return None
    row = next((w for w in workflows if w["id"] == pack_id), None)
    if row is None:  # pack folder removed — never recommend what isn't there
        return None
    missing = row["missing_models"]
    download_bytes = 0
    downloadable = True
    for filename in missing:
        entry = file_catalog.get(filename) or {}
        if isinstance(entry.get("size_bytes"), int):
            download_bytes += entry["size_bytes"]
        if not str(entry.get("source", "")).startswith(("http://", "https://")):
            downloadable = False
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "available": row["available"],
        "missing_models": missing,
        "download_bytes": download_bytes,
        "downloadable": downloadable,
        "license_note": row["license_note"],
    }


@router.get("/setup/probe")
async def setup_probe(
    request: Request,
    session: Session = Depends(get_session),
    comfy_url: str | None = None,
    llm_url: str | None = None,
    trainer_dir: str | None = None,
):
    """One-shot probe for the setup wizard: everything /api/health knows plus
    GPU/VRAM/tier, the LLM's model list, and per-pack availability.

    Query params probe CANDIDATE values without persisting anything — omit
    them to probe the effective settings. Numbers come straight from the
    engine's /system_stats; nothing is invented when it doesn't answer."""
    settings: Settings = request.app.state.settings
    if comfy_url is None:
        comfy_url = effective_setting(session, settings, "comfyui_url")
    if llm_url is None:
        llm_url = effective_setting(session, settings, "llm_base_url")
    if trainer_dir is None:
        trainer_dir = effective_setting(session, settings, "lora_factory_dir")

    if comfy_url:
        comfy_status, stats = await run_in_threadpool(probe_comfy, comfy_url)
    else:
        comfy_status, stats = "not_configured", {}
    gpus = parse_gpus(stats)
    tier = derive_tier(gpus) if comfy_status == "ok" else "board"

    workflows: list[dict] = []
    if comfy_status == "ok":
        client = ComfyClient(comfy_url)
        packs = load_packs(settings)
        for pack_id in sorted(packs):
            pack = packs[pack_id]
            availability = await pack_availability(pack, client)
            workflows.append(
                {
                    "id": pack.id,
                    "name": pack.manifest.get("name", pack.id),
                    "kind": pack.manifest.get("kind", "image"),
                    "available": availability["available"],
                    "missing_models": availability["missing_models"],
                    "license_note": str(pack.manifest.get("license_note") or ""),
                }
            )

    recommended = None
    if comfy_status == "ok":
        file_catalog = catalog.load_catalog(settings)
        tier_packs = TIER_PACKS.get(tier) or {"image": None, "video": None}
        recommended = {
            "tier": tier,
            "image": _recommended_pack(tier_packs["image"], workflows, file_catalog),
            "video": _recommended_pack(tier_packs["video"], workflows, file_catalog),
            "llm": recommended_llm_tag(best_vram_gb(gpus)),
        }

    if llm_url:
        llm_key = llm_probe_key(session, settings, llm_url)
        llm_status, models = await run_in_threadpool(probe_llm, llm_url, llm_key)
    else:
        llm_status, models = "not_configured", []

    return {
        "comfy": {"status": comfy_status, "url": comfy_url, "gpus": gpus, "tier": tier},
        "llm": {"status": llm_status, "url": llm_url, "models": models},
        "trainer": {"status": probe_trainer(trainer_dir), "dir": trainer_dir},
        "ffmpeg": ffmpeg_status(),
        "workflows": workflows,
        "recommended": recommended,
        "tiers": {name: floor for name, floor in TIER_FLOORS},
    }


@router.get("/health")
def health(request: Request, session: Session = Depends(get_session)):
    settings: Settings = request.app.state.settings

    comfy_url = effective_setting(session, settings, "comfyui_url")
    comfy = probe_comfy(comfy_url)[0] if comfy_url else "not_configured"

    llm_base = effective_setting(session, settings, "llm_base_url")
    llm = (
        probe_llm(llm_base, llm_probe_key(session, settings, llm_base))[0]
        if llm_base
        else "not_configured"
    )

    trainer = probe_trainer(effective_setting(session, settings, "lora_factory_dir"))

    return {"comfy": comfy, "llm": llm, "trainer": trainer, "ffmpeg": ffmpeg_status()}
