# OWNED-BY: engine-agent
"""POST /api/shots/{id}/generate — validate the workflow, enqueue an image_gen job."""

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlmodel import Session

from storybored.api.preflight import require_pack_available, resolve_pack
from storybored.api.shots import get_shot_or_404
from storybored.db import get_session
from storybored.engine import registry

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

    pack = resolve_pack(session, settings, packs, body.workflow_id, kind="image")
    workflow_id = pack.id
    await require_pack_available(
        session, settings, pack, f"cannot validate workflow '{workflow_id}'"
    )

    shot.status = "queued"
    session.add(shot)
    session.commit()
    session.refresh(shot)
    request.app.state.bus.publish("shot", jsonable_encoder(shot))

    job = request.app.state.runner.enqueue(
        "image_gen",
        {
            "shot_id": shot_id,
            "workflow_id": workflow_id,
            "n_takes": body.n_takes,
            "params": body.params,
        },
    )
    return {"job_id": job.id}
