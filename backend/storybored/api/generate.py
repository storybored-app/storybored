# OWNED-BY: engine-agent
"""POST /api/shots/{id}/generate — validate the workflow, enqueue an image_gen job."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlmodel import Session

from storybored.api.settings_api import effective_setting
from storybored.api.shots import get_shot_or_404
from storybored.db import get_session
from storybored.engine import registry
from storybored.engine.comfy_client import ComfyClient
from storybored.models import Scene

router = APIRouter(prefix="/api", tags=["generate"])


class GenerateRequest(BaseModel):
    workflow_id: str | None = None
    n_takes: int = Field(default=1, ge=1, le=16)
    params: dict = Field(default_factory=dict)


@router.post("/shots/{shot_id}/generate")
async def generate_shot(
    shot_id: int,
    body: GenerateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    shot = get_shot_or_404(session, shot_id)
    settings = request.app.state.settings
    packs = registry.load_packs(settings)

    workflow_id = body.workflow_id or effective_setting(
        session, settings, "default_image_workflow"
    )
    if not workflow_id:
        workflow_id = registry.default_workflow_id(packs, kind="image")
    if not workflow_id:
        raise HTTPException(status_code=503, detail="no image workflow packs installed")

    pack = packs.get(workflow_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"unknown workflow '{workflow_id}'")
    if pack.manifest.get("kind", "image") != "image":
        raise HTTPException(
            status_code=400, detail=f"workflow '{workflow_id}' is not an image workflow"
        )

    comfy_url = effective_setting(session, settings, "comfyui_url")
    availability = await registry.pack_availability(pack, ComfyClient(comfy_url))
    if availability["error"]:
        raise HTTPException(
            status_code=503,
            detail=f"engine unreachable — cannot validate workflow '{workflow_id}': "
            f"{availability['error']}",
        )
    if not availability["available"]:
        missing = ", ".join(availability["missing_models"])
        raise HTTPException(
            status_code=409,
            detail=f"workflow '{workflow_id}' is missing models: {missing}",
        )

    shot.status = "queued"
    session.add(shot)
    session.commit()
    session.refresh(shot)
    request.app.state.bus.publish("shot", jsonable_encoder(shot))

    scene = session.get(Scene, shot.scene_id)
    job = request.app.state.runner.enqueue(
        "image_gen",
        {
            "shot_id": shot_id,
            "workflow_id": workflow_id,
            "n_takes": body.n_takes,
            "params": body.params,
        },
        project_id=scene.project_id if scene is not None else None,
    )
    return {"job_id": job.id}
