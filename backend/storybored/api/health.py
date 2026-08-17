"""GET /api/health — status of the engine, LLM, trainer and bundled ffmpeg.

Probes are strict: a component only reports "ok" when the response actually
looks like the service we expect (a random web server answering 200/404 on
the right port must not pass). Status vocabulary:

- ``ok``             — reachable and recognizably the right service
- ``unreachable``    — connection refused / timed out
- ``unrecognized``   — something answered, but it doesn't look like the
                       expected service (wrong port, wrong app)
- ``error``          — the service answered with a 5xx
- ``not_configured`` — no URL/path set
- ``missing``        — (trainer/ffmpeg) configured path doesn't exist
"""

from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Request
from sqlmodel import Session

from storybored.api.settings_api import effective_setting
from storybored.config import Settings
from storybored.db import get_session

router = APIRouter(prefix="/api", tags=["health"])

PROBE_TIMEOUT_S = 3.0


def _get_json(url: str) -> tuple[str, object]:
    """GET url → (status, parsed body). Status per the module vocabulary."""
    try:
        resp = httpx.get(url, timeout=PROBE_TIMEOUT_S)
    except httpx.HTTPError:
        return "unreachable", None
    if resp.status_code >= 500:
        return "error", None
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


def probe_llm(base_url: str) -> tuple[str, list[str]]:
    """Probe an OpenAI-compatible base URL. "ok" requires {base}/models to
    return 200 JSON. Returns (status, model ids when the shape is standard)."""
    status, data = _get_json(f"{base_url.rstrip('/')}/models")
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


@router.get("/health")
def health(request: Request, session: Session = Depends(get_session)):
    settings: Settings = request.app.state.settings

    comfy_url = effective_setting(session, settings, "comfyui_url")
    comfy = probe_comfy(comfy_url)[0] if comfy_url else "not_configured"

    llm_base = effective_setting(session, settings, "llm_base_url")
    llm = probe_llm(llm_base)[0] if llm_base else "not_configured"

    trainer = probe_trainer(effective_setting(session, settings, "lora_factory_dir"))

    return {"comfy": comfy, "llm": llm, "trainer": trainer, "ffmpeg": ffmpeg_status()}
