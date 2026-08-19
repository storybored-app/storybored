# OWNED-BY: engine-agent
"""Workflow pack registry endpoints.

- GET  /api/workflows                          registry + model availability
- POST /api/workflows/{id}/download-models     fetch missing catalog files
- POST /api/workflows/analyze                  propose a manifest for a graph
- POST /api/workflows/import                   create a user pack from the wizard
- DELETE /api/workflows/{id}                   remove a user pack (DATA_DIR only)
"""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from storybored.db import get_session
from storybored.engine import registry
from storybored.engine.analyze import analyze_graph, is_api_format, is_ui_format
from storybored.engine.catalog import load_catalog, model_file_info
from storybored.engine.comfy_client import ComfyClient, clear_object_info_cache
from storybored.engine.graph import (
    apply_engine_lora_overrides,
    apply_model_overrides,
    parse_engine_loras,
    parse_engine_models,
)
from storybored.engine.validate import validate_pack, write_required_models
from storybored.models import Job
from storybored.settings_store import effective_setting

router = APIRouter(prefix="/api", tags=["workflows"])

#: how many recent finished jobs per engine feed the speed median
TIMING_WINDOW = 20
#: render-shaped job types whose duration reflects one engine's speed
TIMING_JOB_TYPES = ("image_gen", "video_gen", "character_thumb")


def measured_render_timings(session: Session) -> dict[str, tuple[float, int]]:
    """{workflow_id: (median seconds per frame/clip, sample count)} from the
    last TIMING_WINDOW completed render jobs per engine on this machine.

    image_gen durations are divided by the job's n_takes so multi-take jobs
    contribute per-frame numbers. First-run jobs include model-load time —
    the median absorbs that once there are a few samples, and for a single
    sample it is still the honest answer to "how long did this take here".
    """
    jobs = session.exec(
        select(Job)
        .where(Job.status == "done", Job.type.in_(TIMING_JOB_TYPES))  # type: ignore[attr-defined]
        .order_by(Job.id.desc())  # type: ignore[attr-defined]
        .limit(500)
    ).all()
    per_engine: dict[str, list[float]] = {}
    for job in jobs:
        if job.started_at is None or job.finished_at is None:
            continue
        try:
            payload = json.loads(job.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        workflow_id = str(payload.get("workflow_id") or "")
        if not workflow_id:
            continue
        samples = per_engine.setdefault(workflow_id, [])
        if len(samples) >= TIMING_WINDOW:
            continue
        duration = (job.finished_at - job.started_at).total_seconds()
        frames = max(1, int(payload.get("n_takes") or 1)) if job.type == "image_gen" else 1
        if duration > 0:
            samples.append(duration / frames)
    out: dict[str, tuple[float, int]] = {}
    for workflow_id, samples in per_engine.items():
        if samples:
            ordered = sorted(samples)
            out[workflow_id] = (round(ordered[len(ordered) // 2], 1), len(samples))
    return out


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
    entries = await registry.list_workflows(
        settings,
        comfy_url,
        default_ids,
        engine_loras,
        engine_models,
        comfy_models_dir=effective_setting(session, settings, "comfy_models_dir"),
    )
    # measured render speed on THIS machine, from real completed jobs — the
    # honest counterpart to the modeled `fit` (measured beats modeled)
    timings = measured_render_timings(session)
    for entry in entries:
        median_s, samples = timings.get(entry["id"], (None, 0))
        entry["median_render_s"] = median_s
        entry["timing_samples"] = samples
    return entries


@router.get("/workflows/{workflow_id}/graph")
def export_workflow_graph(
    workflow_id: str,
    request: Request,
    effective: bool = False,
    session: Session = Depends(get_session),
):
    """Download a pack's ComfyUI graph (API format) — importable straight back
    into ComfyUI. ``?effective=true`` first applies the user's engine
    customizations (model swaps + LoRA toggles/appends from Settings), so the
    file matches what StoryBored would actually submit. Per-shot splices
    (characters, style LoRAs, prompt text) are render-time and not included."""
    settings = request.app.state.settings
    pack = registry.get_pack(settings, workflow_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"unknown workflow '{workflow_id}'")
    graph = pack.load_graph()
    suffix = "api"
    if effective:
        engine_loras = parse_engine_loras(
            effective_setting(session, settings, "engine_loras")
        ).get(workflow_id, [])
        engine_models = parse_engine_models(
            effective_setting(session, settings, "engine_models")
        ).get(workflow_id, {})
        apply_model_overrides(graph, pack.manifest, engine_models)
        apply_engine_lora_overrides(graph, engine_loras)
        suffix = "effective.api"
    return JSONResponse(
        graph,
        headers={
            "Content-Disposition": f'attachment; filename="{workflow_id}.{suffix}.json"'
        },
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


# -- import wizard -------------------------------------------------------------

#: pack ids are folder names: lowercase slug, no traversal characters possible
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: exact ComfyUI menu wording (docs/WORKFLOWS.md) — cite it, don't paraphrase
UI_FORMAT_DETAIL = (
    "This file is ComfyUI's editor format, not the API format packs need. "
    'In ComfyUI, enable dev mode (Settings → "Enable Dev mode Options"), '
    'then export the workflow with "Save (API Format)" and upload that file.'
)
NOT_A_GRAPH_DETAIL = (
    "This file is not an API-format ComfyUI graph (top-level keys must be node "
    "ids mapping to objects with a class_type). Export it with "
    '"Save (API Format)" — enable dev mode first (Settings → '
    '"Enable Dev mode Options").'
)


def _checked_graph(graph: object) -> dict:
    """The graph if it's API-format, else a 400 explaining how to export one."""
    if is_ui_format(graph):
        raise HTTPException(status_code=400, detail=UI_FORMAT_DETAIL)
    if not is_api_format(graph):
        raise HTTPException(status_code=400, detail=NOT_A_GRAPH_DETAIL)
    return graph  # type: ignore[return-value]


class AnalyzeBody(BaseModel):
    graph: dict | list


@router.post("/workflows/analyze")
def analyze_workflow(body: AnalyzeBody):
    """Propose a manifest draft for an uploaded API-format graph.

    Every detection is a suggestion (node id + confidence); ambiguities come
    back as candidate lists + warnings, never errors. Editor-format files are
    rejected with instructions for exporting the API format.
    """
    return analyze_graph(_checked_graph(body.graph))


class ImportWorkflowBody(BaseModel):
    id: str
    name: str
    kind: str
    graph: dict | list
    parameters: list[dict]
    output_node: str
    description: str = ""
    character_injection: dict | None = None
    lora_injection: dict | None = None
    model_slots: list[dict] | None = None
    frame_conditioning: dict | None = None


def _user_workflows_dir(settings) -> Path:
    return settings.data_path / "workflows"


async def _pack_entry(pack, session: Session, settings) -> dict:
    """Availability payload for one pack (the wizard's success panel)."""
    comfy_url = effective_setting(session, settings, "comfyui_url")
    availability = await registry.pack_availability(pack, ComfyClient(comfy_url))
    catalog = load_catalog(settings)
    return {
        "id": pack.id,
        "name": pack.manifest.get("name", pack.id),
        "kind": pack.manifest.get("kind", "image"),
        "available": availability["available"],
        "missing_models": availability["missing_models"],
        "missing_models_info": [
            model_file_info(d["filename"], d["class_type"], catalog)
            for d in availability["missing_detail"]
        ],
        "missing_nodes": availability["missing_nodes"],
        "error": availability["error"],
        "removable": True,
    }


@router.post("/workflows/import", status_code=201)
async def import_workflow(
    body: ImportWorkflowBody, request: Request, session: Session = Depends(get_session)
):
    """Write a confirmed wizard draft as a pack under DATA_DIR/workflows/{id}.

    Runs the exact validate-pack checks before anything lands; the folder is
    staged and moved into place only when the pack is valid, so a failed
    import leaves nothing behind. ``required_models`` is always derived from
    the graph (same derivation as ``validate-pack --write``).
    """
    settings = request.app.state.settings
    graph = _checked_graph(body.graph)
    pack_id = body.id.strip()
    if not SLUG_RE.match(pack_id):
        raise HTTPException(
            status_code=400,
            detail="engine id must be a lowercase slug (letters, digits, hyphens; "
            "max 64 characters), e.g. 'my-sdxl'",
        )
    if pack_id in registry.load_packs(settings):
        raise HTTPException(
            status_code=409, detail=f"an engine pack named '{pack_id}' already exists"
        )
    user_root = _user_workflows_dir(settings)
    user_root.mkdir(parents=True, exist_ok=True)
    target = (user_root / pack_id).resolve()
    # belt-and-braces with the slug check: never escape the workflows dir
    if not target.is_relative_to(user_root.resolve()) or target == user_root.resolve():
        raise HTTPException(status_code=400, detail="invalid engine id")
    if target.exists():
        raise HTTPException(
            status_code=409, detail=f"an engine pack named '{pack_id}' already exists"
        )

    manifest: dict = {
        "id": pack_id,
        "name": body.name.strip() or pack_id,
        "kind": body.kind,
        "description": body.description.strip(),
        "graph": "graph.json",
        "parameters": body.parameters,
        "output_node": body.output_node,
    }
    for key in ("character_injection", "lora_injection", "model_slots", "frame_conditioning"):
        value = getattr(body, key)
        if value:
            manifest[key] = value

    # Stage one level deeper than the registry's */manifest.json scan so a
    # half-written pack can never be picked up, then validate and move.
    staging = Path(tempfile.mkdtemp(prefix=".import-", dir=user_root))
    try:
        pack_dir = staging / pack_id
        pack_dir.mkdir()
        (pack_dir / "graph.json").write_text(
            json.dumps(graph, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (pack_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        # required_models comes from the graph itself — same derivation as
        # `validate-pack --write`, so wizard packs can never ship with drift
        write_required_models(pack_dir)
        report = validate_pack(pack_dir)
        if not report.ok:
            raise HTTPException(
                status_code=400,
                detail="the pack failed validation: " + "; ".join(report.errors),
            )
        os.replace(pack_dir, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    pack = registry.get_pack(settings, pack_id)
    if pack is None:  # pragma: no cover - the folder was just written
        raise HTTPException(status_code=500, detail="imported pack failed to register")
    return await _pack_entry(pack, session, settings)


@router.delete("/workflows/{workflow_id}", status_code=204)
def delete_workflow(workflow_id: str, request: Request):
    """Remove a user-imported pack. Repo-shipped packs are not removable."""
    settings = request.app.state.settings
    pack = registry.get_pack(settings, workflow_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"unknown workflow '{workflow_id}'")
    user_root = _user_workflows_dir(settings).resolve()
    pack_dir = pack.dir.resolve()
    if not pack_dir.is_relative_to(user_root):
        raise HTTPException(
            status_code=403, detail="built-in engine packs can't be removed"
        )
    shutil.rmtree(pack_dir)
