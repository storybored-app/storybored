"""Projects CRUD + the nested board payload."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from sqlmodel import Session, select

from storybored.db import get_session
from storybored.models import Project, Scene, Shot, ShotCharacter, Take
from storybored.schemas import ProjectCreate, ProjectUpdate

router = APIRouter(prefix="/api", tags=["projects"])


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
    project = get_project_or_404(session, project_id)
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
            for shot in session.exec(select(Shot).where(Shot.id.in_(shot_ids))):  # type: ignore[attr-defined]
                session.delete(shot)
        for scene in session.exec(select(Scene).where(Scene.project_id == project_id)):
            session.delete(scene)
    session.delete(project)
    session.commit()
    return None
