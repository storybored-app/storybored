"""Projects CRUD + the nested board payload + delete cleanup helpers."""

import shutil
from collections.abc import Iterable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from sqlmodel import Session, select

from storybored.config import Settings
from storybored.db import get_session
from storybored.models import Character, Job, Project, Scene, Shot, ShotCharacter, Take
from storybored.schemas import ProjectCreate, ProjectUpdate

router = APIRouter(prefix="/api", tags=["projects"])


def unlink_data_files(settings: Settings, rel_paths: Iterable[str | None]) -> None:
    """Best-effort unlink of DATA_DIR-relative files.

    Every path is resolved and checked with is_relative_to before touching the
    filesystem, so a hostile/corrupt DB row can never delete outside DATA_DIR."""
    for rel in rel_paths:
        if not rel:
            continue
        try:
            path = (settings.data_path / rel).resolve()
            if path.is_relative_to(settings.data_path) and path.is_file():
                path.unlink()
        except OSError:
            pass


def take_file_paths(takes: Iterable[Take]) -> list[str | None]:
    """The on-disk files (still/clip + thumb) belonging to the given takes."""
    paths: list[str | None] = []
    for take in takes:
        paths.extend((take.file_path, take.thumb_path))
    return paths


def remove_project_trees(settings: Settings, project_id: int) -> None:
    """Remove media/{id} and exports/{id}, guarded to stay inside DATA_DIR."""
    for base in (settings.media_path, settings.exports_path):
        try:
            tree = (base / str(project_id)).resolve()
            if tree.is_relative_to(settings.data_path) and tree.is_dir():
                shutil.rmtree(tree, ignore_errors=True)
        except OSError:
            pass


def get_project_or_404(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def touch_project(session: Session, project_id: int) -> None:
    project = session.get(Project, project_id)
    if project is not None:
        project.updated_at = datetime.now(UTC)
        session.add(project)


def board_payload(session: Session, project: Project) -> dict:
    """Full nested board: project → scenes → shots → takes."""
    data = jsonable_encoder(project)
    scenes = session.exec(
        select(Scene).where(Scene.project_id == project.id).order_by(Scene.idx)  # type: ignore[arg-type]
    ).all()
    data["scenes"] = []
    for scene in scenes:
        scene_data = jsonable_encoder(scene)
        shots = session.exec(
            select(Shot).where(Shot.scene_id == scene.id).order_by(Shot.idx)  # type: ignore[arg-type]
        ).all()
        scene_data["shots"] = []
        for shot in shots:
            shot_data = jsonable_encoder(shot)
            takes = session.exec(
                select(Take).where(Take.shot_id == shot.id).order_by(Take.id)  # type: ignore[arg-type]
            ).all()
            shot_data["takes"] = jsonable_encoder(takes)
            scene_data["shots"].append(shot_data)
        data["scenes"].append(scene_data)
    return data


def project_thumbnail(session: Session, project_id: int) -> str | None:
    """Board-order thumbnail: first scene/shot whose picked still is done;
    falls back to the newest finished image take anywhere in the project."""
    picked = session.exec(
        select(Take)
        .join(Shot, Shot.picked_take_id == Take.id)  # type: ignore[arg-type]
        .join(Scene, Scene.id == Shot.scene_id)  # type: ignore[arg-type]
        .where(Scene.project_id == project_id)
        .where(Take.status == "done")
        .order_by(Scene.idx, Shot.idx)  # type: ignore[arg-type]
    ).first()
    if picked is None:
        picked = session.exec(
            select(Take)
            .join(Shot, Shot.id == Take.shot_id)  # type: ignore[arg-type]
            .join(Scene, Scene.id == Shot.scene_id)  # type: ignore[arg-type]
            .where(Scene.project_id == project_id)
            .where(Take.status == "done")
            .where(Take.kind == "image")
            .order_by(Take.id.desc())  # type: ignore[attr-defined]
        ).first()
    if picked is None:
        return None
    return picked.thumb_path or picked.file_path


@router.get("/projects")
def list_projects(session: Session = Depends(get_session)):
    projects = session.exec(
        select(Project).order_by(Project.updated_at.desc())  # type: ignore[attr-defined]
    ).all()
    out = []
    for project in projects:
        data = jsonable_encoder(project)
        data["thumbnail_path"] = project_thumbnail(session, project.id)
        out.append(data)
    return out


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate, session: Session = Depends(get_session)):
    project = Project(
        title=body.title, description=body.description, aspect_ratio=body.aspect_ratio
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return jsonable_encoder(project)


@router.get("/projects/{project_id}")
def get_project(project_id: int, session: Session = Depends(get_session)):
    project = get_project_or_404(session, project_id)
    return board_payload(session, project)


@router.patch("/projects/{project_id}")
def update_project(
    project_id: int, body: ProjectUpdate, session: Session = Depends(get_session)
):
    project = get_project_or_404(session, project_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(project, key, value)
    project.updated_at = datetime.now(UTC)
    session.add(project)
    session.commit()
    session.refresh(project)
    return jsonable_encoder(project)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: int, request: Request, session: Session = Depends(get_session)):
    """Delete the project, its rows, its jobs, and its media/exports on disk."""
    project = get_project_or_404(session, project_id)
    settings: Settings = request.app.state.settings
    runner = request.app.state.runner

    # 1) cancel any in-flight work for this project, then forget its job rows
    jobs = session.exec(select(Job).where(Job.project_id == project_id)).all()
    for job in jobs:
        if job.status in ("queued", "running") and job.id is not None:
            runner.cancel(job.id)
    for job in jobs:
        session.delete(job)
    session.flush()

    # 2) delete rows children-first (FKs are enforced; no relationships are
    #    mapped, so flush between stages to pin the ordering)
    scene_ids = session.exec(select(Scene.id).where(Scene.project_id == project_id)).all()
    if scene_ids:
        shot_ids = session.exec(select(Shot.id).where(Shot.scene_id.in_(scene_ids))).all()  # type: ignore[attr-defined]
        if shot_ids:
            for take in session.exec(select(Take).where(Take.shot_id.in_(shot_ids))):  # type: ignore[attr-defined]
                session.delete(take)
            for link in session.exec(
                select(ShotCharacter).where(ShotCharacter.shot_id.in_(shot_ids))  # type: ignore[attr-defined]
            ):
                session.delete(link)
            session.flush()
            for shot in session.exec(select(Shot).where(Shot.id.in_(shot_ids))):  # type: ignore[attr-defined]
                session.delete(shot)
            session.flush()
        for scene in session.exec(select(Scene).where(Scene.project_id == project_id)):
            session.delete(scene)
        session.flush()
    session.delete(project)

    # 3) characters whose auto-thumbnail was a take inside this project would
    #    dangle — clear them (a deliberate upload elsewhere is unaffected)
    changed_chars = []
    for char in session.exec(
        select(Character).where(
            Character.thumbnail_path.like(f"media/{project_id}/%")  # type: ignore[attr-defined]
        )
    ):
        char.thumbnail_path = None
        session.add(char)
        changed_chars.append(char)
    session.commit()

    # 4) with the rows gone, reclaim the disk trees
    remove_project_trees(settings, project_id)
    for char in changed_chars:
        request.app.state.bus.publish("character", jsonable_encoder(char))
    return None
