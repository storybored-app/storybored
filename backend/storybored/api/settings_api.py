"""Runtime settings: DB-backed overrides on top of env config.

A `setting` row's value wins over the corresponding env var when set — used
for LLM config and workflow defaults that users edit in the Settings screen.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from storybored.config import Settings
from storybored.db import get_session
from storybored.models import Setting
from storybored.schemas import SettingsUpdate

router = APIRouter(prefix="/api", tags=["settings"])

#: env-backed keys that may be overridden at runtime via the setting table
OVERRIDABLE = {
    "llm_base_url",
    "llm_api_key",
    "llm_model",
    "comfyui_url",
    "lora_factory_dir",
    "default_image_workflow",
    "default_video_workflow",
}


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
    return get_settings(request, session)
