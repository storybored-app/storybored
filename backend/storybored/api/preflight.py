# OWNED-BY: engine-agent
"""Shared engine pre-flight for the generation endpoints.

Every endpoint that enqueues a ComfyUI job (image generate, character
thumbnail, video render) runs the same gate before touching the queue:
resolve the pack, then check the pack's **effective** availability — the
user's ``engine_models`` slot swaps and ``engine_loras`` toggles applied —
so a render that *would* work is never rejected for a model the graph no
longer loads, and a render that *can't* work fails fast with a useful 409
instead of a cryptic engine error minutes later.
"""

from fastapi import HTTPException
from sqlmodel import Session

from storybored.api.settings_api import effective_setting
from storybored.config import Settings
from storybored.engine import registry
from storybored.engine.comfy_client import ComfyClient
from storybored.engine.graph import parse_engine_loras, parse_engine_models
from storybored.engine.registry import WorkflowPack


def resolve_pack(
    session: Session,
    settings: Settings,
    packs: dict[str, WorkflowPack],
    requested_id: str | None,
    kind: str,
) -> WorkflowPack:
    """Requested pack, else the user's default for `kind`, else the
    deterministic default. Raises 503/404/400 exactly like the callers used to.
    """
    workflow_id = requested_id or effective_setting(
        session, settings, f"default_{kind}_workflow"
    )
    if not workflow_id:
        workflow_id = registry.default_workflow_id(packs, kind=kind)
    if not workflow_id:
        raise HTTPException(status_code=503, detail=f"no {kind} workflow packs installed")
    pack = packs.get(workflow_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"unknown workflow '{workflow_id}'")
    if pack.manifest.get("kind", "image") != kind:
        raise HTTPException(
            status_code=400, detail=f"workflow '{workflow_id}' is not a {kind} workflow"
        )
    return pack


def unavailable_detail(workflow_id: str, availability: dict) -> str:
    """One human-readable line covering missing models and/or node classes."""
    parts = []
    if availability["missing_models"]:
        parts.append(f"missing models: {', '.join(availability['missing_models'])}")
    if availability["missing_nodes"]:
        parts.append(
            f"missing custom nodes: {', '.join(availability['missing_nodes'])} — "
            "install the node pack(s) that provide them in ComfyUI"
        )
    return f"workflow '{workflow_id}' is " + "; ".join(parts)


async def require_pack_available(
    session: Session, settings: Settings, pack: WorkflowPack, context: str
) -> None:
    """503 when the engine is unreachable, 409 when the effective model/node
    set is incomplete; returns silently when the pack can render."""
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
            detail=f"engine unreachable — {context}: {availability['error']}",
        )
    if not availability["available"]:
        raise HTTPException(
            status_code=409, detail=unavailable_detail(pack.id, availability)
        )
