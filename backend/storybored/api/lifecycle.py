# OWNED-BY: lifecycle-agent
"""Project archive endpoints (.storybored files).

- POST /api/projects/{id}/export           → {job_id} (project_export, lane "io")
- GET  /api/projects/{id}/export/download  → the finished archive
- POST /api/projects/import                → multipart zip + mode=merge|rename
"""

import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from sqlmodel import Session

from storybored.api.projects import get_project_or_404
from storybored.db import get_session
from storybored.export.archive import (
    ArchiveError,
    archive_path,
    import_archive,
    import_result_payload,
)

router = APIRouter(prefix="/api", tags=["lifecycle"])

IMPORT_MODES = ("merge", "rename")


@router.post("/projects/{project_id}/export")
def export_project(
    project_id: int, request: Request, session: Session = Depends(get_session)
):
    """Enqueue a project_export job (non-GPU "io" lane — never blocks renders)."""
    get_project_or_404(session, project_id)
    job = request.app.state.runner.enqueue(
        "project_export", {"project_id": project_id}, lane="io", project_id=project_id
    )
    return {"job_id": job.id}


@router.get("/projects/{project_id}/export/download")
def download_export(
    project_id: int, request: Request, session: Session = Depends(get_session)
):
    get_project_or_404(session, project_id)
    settings = request.app.state.settings
    try:
        target = archive_path(settings, project_id).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="no export archive yet") from None
    # same traversal guard as /api/media: nothing outside DATA_DIR is served
    if not target.is_relative_to(settings.data_path) or not target.is_file():
        raise HTTPException(
            status_code=404, detail="no export archive yet — run an export first"
        )
    return FileResponse(
        target,
        media_type="application/zip",
        filename=f"project-{project_id}.storybored",
    )


@router.post("/projects/import", status_code=201)
def import_project(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form("merge"),
    session: Session = Depends(get_session),
):
    """Import a .storybored archive as a brand-new project.

    mode=merge (default) reuses existing characters with the same @handle;
    mode=rename keeps imported characters separate (colliding handles get a
    numeric suffix and @mentions are rewritten). Missing LoRAs / engine packs
    never fail the import — they come back in `warnings`."""
    if mode not in IMPORT_MODES:
        raise HTTPException(
            status_code=400, detail=f"mode must be one of: {', '.join(IMPORT_MODES)}"
        )
    settings = request.app.state.settings
    settings.data_path.mkdir(parents=True, exist_ok=True)

    # import_archive removes its own half-extracted trees on failure;
    # the rollbacks here discard the half-written rows.
    with tempfile.NamedTemporaryFile(
        dir=settings.data_path, suffix=".storybored.upload"
    ) as spool:
        shutil.copyfileobj(file.file, spool)
        spool.flush()
        try:
            with zipfile.ZipFile(Path(spool.name)) as zf:
                result = import_archive(session, settings, zf, mode)
                session.commit()
        except zipfile.BadZipFile:
            session.rollback()
            raise HTTPException(
                status_code=400, detail="that file is not a .storybored archive"
            ) from None
        except ArchiveError as exc:
            session.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except Exception:
            session.rollback()
            raise

    bus = request.app.state.bus
    for row in result.created_rows:
        # keep the characters page live (invariant: mutations publish events)
        bus.publish("character", jsonable_encoder(row))
    session.refresh(result.project)
    payload = import_result_payload(result)
    payload["project"] = jsonable_encoder(result.project)
    return payload
