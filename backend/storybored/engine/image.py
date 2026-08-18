# OWNED-BY: engine-agent
"""image_gen job handler: shot → n takes → ComfyUI → PNG + thumbnail on disk.

Payload: {"shot_id", "workflow_id", "n_takes", "params"}. For each take a
fresh random seed is drawn unless the caller pinned "seed" in params. Take
rows are created pending, flipped to done/failed as results land; the shot
transitions queued→generated on the first finished take. Every state change
publishes an SSE event ("take" / "shot").
"""

import asyncio
import json
import random
from pathlib import Path

from fastapi.encoders import jsonable_encoder
from PIL import Image
from sqlmodel import select

from storybored.api.settings_api import effective_setting
from storybored.api.shots import refresh_shot_characters
from storybored.engine import registry
from storybored.engine.comfy_client import ComfyCancelled, ComfyClient, ComfyError
from storybored.engine.graph import (
    added_engine_loras,
    apply_engine_lora_overrides,
    apply_model_overrides,
    apply_parameters,
    inject_characters,
    inject_style_loras,
    parse_engine_loras,
    parse_engine_models,
    parse_mentions,
    parse_style_loras,
    set_filename_prefix,
    substitute_mentions,
)
from storybored.jobs.registry import register
from storybored.jobs.runner import JobCancelled
from storybored.models import Character, Project, Scene, Shot, Take

THUMB_PX = 384
SEED_MAX = 2**32

#: default prompt for generated character portraits: identity front and center,
#: framed for the square card, wardrobe stated (LoRAs trained on unclothed data
#: default to nudity when a prompt omits clothing).
CHARACTER_PORTRAIT_PROMPT = (
    "portrait photograph of @{handle} wearing a plain dark crew-neck t-shirt, "
    "the t-shirt collar and sleeves clearly visible on the shoulders, head and "
    "shoulders framing, centered and facing the camera with a relaxed natural "
    "expression, plain softly lit studio backdrop, soft even light, sharp focus "
    "on the face, photorealistic"
)
PORTRAIT_SIZE = 1024


def make_thumbnail(src: Path, dest: Path, size: int = THUMB_PX) -> None:
    with Image.open(src) as img:
        img = img.convert("RGB")
        img.thumbnail((size, size))
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, format="PNG")


def _maybe_set_character_thumbnails(ctx, character_ids: list[int], thumb_rel: str) -> None:
    """First finished still featuring a character becomes its thumbnail (only
    when it has none yet — a deliberate upload or earlier take always wins)."""
    if not character_ids or not thumb_rel:
        return
    with ctx.session_factory() as session:
        for cid in character_ids:
            char = session.get(Character, cid)
            if char is None or char.thumbnail_path:
                continue
            char.thumbnail_path = thumb_rel
            session.add(char)
            session.commit()
            session.refresh(char)
            ctx.publish("character", jsonable_encoder(char))


def _first_image(entry: dict, output_node: str | None) -> dict:
    """The first image ref from the history outputs, preferring output_node."""
    outputs = entry.get("outputs") or {}
    candidates = []
    if output_node and output_node in outputs:
        candidates.append(outputs[output_node])
    candidates.extend(v for k, v in outputs.items() if k != output_node)
    for out in candidates:
        images = out.get("images") or []
        if images:
            return images[0]
    raise ComfyError("no image outputs found in ComfyUI history")


def _update_take(ctx, take_id: int, **fields) -> None:
    with ctx.session_factory() as session:
        take = session.get(Take, take_id)
        if take is None:
            return
        for key, value in fields.items():
            setattr(take, key, value)
        session.add(take)
        session.commit()
    ctx.publish("take", jsonable_encoder(take))


def _mark_shot_generated(ctx, shot_id: int) -> None:
    with ctx.session_factory() as session:
        shot = session.get(Shot, shot_id)
        if shot is None or shot.status not in ("draft", "queued"):
            return
        shot.status = "generated"
        session.add(shot)
        session.commit()
    ctx.publish("shot", jsonable_encoder(shot))


@register("image_gen")
async def image_gen(job, ctx):
    payload = json.loads(job.payload_json or "{}")
    shot_id = payload.get("shot_id")
    workflow_id = payload.get("workflow_id") or ""
    n_takes = max(1, int(payload.get("n_takes") or 1))
    user_params = dict(payload.get("params") or {})
    settings = ctx.settings

    with ctx.session_factory() as session:
        shot = session.get(Shot, shot_id)
        if shot is None:
            raise RuntimeError(f"shot {shot_id} not found")
        scene = session.get(Scene, shot.scene_id)
        if scene is None:
            # never fall back to a fake project bucket (media/0) that no
            # cleanup path owns — a shot without its scene is a dead shot
            raise RuntimeError(f"shot {shot_id} has no parent scene — was it deleted?")
        project_id = scene.project_id
        project = session.get(Project, project_id)
        scene_look = (scene.look or "").strip()
        continuity = bool(project and project.continuity_enabled) and bool(scene_look)
        refresh_shot_characters(session, shot)
        session.commit()
        handles = parse_mentions(shot.description or "")
        rows = (
            session.exec(
                select(Character).where(Character.handle.in_(handles))  # type: ignore[attr-defined]
            ).all()
            if handles
            else []
        )
        by_handle = {c.handle: c for c in rows}
        comfy_url = effective_setting(session, settings, "comfyui_url")
        style_loras = parse_style_loras(effective_setting(session, settings, "style_loras"))
        engine_loras = parse_engine_loras(
            effective_setting(session, settings, "engine_loras")
        ).get(workflow_id, [])
        engine_models = parse_engine_models(
            effective_setting(session, settings, "engine_models")
        ).get(workflow_id, {})

    pack = registry.get_pack(settings, workflow_id)
    if pack is None:
        raise RuntimeError(f"unknown workflow '{workflow_id}'")
    manifest = pack.manifest
    base_graph = pack.load_graph()

    prompt_text = str(user_params.get("prompt") or shot.description or "")
    # Continuity mode: append the scene's look so every shot in the scene
    # renders in the same visual environment. Deterministic (no LLM) and
    # skipped when the prompt already carries the look (e.g. story-vibes
    # boards that baked the environment into each description).
    if continuity and scene_look.lower() not in prompt_text.lower():
        prompt_text = f"{prompt_text.rstrip().rstrip('.')}. Scene environment: {scene_look}"
    prompt_text = substitute_mentions(prompt_text, by_handle)
    characters = [by_handle[h] for h in handles if h in by_handle and by_handle[h].lora_name]

    client = ComfyClient(comfy_url)
    seed_pinned = user_params.get("seed") is not None
    done_ids: list[int] = []
    failed = 0
    last_error = ""

    for i in range(n_takes):
        ctx.raise_if_cancelled()
        seed = int(user_params["seed"]) if seed_pinned else random.randrange(SEED_MAX)
        params = dict(user_params)
        params["prompt"] = prompt_text
        params["seed"] = seed

        take = Take(
            shot_id=shot_id,
            kind="image",
            status="pending",
            workflow_id=pack.id,
            params_json=json.dumps(params),
            seed=seed,
        )
        with ctx.session_factory() as session:
            session.add(take)
            session.commit()
            session.refresh(take)
        ctx.publish("take", jsonable_encoder(take))
        label = f"take {i + 1}/{n_takes}"
        prompt_id: str | None = None
        try:
            graph = apply_parameters(base_graph, manifest, params)
            apply_model_overrides(graph, manifest, engine_models)
            apply_engine_lora_overrides(graph, engine_loras)
            injection = manifest.get("character_injection")
            # Splice order characters → styles → engine additions: each later
            # splice lands closer to the base stack, so the final chain is
            # base → engine additions → styles → characters (identity last).
            inject_characters(graph, injection, characters)
            inject_style_loras(graph, injection, style_loras)
            inject_style_loras(
                graph, injection, added_engine_loras(engine_loras), id_prefix="engine_lora_"
            )
            set_filename_prefix(graph, f"storybored/take_{take.id}")

            ctx.update_progress(progress=i / n_takes, detail=f"{label}: submitting")
            prompt_id = await client.submit(graph)

            def on_status(pos, label=label):
                if pos is None or pos == 0:
                    ctx.update_progress(detail=f"{label}: rendering")
                else:
                    ctx.update_progress(detail=f"{label}: engine queue position {pos}")

            entry = await client.wait_for(
                prompt_id, on_status=on_status, should_cancel=ctx.cancelled
            )
            image_ref = _first_image(entry, manifest.get("output_node"))

            dest_dir = settings.media_path / str(project_id) / str(shot_id)
            dest = dest_dir / f"take_{take.id}.png"
            await client.download(
                image_ref.get("filename", ""),
                image_ref.get("subfolder", ""),
                image_ref.get("type", "output"),
                dest,
            )
            thumb = dest_dir / f"take_{take.id}_thumb.png"
            make_thumbnail(dest, thumb)

            _update_take(
                ctx,
                take.id,
                status="done",
                file_path=str(dest.relative_to(settings.data_path)),
                thumb_path=str(thumb.relative_to(settings.data_path)),
            )
            done_ids.append(take.id)
            _mark_shot_generated(ctx, shot_id)
            _maybe_set_character_thumbnails(
                ctx,
                [c.id for c in rows],
                str(thumb.relative_to(settings.data_path)),
            )
        except (JobCancelled, ComfyCancelled, asyncio.CancelledError) as exc:
            _update_take(ctx, take.id, status="failed", error="cancelled")
            if prompt_id is not None:
                try:
                    await client.cancel(prompt_id)
                except BaseException:  # noqa: BLE001 - best effort during cancellation
                    pass
            raise JobCancelled(str(exc)) from exc
        except ComfyError as exc:
            failed += 1
            last_error = str(exc)
            _update_take(ctx, take.id, status="failed", error=last_error)
        except Exception as exc:  # noqa: BLE001 - one bad take must not sink the rest
            failed += 1
            last_error = f"{type(exc).__name__}: {exc}"
            _update_take(ctx, take.id, status="failed", error=last_error)
        ctx.update_progress(progress=(i + 1) / n_takes)

    if not done_ids:
        raise RuntimeError(f"all {n_takes} take(s) failed: {last_error}")
    return {"take_ids": done_ids, "failed": failed}


@register("character_thumb")
async def character_thumb(job, ctx):
    """Render a portrait of one character and set it as their thumbnail.

    Payload: {"character_id", "workflow_id", "prompt"?}. One square render
    through the same LoRA pipeline as shots (engine overrides, style LoRAs,
    the character's own LoRA), saved under media/characters/{id}/.
    """
    payload = json.loads(job.payload_json or "{}")
    character_id = payload.get("character_id")
    workflow_id = payload.get("workflow_id") or ""
    settings = ctx.settings

    with ctx.session_factory() as session:
        char = session.get(Character, character_id)
        if char is None:
            raise RuntimeError(f"character {character_id} not found")
        comfy_url = effective_setting(session, settings, "comfyui_url")
        style_loras = parse_style_loras(effective_setting(session, settings, "style_loras"))
        engine_loras = parse_engine_loras(
            effective_setting(session, settings, "engine_loras")
        ).get(workflow_id, [])
        engine_models = parse_engine_models(
            effective_setting(session, settings, "engine_models")
        ).get(workflow_id, {})

    pack = registry.get_pack(settings, workflow_id)
    if pack is None:
        raise RuntimeError(f"unknown workflow '{workflow_id}'")
    manifest = pack.manifest

    prompt_text = str(payload.get("prompt") or "").strip() or CHARACTER_PORTRAIT_PROMPT.format(
        handle=char.handle
    )
    prompt_text = substitute_mentions(prompt_text, {char.handle: char})
    params = {
        "prompt": prompt_text,
        "seed": random.randrange(SEED_MAX),
        "width": PORTRAIT_SIZE,
        "height": PORTRAIT_SIZE,
    }

    graph = apply_parameters(pack.load_graph(), manifest, params)
    apply_model_overrides(graph, manifest, engine_models)
    apply_engine_lora_overrides(graph, engine_loras)
    injection = manifest.get("character_injection")
    inject_characters(graph, injection, [char] if char.lora_name else [])
    inject_style_loras(graph, injection, style_loras)
    inject_style_loras(
        graph, injection, added_engine_loras(engine_loras), id_prefix="engine_lora_"
    )
    set_filename_prefix(graph, f"storybored/charthumb_{character_id}_{job.id}")

    client = ComfyClient(comfy_url)
    ctx.update_progress(progress=0.0, detail="portrait: submitting")
    prompt_id = await client.submit(graph)
    try:
        def on_status(pos):
            if pos is None or pos == 0:
                ctx.update_progress(detail="portrait: rendering")
            else:
                ctx.update_progress(detail=f"portrait: engine queue position {pos}")

        entry = await client.wait_for(
            prompt_id, on_status=on_status, should_cancel=ctx.cancelled
        )
    except (JobCancelled, ComfyCancelled, asyncio.CancelledError) as exc:
        try:
            await client.cancel(prompt_id)
        except BaseException:  # noqa: BLE001 - best effort during cancellation
            pass
        raise JobCancelled(str(exc)) from exc

    image_ref = _first_image(entry, manifest.get("output_node"))
    dest_dir = settings.media_path / "characters" / str(character_id)
    dest = dest_dir / f"portrait_{job.id}.png"
    await client.download(
        image_ref.get("filename", ""),
        image_ref.get("subfolder", ""),
        image_ref.get("type", "output"),
        dest,
    )
    thumb = dest_dir / f"portrait_{job.id}_thumb.png"
    make_thumbnail(dest, thumb)

    thumb_rel = str(thumb.relative_to(settings.data_path))
    with ctx.session_factory() as session:
        row = session.get(Character, character_id)
        if row is not None:
            row.thumbnail_path = thumb_rel
            session.add(row)
            session.commit()
            session.refresh(row)
            ctx.publish("character", jsonable_encoder(row))
    return {
        "thumbnail_path": thumb_rel,
        "file_path": str(dest.relative_to(settings.data_path)),
    }
