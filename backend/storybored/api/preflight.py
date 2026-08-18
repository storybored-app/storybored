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
from sqlmodel import Session, select

from storybored.api.settings_api import effective_setting
from storybored.config import Settings
from storybored.engine import families, registry
from storybored.engine.comfy_client import ComfyClient
from storybored.engine.graph import parse_engine_loras, parse_engine_models
from storybored.engine.registry import WorkflowPack
from storybored.models import Character, ShotCharacter


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


def require_characters_compatible(
    session: Session, pack: WorkflowPack, shot_id: int
) -> None:
    """409 when any of the shot's @characters carries a LoRA family that
    differs from the pack's declared ``lora_family``.

    Character LoRAs are model-family-bound — splicing a Krea 2 LoRA into a
    Z-Image graph renders garbage — so this fails fast with the character and
    both families named. NULL/absent on either side means unknown/agnostic
    and never blocks (pre-family rows must not start failing after upgrade).
    """
    pack_family = families.pack_family(pack.manifest)
    if not pack_family:
        return
    links = session.exec(
        select(ShotCharacter).where(ShotCharacter.shot_id == shot_id)
    ).all()
    ids = [link.character_id for link in links]
    if not ids:
        return
    characters = session.exec(
        select(Character).where(Character.id.in_(ids))  # type: ignore[attr-defined]
    ).all()
    conflicts = [
        c
        for c in sorted(characters, key=lambda c: c.handle)
        if c.lora_family and c.lora_family != pack_family
    ]
    if not conflicts:
        return
    clauses = "; ".join(
        f"@{c.handle} was trained for {families.family_label(c.lora_family)}"
        f" — this engine renders with {families.family_label(pack_family)}"
        for c in conflicts
    )
    noun = "mention" if len(conflicts) == 1 else "mentions"
    raise HTTPException(
        status_code=409,
        detail=f"{clauses}; switch engines or remove the {noun}",
    )


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
