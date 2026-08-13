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
    apply_parameters,
    inject_characters,
    parse_mentions,
    set_filename_prefix,
    substitute_mentions,
)
from storybored.jobs.registry import register
from storybored.jobs.runner import JobCancelled
from storybored.models import Character, Scene, Shot, Take

THUMB_PX = 384
SEED_MAX = 2**32


def make_thumbnail(src: Path, dest: Path, size: int = THUMB_PX) -> None:
    with Image.open(src) as img:
        img = img.convert("RGB")
        img.thumbnail((size, size))
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, format="PNG")


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
        project_id = scene.project_id if scene is not None else 0
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

    pack = registry.get_pack(settings, workflow_id)
    if pack is None:
        raise RuntimeError(f"unknown workflow '{workflow_id}'")
    manifest = pack.manifest
    base_graph = pack.load_graph()

    prompt_text = str(user_params.get("prompt") or shot.description or "")
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
            inject_characters(graph, manifest.get("character_injection"), characters)
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
