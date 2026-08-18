"""Scene endpoints: create under a project, update, delete, reorder."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from sqlmodel import Session, select

from storybored.api.projects import (
    get_project_or_404,
    take_file_paths,
    touch_project,
    unlink_data_files,
)
from storybored.db import get_session
from storybored.models import Scene, Shot, ShotCharacter, Take
from storybored.schemas import SceneCreate, SceneReorder, SceneUpdate

router = APIRouter(prefix="/api", tags=["scenes"])


def get_scene_or_404(session: Session, scene_id: int) -> Scene:
    scene = session.get(Scene, scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail="scene not found")
    return scene


@router.post("/projects/{project_id}/scenes", status_code=201)
def create_scene(
    project_id: int, body: SceneCreate, session: Session = Depends(get_session)
):
    get_project_or_404(session, project_id)
    existing = session.exec(select(Scene).where(Scene.project_id == project_id)).all()
    scene = Scene(
        project_id=project_id,
        idx=len(existing),
        title=body.title,
        slugline=body.slugline,
        description=body.description,
        look=body.look,
    )
    session.add(scene)
    touch_project(session, project_id)
    session.commit()
    session.refresh(scene)
    return jsonable_encoder(scene)


@router.post("/projects/{project_id}/scenes/reorder")
def reorder_scenes(
    project_id: int, body: SceneReorder, session: Session = Depends(get_session)
):
    get_project_or_404(session, project_id)
    scenes = {
        s.id: s
        for s in session.exec(select(Scene).where(Scene.project_id == project_id)).all()
    }
    unknown = [sid for sid in body.scene_ids if sid not in scenes]
    if unknown:
        raise HTTPException(status_code=400, detail=f"scene ids not in project: {unknown}")
    ordered = [scenes[sid] for sid in body.scene_ids]
    # any scenes not listed keep their relative order, after the listed ones
    ordered += [s for s in scenes.values() if s.id not in set(body.scene_ids)]
    for idx, scene in enumerate(ordered):
        scene.idx = idx
        session.add(scene)
    touch_project(session, project_id)
    session.commit()
    return {"scene_ids": [s.id for s in ordered]}


@router.patch("/scenes/{scene_id}")
def update_scene(scene_id: int, body: SceneUpdate, session: Session = Depends(get_session)):
    scene = get_scene_or_404(session, scene_id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("plate_take_id"):
        take = session.get(Take, changes["plate_take_id"])
        shot = session.get(Shot, take.shot_id) if take else None
        if (
            take is None
            or take.kind != "image"
            or take.status != "done"
            or shot is None
            or shot.scene_id != scene_id
        ):
            raise HTTPException(
                status_code=400,
                detail="scene plate must be a finished image take from this scene",
            )
    elif "plate_take_id" in changes:
        changes["plate_take_id"] = None  # 0/null both clear the plate
    for key, value in changes.items():
        setattr(scene, key, value)
    session.add(scene)
    touch_project(session, scene.project_id)
    session.commit()
    session.refresh(scene)
    return jsonable_encoder(scene)


@router.delete("/scenes/{scene_id}", status_code=204)
def delete_scene(scene_id: int, request: Request, session: Session = Depends(get_session)):
    scene = get_scene_or_404(session, scene_id)
    shot_ids = session.exec(select(Shot.id).where(Shot.scene_id == scene_id)).all()
    doomed_files: list[str | None] = []
    if shot_ids:
        takes = session.exec(select(Take).where(Take.shot_id.in_(shot_ids))).all()  # type: ignore[attr-defined]
        doomed_files = take_file_paths(takes)
        for take in takes:
            session.delete(take)
        for link in session.exec(
            select(ShotCharacter).where(ShotCharacter.shot_id.in_(shot_ids))  # type: ignore[attr-defined]
        ):
            session.delete(link)
        session.flush()
        for shot in session.exec(select(Shot).where(Shot.id.in_(shot_ids))):  # type: ignore[attr-defined]
            session.delete(shot)
        session.flush()
    project_id = scene.project_id
    session.delete(scene)
    # compact remaining scene indexes
    remaining = session.exec(
        select(Scene)
        .where(Scene.project_id == project_id, Scene.id != scene_id)
        .order_by(Scene.idx)  # type: ignore[arg-type]
    ).all()
    for idx, s in enumerate(remaining):
        s.idx = idx
        session.add(s)
    touch_project(session, project_id)
    session.commit()
    # rows are gone — reclaim the takes' files (guarded, best-effort)
    unlink_data_files(request.app.state.settings, doomed_files)
    return None
