"""GET /api/health — status of the engine, LLM, trainer and bundled ffmpeg."""

from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Request
from sqlmodel import Session

from storybored.api.settings_api import effective_setting
from storybored.config import Settings
from storybored.db import get_session

router = APIRouter(prefix="/api", tags=["health"])


def _probe(url: str) -> str:
    try:
        resp = httpx.get(url, timeout=2.0)
        return "ok" if resp.status_code < 500 else "error"
    except httpx.HTTPError:
        return "unreachable"


@router.get("/health")
def health(request: Request, session: Session = Depends(get_session)):
    settings: Settings = request.app.state.settings

    comfy_url = effective_setting(session, settings, "comfyui_url")
    comfy = _probe(f"{comfy_url.rstrip('/')}/system_stats") if comfy_url else "not_configured"

    llm_base = effective_setting(session, settings, "llm_base_url")
    if not llm_base:
        llm = "not_configured"
    else:
        llm = _probe(f"{llm_base.rstrip('/')}/models")

    trainer_dir = effective_setting(session, settings, "lora_factory_dir")
    if not trainer_dir:
        trainer = "not_configured"
    else:
        trainer = "ok" if Path(trainer_dir).is_dir() else "missing"

    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - health must never raise
        ffmpeg = "missing"

    return {"comfy": comfy, "llm": llm, "trainer": trainer, "ffmpeg": ffmpeg}
