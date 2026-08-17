# OWNED-BY: export-agent
"""Video render + animatic export endpoints.

- POST /api/shots/{id}/render-video      {workflow_id?, motion_prompt?,
                                          frame_position?} → {job_id}
- POST /api/projects/{id}/render-videos  queue video_gen for every approved shot
                                         lacking a video take → {job_ids}
- POST /api/projects/{id}/animatic       {scene_id?, title_cards?} → {job_id}
- GET  /api/projects/{id}/exports        → finished export files (newest first)
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlmodel import Session, select

from storybored.api.preflight import require_pack_available, resolve_pack
from storybored.api.projects import get_project_or_404
from storybored.api.shots import get_shot_or_404
from storybored.db import get_session
from storybored.engine import registry
from storybored.models import Scene, Shot, Take

router = APIRouter(prefix="/api", tags=["export"])


class RenderVideoBody(BaseModel):
    workflow_id: str | None = None
    motion_prompt: str | None = None
    frame_position: Literal["first", "last"] | None = None


def _require_renderable(session: Session, shot: Shot) -> None:
    """A shot renders to video only when approved with a finished picked still."""
    if shot.status != "approved":
        raise HTTPException(
            status_code=409, detail="shot must be approved before rendering video"
        )
    if shot.picked_take_id is None:
        raise HTTPException(status_code=409, detail="shot has no picked still to animate")
    picked = session.get(Take, shot.picked_take_id)
    if picked is None or picked.kind != "image" or picked.status != "done":
        raise HTTPException(status_code=409, detail="picked take is not a finished still")


def _enqueue_video(
    request: Request,
    shot: Shot,
    workflow_id: str | None = None,
    project_id: int | None = None,
):
    payload: dict = {"shot_id": shot.id}
    if workflow_id:
        payload["workflow_id"] = workflow_id
    return request.app.state.runner.enqueue("video_gen", payload, project_id=project_id)


async def _preflight_video(
    request: Request, session: Session, workflow_id: str | None
) -> None:
    """Same gate the image path has: resolve the video pack (explicit id or the
    default) and 409/503 before enqueueing a job that could never render."""
    settings = request.app.state.settings
    packs = registry.load_packs(settings)
    pack = resolve_pack(session, settings, packs, workflow_id, kind="video")
    await require_pack_available(
        session, settings, pack, f"cannot validate workflow '{pack.id}'"
    )


@router.post("/shots/{shot_id}/render-video")
async def render_video(
    shot_id: int,
    request: Request,
    body: RenderVideoBody | None = None,
    session: Session = Depends(get_session),
):
    shot = get_shot_or_404(session, shot_id)
    _require_renderable(session, shot)
    body = body or RenderVideoBody()
    await _preflight_video(request, session, body.workflow_id)
    if body.motion_prompt is not None or body.frame_position is not None:
        if body.motion_prompt is not None:
            shot.motion_prompt = body.motion_prompt
        if body.frame_position is not None:
            shot.frame_position = body.frame_position
        session.add(shot)
        session.commit()
        session.refresh(shot)
        request.app.state.bus.publish("shot", jsonable_encoder(shot))
    scene = session.get(Scene, shot.scene_id)
    job = _enqueue_video(
        request,
        shot,
        body.workflow_id,
        project_id=scene.project_id if scene is not None else None,
    )
    return {"job_id": job.id}


@router.post("/projects/{project_id}/render-videos")
async def render_videos(
    project_id: int, request: Request, session: Session = Depends(get_session)
):
    """Queue one video_gen per approved shot that has no video take yet."""
    get_project_or_404(session, project_id)
    # every queued job uses the default video pack — one gate covers the batch
    await _preflight_video(request, session, None)
    shots = session.exec(
        select(Shot)
        .join(Scene, Scene.id == Shot.scene_id)  # type: ignore[arg-type]
        .where(Scene.project_id == project_id)
        .where(Shot.status == "approved")
        .where(Shot.video_take_id == None)  # noqa: E711 - SQL NULL check
        .order_by(Scene.idx, Shot.idx)  # type: ignore[arg-type]
    ).all()
    job_ids = [
        _enqueue_video(request, shot, project_id=project_id).id for shot in shots
    ]
    return {"job_ids": job_ids, "queued": len(job_ids)}


class AnimaticBody(BaseModel):
    scene_id: int | None = None
    title_cards: bool = False


@router.post("/projects/{project_id}/animatic")
def export_animatic(
    project_id: int,
    request: Request,
    body: AnimaticBody | None = None,
    session: Session = Depends(get_session),
):
    get_project_or_404(session, project_id)
    body = body or AnimaticBody()
    if body.scene_id is not None:
        scene = session.get(Scene, body.scene_id)
        if scene is None or scene.project_id != project_id:
            raise HTTPException(status_code=404, detail="scene not found in this project")
    payload = {"project_id": project_id}
    if body.scene_id is not None:
        payload["scene_id"] = body.scene_id
    if body.title_cards:
        payload["title_cards"] = True
    job = request.app.state.runner.enqueue("animatic", payload, project_id=project_id)
    return {"job_id": job.id}


@router.get("/projects/{project_id}/exports")
def list_exports(
    project_id: int, request: Request, session: Session = Depends(get_session)
):
    get_project_or_404(session, project_id)
    settings = request.app.state.settings
    export_dir = settings.exports_path / str(project_id)
    if not export_dir.is_dir():
        return []
    files = sorted(
        (p for p in export_dir.iterdir() if p.is_file() and p.suffix == ".mp4"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [
        {
            "file_name": p.name,
            "file_path": str(p.relative_to(settings.data_path)),
            "size_bytes": p.stat().st_size,
            "created_at": datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat(),
        }
        for p in files
    ]
