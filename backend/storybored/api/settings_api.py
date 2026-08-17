"""Runtime settings: DB-backed overrides on top of env config.

A `setting` row's value wins over the corresponding env var when set — used
for LLM config and workflow defaults that users edit in the Settings screen.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from storybored.config import Settings
from storybored.db import get_session
from storybored.engine.comfy_client import clear_object_info_cache
from storybored.models import Setting
from storybored.schemas import SettingsUpdate

router = APIRouter(prefix="/api", tags=["settings"])

#: env-backed keys that may be overridden at runtime via the setting table
OVERRIDABLE = {
    "llm_base_url",
    "llm_api_key",
    "llm_model",
    "comfyui_url",
    "comfy_loras_dir",
    "comfy_models_dir",
    "lora_factory_dir",
    # not env-backed: "1" once the first-run setup wizard has finished (or was
    # explicitly skipped) — the UI stops auto-offering it after that.
    "setup_complete",
    "default_image_workflow",
    "default_video_workflow",
    "style_loras",
    "engine_loras",
    "engine_models",
}


def _validate_style_loras(raw: str) -> None:
    """style_loras is a JSON list of {lora_name, strength?, enabled?} — reject
    anything else at write time so renders never see a malformed setting."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="style_loras must be valid JSON") from None
    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail="style_loras must be a JSON list")
    for item in data:
        _check_lora_entry(item, allow_node=False)


def _check_lora_entry(item: object, *, allow_node: bool) -> None:
    """Shared shape check for one style/engine LoRA entry."""
    if not isinstance(item, dict):
        raise HTTPException(status_code=400, detail="each LoRA entry must be an object")
    node = str(item.get("node", "")).strip() if allow_node else ""
    name = str(item.get("lora_name", "")).strip()
    if not node and not name:
        detail = (
            "each engine LoRA entry needs a node or a lora_name"
            if allow_node
            else "each style LoRA entry needs a lora_name"
        )
        raise HTTPException(status_code=400, detail=detail)
    if "strength" in item:
        strength = item["strength"]
        if isinstance(strength, bool) or not isinstance(strength, (int, float)):
            raise HTTPException(status_code=400, detail="LoRA strength must be a number")
        if not -5 <= strength <= 5:
            raise HTTPException(
                status_code=400, detail="LoRA strength must be between -5 and 5"
            )
    if not isinstance(item.get("enabled", True), bool):
        raise HTTPException(status_code=400, detail="LoRA enabled must be true or false")


def _validate_engine_loras(raw: str) -> None:
    """engine_loras: JSON object of pack id → list of node-override / append entries."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="engine_loras must be valid JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=400, detail="engine_loras must be a JSON object keyed by engine id"
        )
    for pack_id, items in data.items():
        if not isinstance(items, list):
            raise HTTPException(
                status_code=400, detail=f"engine_loras['{pack_id}'] must be a list"
            )
        for item in items:
            _check_lora_entry(item, allow_node=True)


def _validate_engine_models(raw: str) -> None:
    """engine_models: JSON object of pack id → {slot key: model filename}."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="engine_models must be valid JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=400, detail="engine_models must be a JSON object keyed by engine id"
        )
    for pack_id, slots in data.items():
        if not isinstance(slots, dict):
            raise HTTPException(
                status_code=400, detail=f"engine_models['{pack_id}'] must be an object"
            )
        for key, name in slots.items():
            if not isinstance(name, str) or not name.strip():
                raise HTTPException(
                    status_code=400,
                    detail=f"engine_models['{pack_id}']['{key}'] must be a model filename",
                )


def _is_secret_key(key: str) -> bool:
    """Keys whose values must never be returned (API keys, tokens, secrets)."""
    return key == "llm_api_key" or key.endswith(("_key", "_token", "_secret"))


def resolve_setting(
    session: Session, settings: Settings, key: str
) -> tuple[str, str]:
    """Return (value, source) — source is 'override' (DB), 'env', or 'unset'."""
    row = session.get(Setting, key)
    if row is not None and row.value:
        return row.value, "override"
    env_val = str(getattr(settings, key, "") or "")
    if env_val:
        return env_val, "env"
    return "", "unset"


def effective_setting(session: Session, settings: Settings, key: str) -> str:
    """DB override if present and non-empty, else env value, else ""."""
    return resolve_setting(session, settings, key)[0]


@router.get("/settings")
def get_settings(request: Request, session: Session = Depends(get_session)):
    settings: Settings = request.app.state.settings
    # Redact secret values from `overrides` exactly as `effective` does — expose
    # only a `<key>_set` boolean so a LAN peer can't read the raw API key back.
    overrides: dict[str, object] = {}
    for row in session.exec(select(Setting)).all():
        if _is_secret_key(row.key):
            overrides[f"{row.key}_set"] = bool(row.value)
        else:
            overrides[row.key] = row.value
    effective = {
        key: effective_setting(session, settings, key)
        for key in sorted(OVERRIDABLE)
        if not _is_secret_key(key)
    }
    effective["llm_api_key_set"] = bool(effective_setting(session, settings, "llm_api_key"))
    return {"overrides": overrides, "effective": effective}


@router.put("/settings")
def put_settings(
    body: SettingsUpdate, request: Request, session: Session = Depends(get_session)
):
    unknown = sorted(k for k in body.values if k not in OVERRIDABLE)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown setting key(s): {', '.join(unknown)}",
        )
    for key, value in body.values.items():
        if key == "style_loras" and value:
            _validate_style_loras(value)
        if key == "engine_loras" and value:
            _validate_engine_loras(value)
        if key == "engine_models" and value:
            _validate_engine_models(value)
        row = session.get(Setting, key)
        if value is None or value == "":
            if row is not None:
                session.delete(row)
            continue
        if row is None:
            row = Setting(key=key, value=value)
        else:
            row.value = value
        session.add(row)
    session.commit()
    if "comfyui_url" in body.values:
        # model/node availability is cached per engine URL for 60s — flush so
        # the very next /api/workflows call asks the *new* engine, not the old
        clear_object_info_cache()
    return get_settings(request, session)
