# OWNED-BY: engine-agent
"""Characters: CRUD, available LoRA list, LoRA import, thumbnail upload.

Handles are stored lowercase without the @; they are what @mentions in shot
descriptions resolve against. `lora_name` is the engine dropdown name
(including any subdir), validated against /object_info when importing by name.
"""

import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlmodel import Session, select

from storybored.api.preflight import require_pack_available, resolve_pack
from storybored.db import get_session
from storybored.engine import families, registry
from storybored.engine.comfy_client import ComfyClient, ComfyError
from storybored.llm.client import LLMError, LLMNotConfiguredError, get_llm_config
from storybored.llm.guides import resolve_prompt_guide
from storybored.llm.portrait import build_portrait_notes, generate_portrait_prompt
from storybored.models import Character, ShotCharacter
from storybored.settings_store import effective_setting

router = APIRouter(prefix="/api", tags=["characters"])

HANDLE_RE = re.compile(r"^[a-z0-9_-]+$")
STATUSES = {"ready", "dataset", "training", "trained"}
THUMB_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


class CharacterCreate(BaseModel):
    name: str
    handle: str
    trigger: str = ""
    class_word: str = "person"
    lora_name: str = ""
    lora_strength: float = 1.0
    #: model family the LoRA belongs to (e.g. "krea2", "z-image"); None/"" =
    #: unspecified — the character then works with any engine, unchecked
    lora_family: str | None = None
    bio: str = ""
    notes: str = ""
    status: str = "ready"


class CharacterUpdate(BaseModel):
    name: str | None = None
    handle: str | None = None
    trigger: str | None = None
    class_word: str | None = None
    lora_name: str | None = None
    lora_strength: float | None = None
    lora_family: str | None = None  # "" clears back to unspecified
    bio: str | None = None
    notes: str | None = None
    status: str | None = None


def normalize_family(raw: str | None) -> str | None:
    """"" / whitespace → None (unspecified); anything else is kept verbatim —
    family ids are an open vocabulary matched by exact string."""
    value = (raw or "").strip()
    return value or None


def normalize_handle(raw: str) -> str:
    handle = (raw or "").strip().lstrip("@").lower()
    if not HANDLE_RE.match(handle):
        raise HTTPException(
            status_code=422,
            detail="handle must be letters/digits/underscore/dash (e.g. 'ava_2')",
        )
    return handle


def _get_character_or_404(session: Session, character_id: int) -> Character:
    char = session.get(Character, character_id)
    if char is None:
        raise HTTPException(status_code=404, detail="character not found")
    return char


def _publish(request: Request, char: Character, **extra) -> None:
    data = jsonable_encoder(char)
    data.update(extra)
    request.app.state.bus.publish("character", data)


def _handle_taken(session: Session, handle: str, exclude_id: int | None = None) -> bool:
    row = session.exec(select(Character).where(Character.handle == handle)).first()
    return row is not None and row.id != exclude_id


# -- LoRA discovery / import (declared before /{character_id} routes) ----------


@router.get("/characters/available-loras")
async def available_loras(request: Request, session: Session = Depends(get_session)):
    settings = request.app.state.settings
    comfy_url = effective_setting(session, settings, "comfyui_url")
    try:
        return await ComfyClient(comfy_url).model_enum("LoraLoader", "lora_name")
    except ComfyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/characters/import-lora")
async def import_lora(request: Request, session: Session = Depends(get_session)):
    """Multipart: copy an uploaded .safetensors into COMFY_LORAS_DIR.
    JSON {"lora_name": ...}: reference an entry already in the engine dropdown."""
    settings = request.app.state.settings
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise HTTPException(
                status_code=400, detail="multipart field 'file' with a .safetensors upload required"
            )
        filename = Path(upload.filename or "").name
        if not filename.endswith(".safetensors"):
            raise HTTPException(
                status_code=400, detail="only .safetensors files can be imported"
            )
        loras_dir = effective_setting(session, settings, "comfy_loras_dir")
        if not loras_dir:
            raise HTTPException(
                status_code=400,
                detail="No LoRA folder is configured — set it in Settings (or "
                "COMFY_LORAS_DIR in .env) so imported LoRA files land where the "
                "engine can load them, or import by name with JSON "
                "{\"lora_name\": ...} instead",
            )
        dest_dir = Path(loras_dir).expanduser()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        with dest.open("wb") as fh:
            shutil.copyfileobj(upload.file, fh)
        return {"lora_name": filename, "imported": True}

    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="send a multipart .safetensors file or JSON {\"lora_name\": ...}",
        ) from None
    lora_name = (body or {}).get("lora_name", "")
    if not lora_name:
        raise HTTPException(status_code=400, detail="lora_name required")
    comfy_url = effective_setting(session, settings, "comfyui_url")
    try:
        enum = await ComfyClient(comfy_url).model_enum("LoraLoader", "lora_name")
    except ComfyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if lora_name not in enum:
        raise HTTPException(
            status_code=400,
            detail=f"'{lora_name}' is not in the engine's LoRA list",
        )
    return {"lora_name": lora_name, "imported": False}


# -- CRUD ----------------------------------------------------------------------


@router.get("/characters")
def list_characters(session: Session = Depends(get_session)):
    chars = session.exec(
        select(Character).order_by(Character.name)  # type: ignore[arg-type]
    ).all()
    return jsonable_encoder(chars)


@router.post("/characters", status_code=201)
def create_character(
    body: CharacterCreate, request: Request, session: Session = Depends(get_session)
):
    handle = normalize_handle(body.handle)
    if body.status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(STATUSES)}")
    if _handle_taken(session, handle):
        raise HTTPException(status_code=409, detail=f"handle '@{handle}' is already taken")
    char = Character(
        **{
            **body.model_dump(),
            "handle": handle,
            "lora_family": normalize_family(body.lora_family),
        }
    )
    session.add(char)
    session.commit()
    session.refresh(char)
    _publish(request, char)
    return jsonable_encoder(char)


@router.patch("/characters/{character_id}")
def update_character(
    character_id: int,
    body: CharacterUpdate,
    request: Request,
    session: Session = Depends(get_session),
):
    char = _get_character_or_404(session, character_id)
    changes = body.model_dump(exclude_unset=True)
    if "handle" in changes:
        handle = normalize_handle(changes["handle"])
        if _handle_taken(session, handle, exclude_id=character_id):
            raise HTTPException(status_code=409, detail=f"handle '@{handle}' is already taken")
        changes["handle"] = handle
    if "status" in changes and changes["status"] not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(STATUSES)}")
    if "lora_family" in changes:
        changes["lora_family"] = normalize_family(changes["lora_family"])
    for key, value in changes.items():
        setattr(char, key, value)
    session.add(char)
    session.commit()
    session.refresh(char)
    _publish(request, char)
    return jsonable_encoder(char)


@router.delete("/characters/{character_id}", status_code=204)
def delete_character(
    character_id: int, request: Request, session: Session = Depends(get_session)
):
    char = _get_character_or_404(session, character_id)
    for link in session.exec(
        select(ShotCharacter).where(ShotCharacter.character_id == character_id)
    ):
        session.delete(link)
    # no ORM relationships are mapped, so flush to guarantee the link rows go
    # before the character row (FKs are enforced)
    session.flush()
    _publish(request, char, deleted=True)
    session.delete(char)
    session.commit()
    return None


@router.post("/characters/{character_id}/thumbnail")
def upload_thumbnail(
    character_id: int,
    request: Request,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    char = _get_character_or_404(session, character_id)
    settings = request.app.state.settings
    ext = Path(file.filename or "").suffix.lower() or ".png"
    if ext not in THUMB_EXTS:
        raise HTTPException(
            status_code=400, detail=f"thumbnail must be one of {sorted(THUMB_EXTS)}"
        )
    dest_dir = settings.media_path / "characters"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{character_id}{ext}"
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    char.thumbnail_path = str(dest.relative_to(settings.data_path))
    session.add(char)
    session.commit()
    session.refresh(char)
    _publish(request, char)
    return jsonable_encoder(char)


class ThumbnailGenRequest(BaseModel):
    workflow_id: str | None = None
    prompt: str | None = None
    #: unsaved editor state wins over the stored bio when provided ("" = render
    #: the stock portrait even though a bio is saved)
    bio: str | None = None


@router.post("/characters/{character_id}/generate-thumbnail")
async def generate_thumbnail(
    character_id: int,
    request: Request,
    body: ThumbnailGenRequest | None = None,
    session: Session = Depends(get_session),
):
    """Enqueue a character_thumb job: render a portrait of this character with
    their LoRA and set it as the card thumbnail. Body optional {workflow_id, prompt}."""
    body = body or ThumbnailGenRequest()
    char = _get_character_or_404(session, character_id)
    if not char.lora_name:
        raise HTTPException(
            status_code=400,
            detail="this character has no LoRA yet — train or import one first",
        )
    settings = request.app.state.settings
    packs = registry.load_packs(settings)
    pack = resolve_pack(session, settings, packs, body.workflow_id, kind="image")
    workflow_id = pack.id
    pack_family = families.pack_family(pack.manifest)
    if pack_family and char.lora_family and char.lora_family != pack_family:
        raise HTTPException(
            status_code=409,
            detail=(
                f"@{char.handle} was trained for {families.family_label(char.lora_family)}"
                f" — this engine renders with {families.family_label(pack_family)};"
                " pick a compatible engine"
            ),
        )
    await require_pack_available(session, settings, pack, "cannot render a portrait")

    # With a bio (and no explicit prompt), draft a personality-informed
    # portrait prompt via PromptSmith BEFORE enqueueing — the render job never
    # calls an LLM. LLM down/failing degrades to the stock studio portrait,
    # surfaced via bio_used so the UI can say so.
    prompt = (body.prompt or "").strip()
    bio = (body.bio if body.bio is not None else char.bio or "").strip()
    bio_used = False
    if not prompt and bio:
        try:
            config = get_llm_config(session, settings)
            guide = resolve_prompt_guide(session, settings, "image", workflow_id)
            notes = build_portrait_notes(char.name, char.handle, char.class_word, bio)
            prompt = generate_portrait_prompt(config, notes, char.handle, guide)
            bio_used = True
        except (LLMNotConfiguredError, LLMError):
            prompt = ""

    payload = {"character_id": character_id, "workflow_id": workflow_id}
    if prompt:
        payload["prompt"] = prompt
    job = request.app.state.runner.enqueue("character_thumb", payload)
    return {"job_id": job.id, "prompt": prompt or None, "bio_used": bio_used}
