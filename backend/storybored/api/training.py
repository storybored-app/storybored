# OWNED-BY: training-agent
"""Character training wizard endpoints.

POST /api/characters/wizard      — multipart images and/or image URLs →
                                   character(status=dataset) + dataset_prep job
GET  /api/training/{character_id} — prep report, sample paths, job states
POST /api/training/{character_id}/train — start the lora_train job (explicit
                                   user step after reviewing the prep report)

Every endpoint first runs restart recovery so a train that survived a backend
restart is re-attached before we report or mutate anything.
"""

import json
import logging
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from sqlmodel import Session, select

from storybored.config import Settings
from storybored.db import get_session
from storybored.models import Character, Job
from storybored.training import fetch
from storybored.training.lora_factory import (
    TrainerNotConfigured,
    recover_orphan_trains,
    resolve_trainer_dir,
)

log = logging.getLogger("storybored.training")

router = APIRouter(prefix="/api", tags=["training"])

HANDLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


# -- helpers ------------------------------------------------------------------


def _recover(request: Request) -> None:
    try:
        recover_orphan_trains(request.app.state.runner)
    except Exception:  # noqa: BLE001 - recovery must never break an endpoint
        log.exception("train recovery scan failed")


def _trainer_or_503(session: Session, settings: Settings):
    try:
        return resolve_trainer_dir(session, settings)
    except TrainerNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _get_character_or_404(session: Session, character_id: int) -> Character:
    character = session.get(Character, character_id)
    if character is None:
        raise HTTPException(status_code=404, detail="character not found")
    return character


def _parse_urls(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(u).strip() for u in data if str(u).strip()]
        except json.JSONDecodeError:
            pass
    return [p for p in re.split(r"[\s,]+", raw) if p]


def _job_name(handle: str) -> str:
    return f"{handle}-v1"


def _latest_job(session: Session, job_type: str, character_id: int) -> Job | None:
    rows = session.exec(
        select(Job).where(Job.type == job_type).order_by(Job.id.desc())  # type: ignore[attr-defined]
    ).all()
    for row in rows:
        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        if payload.get("character_id") == character_id:
            return row
    return None


def _upload_ext(filename: str, content_type: str) -> str | None:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in fetch.CONTENT_TYPE_EXT:
        return fetch.CONTENT_TYPE_EXT[ct]
    suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if suffix in fetch.ALLOWED_EXTS:
        return ".jpg" if suffix == ".jpeg" else suffix
    return None


def _save_upload(upload: UploadFile, dest, index: int) -> dict:
    name = upload.filename or f"image_{index}"
    ext = _upload_ext(name, upload.content_type or "")
    if ext is None:
        return {"filename": name, "ok": False, "error": "not a supported image type"}
    path = dest / f"upload_{index:03d}{ext}"
    size = 0
    with path.open("wb") as fh:
        while True:
            chunk = upload.file.read(fetch.CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > fetch.MAX_BYTES:
                fh.close()
                path.unlink(missing_ok=True)
                return {
                    "filename": name,
                    "ok": False,
                    "error": f"too large (over {fetch.MAX_BYTES} bytes)",
                }
            fh.write(chunk)
    if size == 0:
        path.unlink(missing_ok=True)
        return {"filename": name, "ok": False, "error": "empty file"}
    return {"filename": name, "ok": True, "path": str(path), "bytes": size}


# -- endpoints ----------------------------------------------------------------


@router.post("/characters/wizard", status_code=201)
def characters_wizard(
    request: Request,
    name: str = Form(...),
    handle: str = Form(...),
    trigger: str = Form(""),
    class_word: str = Form("person"),
    image_urls: str = Form(""),
    images: list[UploadFile] | None = File(default=None),
    session: Session = Depends(get_session),
):
    settings: Settings = request.app.state.settings
    _recover(request)
    _trainer_or_503(session, settings)

    handle_norm = handle.strip().lstrip("@").lower()
    if not HANDLE_RE.match(handle_norm):
        raise HTTPException(
            status_code=400,
            detail="handle must be lowercase letters/digits/underscores, starting with a letter",
        )
    if not name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    existing = session.exec(
        select(Character).where(Character.handle == handle_norm)
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"character @{handle_norm} already exists")

    uploads = [u for u in (images or []) if (u.filename or "").strip()]
    urls = _parse_urls(image_urls)
    total = len(uploads) + len(urls)
    if total == 0:
        raise HTTPException(status_code=400, detail="add at least one image (upload or URL)")
    if total > fetch.MAX_FILES:
        raise HTTPException(
            status_code=400, detail=f"too many images ({total}, max {fetch.MAX_FILES})"
        )

    staging = fetch.staging_dir(settings, handle_norm, clean=True)
    upload_results = [_save_upload(u, staging, i) for i, u in enumerate(uploads)]
    fetch_results = fetch.fetch_images(urls, staging) if urls else []
    staged = sum(1 for r in upload_results + fetch_results if r.get("ok"))
    if staged == 0:
        raise HTTPException(
            status_code=400,
            detail="no usable images — every upload/URL was rejected",
        )

    trigger_final = trigger.strip() or f"{handle_norm}x7"
    character = Character(
        name=name.strip(),
        handle=handle_norm,
        trigger=trigger_final,
        class_word=class_word.strip() or "person",
        status="dataset",
    )
    session.add(character)
    session.commit()
    session.refresh(character)
    request.app.state.bus.publish("character", jsonable_encoder(character))

    job = request.app.state.runner.enqueue(
        "dataset_prep",
        {
            "character_id": character.id,
            "handle": handle_norm,
            "job_name": _job_name(handle_norm),
            "staging_dir": str(staging),
            "trigger": trigger_final,
            "class_word": character.class_word,
        },
    )
    return {
        "character": jsonable_encoder(character),
        "job_id": job.id,
        "staged": staged,
        "uploads": upload_results,
        "fetches": fetch_results,
    }


@router.get("/training/{character_id}")
def training_status(
    character_id: int, request: Request, session: Session = Depends(get_session)
):
    settings: Settings = request.app.state.settings
    _recover(request)
    character = _get_character_or_404(session, character_id)

    prep_job = _latest_job(session, "dataset_prep", character_id)
    train_job = _latest_job(session, "lora_train", character_id)

    report_md = ""
    if prep_job is not None and prep_job.status == "done" and prep_job.result_json:
        try:
            report_md = json.loads(prep_job.result_json).get("report_md", "")
        except json.JSONDecodeError:
            report_md = ""
    if not report_md:
        try:
            factory = resolve_trainer_dir(session, settings)
            report_path = factory / "jobs" / _job_name(character.handle) / "report.md"
            if report_path.is_file():
                report_md = report_path.read_text(errors="replace")
        except TrainerNotConfigured:
            pass

    staging = settings.data_path / "training" / character.handle / "raw"
    samples = sorted(p.name for p in staging.iterdir() if p.is_file()) if staging.is_dir() else []

    return {
        "character": jsonable_encoder(character),
        "prep_job": jsonable_encoder(prep_job) if prep_job else None,
        "train_job": jsonable_encoder(train_job) if train_job else None,
        "report_md": report_md,
        "samples": samples,
    }


@router.post("/training/{character_id}/train")
def start_training(
    character_id: int, request: Request, session: Session = Depends(get_session)
):
    settings: Settings = request.app.state.settings
    _recover(request)
    _trainer_or_503(session, settings)
    character = _get_character_or_404(session, character_id)

    train_job = _latest_job(session, "lora_train", character_id)
    if train_job is not None and train_job.status in ("queued", "running"):
        raise HTTPException(status_code=409, detail="training is already queued or running")
    if character.status == "training":
        raise HTTPException(status_code=409, detail="character is already training")

    prep_job = _latest_job(session, "dataset_prep", character_id)
    if prep_job is None or prep_job.status != "done":
        state = prep_job.status if prep_job is not None else "missing"
        raise HTTPException(
            status_code=409,
            detail=f"dataset prep must finish before training (prep job is {state})",
        )
    prep_payload = json.loads(prep_job.payload_json or "{}")
    job_name = prep_payload.get("job_name") or _job_name(character.handle)

    job = request.app.state.runner.enqueue(
        "lora_train",
        {"character_id": character_id, "handle": character.handle, "job_name": job_name},
    )
    return {"job_id": job.id}
