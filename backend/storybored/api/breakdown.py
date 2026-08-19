# OWNED-BY: llm-agent
"""Script breakdown endpoints.

POST /api/breakdown — synchronous LLM call, returns a DRAFT (nothing persisted).
POST /api/projects/{id}/apply-breakdown — appends draft scenes/shots to the board,
linking characters by handle when they exist.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from storybored.api.projects import get_project_or_404, touch_project
from storybored.casting import MENTION_RE, refresh_shot_characters
from storybored.config import Settings
from storybored.db import get_session
from storybored.llm.breakdown import BreakdownDraft, breakdown_script
from storybored.llm.client import LLMError, LLMNotConfiguredError, get_llm_config
from storybored.llm.guides import resolve_prompt_guide
from storybored.models import Character, Scene, Shot

router = APIRouter(prefix="/api", tags=["breakdown"])


class BreakdownRequest(BaseModel):
    project_id: int
    script_text: str
    mode: str = "script"  # "script" (1st-AD breakdown) | "vibes" (story → coverage)


class ApplyBreakdownRequest(BaseModel):
    draft: BreakdownDraft


@router.post("/breakdown")
def breakdown(
    body: BreakdownRequest, request: Request, session: Session = Depends(get_session)
):
    settings: Settings = request.app.state.settings
    get_project_or_404(session, body.project_id)
    if not body.script_text.strip():
        raise HTTPException(status_code=400, detail="script_text is empty")
    if body.mode not in ("script", "vibes"):
        raise HTTPException(status_code=400, detail="mode must be 'script' or 'vibes'")

    try:
        config = get_llm_config(session, settings)
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    known_handles = [c.handle for c in session.exec(select(Character)).all()]
    # Shot descriptions become image prompts — steer them toward the engine
    # that will render them (the configured default image workflow's guide).
    guide = resolve_prompt_guide(session, settings, "image")
    try:
        draft = breakdown_script(
            config, body.script_text, known_handles, mode=body.mode, guide=guide
        )
    except LLMNotConfiguredError as exc:  # pragma: no cover - config resolved above
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return draft.model_dump()


@router.post("/projects/{project_id}/apply-breakdown")
def apply_breakdown(
    project_id: int,
    body: ApplyBreakdownRequest,
    session: Session = Depends(get_session),
):
    get_project_or_404(session, project_id)
    if not body.draft.scenes:
        raise HTTPException(status_code=400, detail="draft has no scenes")

    by_handle = {c.handle.lower(): c for c in session.exec(select(Character)).all()}
    existing = session.exec(select(Scene).where(Scene.project_id == project_id)).all()
    next_idx = max((s.idx for s in existing), default=-1) + 1

    scene_ids: list[int] = []
    shots_created = 0
    characters_linked = 0

    for offset, draft_scene in enumerate(body.draft.scenes):
        scene = Scene(
            project_id=project_id,
            idx=next_idx + offset,
            title=draft_scene.title,
            slugline=draft_scene.slugline,
            look=draft_scene.look,
        )
        session.add(scene)
        session.flush()
        assert scene.id is not None
        scene_ids.append(scene.id)

        for shot_idx, draft_shot in enumerate(draft_scene.shots):
            # Resolve tagged handles against known characters (unknown ones are
            # dropped). Inject the matched @handle into the description text so it
            # is the single source of truth: visible/editable in the UI and
            # honored at generation time (which re-derives characters from the
            # description). Otherwise the tags would be cosmetic and get wiped.
            description = draft_shot.description or ""
            existing = {m.lower() for m in MENTION_RE.findall(description)}
            matched: list[str] = []
            for raw_handle in draft_shot.characters:
                character = by_handle.get(raw_handle.lstrip("@").strip().lower())
                if character is None or character.handle in matched:
                    continue  # unknown handles are simply skipped
                matched.append(character.handle)
            additions = [f"@{h}" for h in matched if h.lower() not in existing]
            if additions:
                description = (description.rstrip() + " " + " ".join(additions)).strip()

            shot = Shot(
                scene_id=scene.id,
                idx=shot_idx,
                description=description,
                shot_type=draft_shot.shot_type,
                camera=draft_shot.camera,
                dialogue=draft_shot.dialogue,
                duration_s=max(0.5, float(draft_shot.duration_s or 4.0)),
            )
            session.add(shot)
            session.flush()
            shots_created += 1
            refresh_shot_characters(session, shot)
            characters_linked += len(matched)

    touch_project(session, project_id)
    session.commit()
    return {
        "scenes_created": len(scene_ids),
        "shots_created": shots_created,
        "characters_linked": characters_linked,
        "scene_ids": scene_ids,
    }
