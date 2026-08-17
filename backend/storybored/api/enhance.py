# OWNED-BY: llm-agent
"""PromptSmith endpoints — LLM passes whose results land in visible editor
fields; nothing is persisted here and prompts are never rewritten silently at
render time (product rule).

- POST /api/shots/{id}/enhance          rough notes → polished image prompt
- POST /api/shots/{id}/generate-motion  shot details → MiniMax motion prompt
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session

from storybored.api.shots import get_shot_or_404
from storybored.config import Settings
from storybored.db import get_session
from storybored.llm.client import LLMError, LLMNotConfiguredError, get_llm_config
from storybored.llm.enhance import build_notes, enhance_description
from storybored.llm.guides import resolve_prompt_guide
from storybored.llm.motion import build_motion_notes, generate_motion_prompt
from storybored.models import Scene

router = APIRouter(prefix="/api", tags=["enhance"])


class EnhanceRequest(BaseModel):
    """Unsaved editor state wins over stored fields when provided."""

    description: str | None = None
    shot_type: str | None = None
    camera: str | None = None
    #: the image engine selected in the editor — its prompt_guide steers the
    #: enhancement; omitted → the configured default image workflow's guide
    workflow_id: str | None = None


@router.post("/shots/{shot_id}/enhance")
def enhance_shot(
    shot_id: int,
    request: Request,
    body: EnhanceRequest | None = None,
    session: Session = Depends(get_session),
):
    settings: Settings = request.app.state.settings
    shot = get_shot_or_404(session, shot_id)
    body = body or EnhanceRequest()

    description = (body.description if body.description is not None else shot.description) or ""
    if not description.strip():
        raise HTTPException(status_code=400, detail="shot has no description to enhance")

    try:
        config = get_llm_config(session, settings)
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    scene = session.get(Scene, shot.scene_id)
    notes = build_notes(
        description,
        shot_type=body.shot_type if body.shot_type is not None else shot.shot_type,
        camera=body.camera if body.camera is not None else shot.camera,
        scene_slugline=scene.slugline if scene else "",
        scene_description=scene.description if scene else "",
    )
    guide = resolve_prompt_guide(session, settings, "image", body.workflow_id)
    try:
        enhanced = enhance_description(config, notes, description, guide)
    except LLMNotConfiguredError as exc:  # pragma: no cover - config resolved above
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"description": enhanced}


class MotionRequest(BaseModel):
    """Unsaved editor state wins over stored fields when provided."""

    description: str | None = None
    shot_type: str | None = None
    camera: str | None = None
    dialogue: str | None = None
    motion_prompt: str | None = None
    frame_position: Literal["first", "last"] | None = None
    #: the video engine selected in the editor — its prompt_guide steers the
    #: motion draft; omitted → the configured default video workflow's guide
    workflow_id: str | None = None


@router.post("/shots/{shot_id}/generate-motion")
def generate_motion(
    shot_id: int,
    request: Request,
    body: MotionRequest | None = None,
    session: Session = Depends(get_session),
):
    settings: Settings = request.app.state.settings
    shot = get_shot_or_404(session, shot_id)
    body = body or MotionRequest()

    description = (body.description if body.description is not None else shot.description) or ""
    if not description.strip():
        raise HTTPException(
            status_code=400, detail="shot needs a description before generating motion"
        )

    try:
        config = get_llm_config(session, settings)
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    rough_motion = (
        body.motion_prompt if body.motion_prompt is not None else shot.motion_prompt
    ) or ""
    scene = session.get(Scene, shot.scene_id)
    notes = build_motion_notes(
        description,
        shot_type=body.shot_type if body.shot_type is not None else shot.shot_type,
        camera=body.camera if body.camera is not None else shot.camera,
        motion_prompt=rough_motion,
        dialogue=body.dialogue if body.dialogue is not None else shot.dialogue,
        duration_s=shot.duration_s,
        scene_slugline=scene.slugline if scene else "",
        scene_description=scene.description if scene else "",
        frame_position=body.frame_position or shot.frame_position or "first",
    )
    guide = resolve_prompt_guide(session, settings, "video", body.workflow_id)
    try:
        motion = generate_motion_prompt(config, notes, rough_motion, guide)
    except LLMNotConfiguredError as exc:  # pragma: no cover - config resolved above
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"motion_prompt": motion}
