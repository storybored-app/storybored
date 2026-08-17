# OWNED-BY: engine-agent
"""GET /api/workflows — workflow pack registry with model availability."""

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session

from storybored.api.settings_api import effective_setting
from storybored.db import get_session
from storybored.engine import registry
from storybored.engine.comfy_client import clear_object_info_cache
from storybored.engine.graph import parse_engine_loras, parse_engine_models

router = APIRouter(prefix="/api", tags=["workflows"])


@router.get("/workflows")
async def list_workflows(
    request: Request, refresh: bool = False, session: Session = Depends(get_session)
):
    """The registry payload. ``?refresh=true`` drops the 60s /object_info cache
    first, so availability reflects models/nodes installed seconds ago (the
    Settings "Refresh" button)."""
    settings = request.app.state.settings
    if refresh:
        clear_object_info_cache()
    comfy_url = effective_setting(session, settings, "comfyui_url")
    default_ids = {
        "image": effective_setting(session, settings, "default_image_workflow"),
        "video": effective_setting(session, settings, "default_video_workflow"),
    }
    engine_loras = parse_engine_loras(effective_setting(session, settings, "engine_loras"))
    engine_models = parse_engine_models(effective_setting(session, settings, "engine_models"))
    return await registry.list_workflows(
        settings, comfy_url, default_ids, engine_loras, engine_models
    )
