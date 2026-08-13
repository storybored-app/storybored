# OWNED-BY: engine-agent
"""GET /api/workflows — workflow pack registry with model availability."""

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session

from storybored.api.settings_api import effective_setting
from storybored.db import get_session
from storybored.engine import registry

router = APIRouter(prefix="/api", tags=["workflows"])


@router.get("/workflows")
async def list_workflows(request: Request, session: Session = Depends(get_session)):
    settings = request.app.state.settings
    comfy_url = effective_setting(session, settings, "comfyui_url")
    return await registry.list_workflows(settings, comfy_url)
