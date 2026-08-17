"""Shot endpoints: CRUD, reorder (incl. cross-scene moves), takes listing,
pick, approve/unapprove. @mentions in the description refresh shotcharacter."""

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from sqlmodel import Session, select

from storybored.api.projects import take_file_paths, touch_project, unlink_data_files
from storybored.api.scenes import get_scene_or_404
from storybored.db import get_session
from storybored.models import Character, Shot, ShotCharacter, Take
from storybored.schemas import ShotCreate, ShotReorder, ShotUpdate

router = APIRouter(prefix="/api", tags=["shots"])

MENTION_RE = re.compile(r"@([A-Za-z0-9_\-]+)")


def get_shot_or_404(session: Session, shot_id: int) -> Shot:
    shot = session.get(Shot, shot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="shot not found")
    return shot


def _publish_shot(request: Request, shot: Shot) -> None:
    request.app.state.bus.publish("shot", jsonable_encoder(shot))


def refresh_shot_characters(session: Session, shot: Shot) -> None:
    """Sync the shotcharacter link table from @handle mentions in the description.

    Mentions are lowercased before matching because handles are stored lowercase
    (aligns with the engine's lowercase-only MENTION_RE), so ``@TestChar`` casts
    the same character as ``@testchar``."""
    handles = {m.lower() for m in MENTION_RE.findall(shot.description or "")}
    matched_ids: set[int] = set()
    if handles:
        chars = session.exec(
            select(Character).where(Character.handle.in_(handles))  # type: ignore[attr-defined]
        ).all()
        matched_ids = {c.id for c in chars if c.id is not None}
    existing = session.exec(
        select(ShotCharacter).where(ShotCharacter.shot_id == shot.id)
    ).all()
    for link in existing:
        if link.character_id not in matched_ids:
            session.delete(link)
    existing_ids = {link.character_id for link in existing}
    for cid in matched_ids - existing_ids:
        session.add(ShotCharacter(shot_id=shot.id, character_id=cid))


@router.post("/scenes/{scene_id}/shots", status_code=201)
def create_shot(
    scene_id: int, body: ShotCreate, request: Request, session: Session = Depends(get_session)
):
    scene = get_scene_or_404(session, scene_id)
    existing = session.exec(select(Shot).where(Shot.scene_id == scene_id)).all()
    shot = Shot(scene_id=scene_id, idx=len(existing), status="draft", **body.model_dump())
    session.add(shot)
    session.flush()
    refresh_shot_characters(session, shot)
    touch_project(session, scene.project_id)
    session.commit()
    session.refresh(shot)
    _publish_shot(request, shot)
    return jsonable_encoder(shot)


@router.post("/scenes/{scene_id}/shots/reorder")
def reorder_shots(
    scene_id: int, body: ShotReorder, session: Session = Depends(get_session)
):
    """Reorder shots within this scene. Ids from other scenes are moved into it
    (cross-scene drag). Unlisted shots of this scene keep relative order after."""
    scene = get_scene_or_404(session, scene_id)
    listed: list[Shot] = []
    for sid in body.shot_ids:
        shot = session.get(Shot, sid)
        if shot is None:
            raise HTTPException(status_code=400, detail=f"shot {sid} not found")
        listed.append(shot)
    listed_ids = {s.id for s in listed}
    rest = [
        s
        for s in session.exec(
            select(Shot).where(Shot.scene_id == scene_id).order_by(Shot.idx)  # type: ignore[arg-type]
        ).all()
        if s.id not in listed_ids
    ]
    moved_from: set[int] = set()
    for idx, shot in enumerate(listed + rest):
        if shot.scene_id != scene_id:
            moved_from.add(shot.scene_id)
            shot.scene_id = scene_id
        shot.idx = idx
        session.add(shot)
    # compact source scenes shots after cross-scene moves
    for src in moved_from:
        src_shots = session.exec(
            select(Shot).where(Shot.scene_id == src).order_by(Shot.idx)  # type: ignore[arg-type]
        ).all()
        for idx, shot in enumerate(src_shots):
            shot.idx = idx
            session.add(shot)
    touch_project(session, scene.project_id)
    session.commit()
    return {"shot_ids": [s.id for s in listed + rest]}


@router.get("/shots/{shot_id}")
def get_shot(shot_id: int, session: Session = Depends(get_session)):
    return jsonable_encoder(get_shot_or_404(session, shot_id))


@router.patch("/shots/{shot_id}")
def update_shot(
    shot_id: int, body: ShotUpdate, request: Request, session: Session = Depends(get_session)
):
    shot = get_shot_or_404(session, shot_id)
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(shot, key, value)
    session.add(shot)
    if "description" in changes:
        refresh_shot_characters(session, shot)
    scene = get_scene_or_404(session, shot.scene_id)
    touch_project(session, scene.project_id)
    session.commit()
    session.refresh(shot)
    _publish_shot(request, shot)
    return jsonable_encoder(shot)


@router.delete("/shots/{shot_id}", status_code=204)
def delete_shot(shot_id: int, request: Request, session: Session = Depends(get_session)):
    shot = get_shot_or_404(session, shot_id)
    takes = session.exec(select(Take).where(Take.shot_id == shot_id)).all()
    doomed_files = take_file_paths(takes)
    for take in takes:
        session.delete(take)
    for link in session.exec(select(ShotCharacter).where(ShotCharacter.shot_id == shot_id)):
        session.delete(link)
    session.flush()
    scene_id = shot.scene_id
    session.delete(shot)
    remaining = session.exec(
        select(Shot)
        .where(Shot.scene_id == scene_id, Shot.id != shot_id)
        .order_by(Shot.idx)  # type: ignore[arg-type]
    ).all()
    for idx, s in enumerate(remaining):
        s.idx = idx
        session.add(s)
    scene = get_scene_or_404(session, scene_id)
    touch_project(session, scene.project_id)
    session.commit()
    # rows are gone — reclaim the takes' files (guarded, best-effort)
    unlink_data_files(request.app.state.settings, doomed_files)
    return None


# -- takes / pick / approve ---------------------------------------------------


@router.get("/shots/{shot_id}/takes")
def list_takes(shot_id: int, session: Session = Depends(get_session)):
    get_shot_or_404(session, shot_id)
    takes = session.exec(
        select(Take).where(Take.shot_id == shot_id).order_by(Take.id)  # type: ignore[arg-type]
    ).all()
    return jsonable_encoder(takes)


@router.post("/takes/{take_id}/pick")
def pick_take(take_id: int, request: Request, session: Session = Depends(get_session)):
    take = session.get(Take, take_id)
    if take is None:
        raise HTTPException(status_code=404, detail="take not found")
    if take.status != "done":
        raise HTTPException(status_code=409, detail="only a finished take can be picked")
    shot = get_shot_or_404(session, take.shot_id)
    if take.kind == "video":
        shot.video_take_id = take.id
    else:
        shot.picked_take_id = take.id
        if shot.status == "draft":
            shot.status = "generated"
    session.add(shot)
    session.commit()
    session.refresh(shot)
    _publish_shot(request, shot)
    return jsonable_encoder(shot)


@router.delete("/takes/{take_id}", status_code=204)
def delete_take(take_id: int, request: Request, session: Session = Depends(get_session)):
    take = session.get(Take, take_id)
    if take is None:
        raise HTTPException(status_code=404, detail="take not found")
    shot = session.get(Shot, take.shot_id)
    unlink_data_files(request.app.state.settings, (take.file_path, take.thumb_path))
    session.delete(take)
    if shot is not None:
        changed = False
        if shot.picked_take_id == take_id:
            shot.picked_take_id = None
            if shot.status == "approved":
                shot.status = "generated"
            changed = True
        if shot.video_take_id == take_id:
            shot.video_take_id = None
            changed = True
        if changed:
            session.add(shot)
    session.commit()
    if shot is not None:
        _publish_shot(request, shot)
    return None


@router.post("/shots/{shot_id}/approve")
def approve_shot(shot_id: int, request: Request, session: Session = Depends(get_session)):
    shot = get_shot_or_404(session, shot_id)
    if shot.picked_take_id is None:
        raise HTTPException(
            status_code=409, detail="pick a take before approving this shot"
        )
    shot.status = "approved"
    session.add(shot)
    session.commit()
    session.refresh(shot)
    _publish_shot(request, shot)
    return jsonable_encoder(shot)


@router.post("/shots/{shot_id}/unapprove")
def unapprove_shot(shot_id: int, request: Request, session: Session = Depends(get_session)):
    shot = get_shot_or_404(session, shot_id)
    shot.status = "generated" if shot.picked_take_id is not None else "draft"
    session.add(shot)
    session.commit()
    session.refresh(shot)
    _publish_shot(request, shot)
    return jsonable_encoder(shot)
