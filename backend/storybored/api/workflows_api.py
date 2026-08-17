# OWNED-BY: engine-agent
"""Workflow pack registry endpoints.

- GET  /api/workflows                          registry + model availability
- POST /api/workflows/{id}/download-models     fetch missing catalog files
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from storybored.api.settings_api import effective_setting
from storybored.db import get_session
from storybored.engine import registry
from storybored.engine.catalog import load_catalog, model_file_info
from storybored.engine.comfy_client import ComfyClient, clear_object_info_cache
from storybored.engine.graph import parse_engine_loras, parse_engine_models
from storybored.models import Job

router = APIRouter(prefix="/api", tags=["workflows"])


@router.get("/workflows")
async def list_workflows(
    request: Request, refresh: bool = False, session: Session = Depends(get_session)
):
    """The registry payload. ``?refresh=true`` drops the 60s /object_info cache
    first, so availability reflects models/nodes installed seconds ago (the
    Settings "Refresh" button)."""
    settings = request.app.state.settings
    if refresh:
        clear_object_info_cache()
    comfy_url = effective_setting(session, settings, "comfyui_url")
    default_ids = {
        "image": effective_setting(session, settings, "default_image_workflow"),
        "video": effective_setting(session, settings, "default_video_workflow"),
    }
    engine_loras = parse_engine_loras(effective_setting(session, settings, "engine_loras"))
    engine_models = parse_engine_models(effective_setting(session, settings, "engine_models"))
    return await registry.list_workflows(
        settings,
        comfy_url,
        default_ids,
        engine_loras,
        engine_models,
        comfy_models_dir=effective_setting(session, settings, "comfy_models_dir"),
    )


class DownloadModelsBody(BaseModel):
    #: limit to these filenames; omit to download everything missing + verified
    filenames: list[str] | None = None


def _active_download_filenames(session: Session) -> set[str]:
    """Filenames with a model_download already queued/running (no double-queue)."""
    active: set[str] = set()
    rows = session.exec(
        select(Job).where(Job.type == "model_download", Job.status.in_(["queued", "running"]))  # type: ignore[attr-defined]
    ).all()
    for row in rows:
        try:
            name = json.loads(row.payload_json or "{}").get("filename")
        except ValueError:
            continue
        if name:
            active.add(str(name))
    return active


@router.post("/workflows/{workflow_id}/download-models")
async def download_models(
    workflow_id: str,
    request: Request,
    body: DownloadModelsBody | None = None,
    session: Session = Depends(get_session),
):
    """Enqueue io-lane download jobs for the pack's missing model files.

    Only files with a verified catalog source URL are downloadable; the rest
    come back in `skipped` (the UI shows their manual guidance instead).
    409 when COMFY_MODELS_DIR isn't configured — downloading only makes sense
    when StoryBored can write into ComfyUI's models directory.
    """
    body = body or DownloadModelsBody()
    settings = request.app.state.settings
    pack = registry.get_pack(settings, workflow_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"unknown workflow '{workflow_id}'")
    models_dir = effective_setting(session, settings, "comfy_models_dir")
    if not models_dir:
        raise HTTPException(
            status_code=409,
            detail="COMFY_MODELS_DIR is not configured — set it in Settings "
            "(only possible when StoryBored and the engine share a filesystem), "
            "or download the files manually",
        )

    comfy_url = effective_setting(session, settings, "comfyui_url")
    model_overrides = parse_engine_models(
        effective_setting(session, settings, "engine_models")
    ).get(pack.id, {})
    lora_overrides = parse_engine_loras(
        effective_setting(session, settings, "engine_loras")
    ).get(pack.id, [])
    availability = await registry.pack_availability(
        pack, ComfyClient(comfy_url), model_overrides, lora_overrides
    )
    if availability["error"]:
        raise HTTPException(
            status_code=503,
            detail=f"engine unreachable — cannot check what's missing: "
            f"{availability['error']}",
        )

    catalog = load_catalog(settings)
    requested = set(body.filenames) if body.filenames else None
    active = _active_download_filenames(session)
    job_ids: list[int] = []
    skipped: list[str] = []
    for detail in availability["missing_detail"]:
        filename = detail["filename"]
        if requested is not None and filename not in requested:
            continue
        if filename in active:
            continue  # already on its way
        info = model_file_info(filename, detail["class_type"], catalog)
        if not info["downloadable"]:
            skipped.append(filename)
            continue
        job = request.app.state.runner.enqueue(
            "model_download",
            {
                "workflow_id": pack.id,
                "filename": filename,
                "url": info["source"],
                "folder": info["folder"],
                **({"size_bytes": info["size_bytes"]} if "size_bytes" in info else {}),
            },
            lane="io",
        )
        job_ids.append(job.id)
    return {"job_ids": job_ids, "queued": len(job_ids), "skipped": skipped}
