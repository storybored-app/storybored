# OWNED-BY: engine-agent
"""video_gen job handler: approved shot + picked still → image-to-video take.

Flow (docs/CONTRACT.md):
  1. Guard: shot must be `approved` with a finished picked image take.
  2. Upload the picked still to the engine (POST /upload/image via the client).
  3. Build the graph from a video workflow pack (default: minimax-h3-i2v):
     - `first_frame` param ← the uploaded image's server-side name
     - `prompt` param ← shot.motion_prompt (fallback shot.description) with
       `@handle` mentions replaced by "{trigger} {class_word}"
     - `length` param ← payload override or the manifest default
     - seed ← payload override or random; persisted on the take
     - SaveVideo filename_prefix ← `storybored/take_{take_id}`
  4. Submit + poll. Video is slow: the wait ceiling is never below 15 minutes.
  5. Download the MP4 to DATA_DIR/media/{project}/{shot}/take_{id}.mp4, grab a
     first-frame thumbnail via imageio, set take kind=video done and
     shot.video_take_id (SSE events for both).
"""

import asyncio
import json
import random
from pathlib import Path

from fastapi.encoders import jsonable_encoder
from sqlmodel import select

from storybored.api.settings_api import effective_setting
from storybored.engine import registry
from storybored.engine.comfy_client import ComfyCancelled, ComfyClient, ComfyError
from storybored.engine.graph import (
    apply_parameters,
    parse_mentions,
    set_filename_prefix,
    substitute_mentions,
)
from storybored.jobs.registry import register
from storybored.jobs.runner import JobCancelled
from storybored.models import Character, Project, Scene, Shot, Take

DEFAULT_WORKFLOW = "minimax-h3-i2v"
#: video generation is slow — never cap the wait below 15 minutes (contract)
VIDEO_TIMEOUT_S = 3600.0
THUMB_PX = 384
SEED_MAX = 2**32

VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".mkv")


def _make_client(comfy_url: str) -> ComfyClient:
    """Client factory — a seam so tests can substitute a fake."""
    return ComfyClient(comfy_url)


def _uploaded_name(upload_result) -> str:
    """ComfyUI /upload/image response → the name a LoadImage node expects."""
    if isinstance(upload_result, str):
        return upload_result
    name = upload_result.get("name", "")
    subfolder = upload_result.get("subfolder", "")
    return f"{subfolder}/{name}" if subfolder else name


def _pick_video_output(entry: dict, output_node: str | None) -> dict:
    """The video file ref from the history outputs, preferring output_node.

    ComfyUI's SaveVideo reports its files under the same keys history uses for
    other savers ("images"; some versions use "videos"/"gifs"), so accept all
    and prefer entries with a video suffix.
    """
    outputs = entry.get("outputs") or {}
    candidates = []
    if output_node and output_node in outputs:
        candidates.append(outputs[output_node])
    candidates.extend(v for k, v in outputs.items() if k != output_node)
    refs: list[dict] = []
    for out in candidates:
        if not isinstance(out, dict):
            continue
        for key in ("videos", "gifs", "images"):
            refs.extend(r for r in out.get(key) or [] if isinstance(r, dict))
    for ref in refs:
        if str(ref.get("filename", "")).lower().endswith(VIDEO_SUFFIXES):
            return ref
    if refs:
        return refs[0]
    raise ComfyError("no video outputs found in ComfyUI history")


def make_video_thumbnail(video: Path, dest: Path, size: int = THUMB_PX) -> None:
    """First frame of the clip → ~384px PNG (imageio's ffmpeg reader + Pillow)."""
    import imageio
    from PIL import Image

    reader = imageio.get_reader(str(video), format="ffmpeg")
    try:
        frame = reader.get_data(0)
    finally:
        reader.close()
    img = Image.fromarray(frame)
    img.thumbnail((size, size))
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")


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


@register("video_gen")
async def video_gen(job, ctx):
    payload = json.loads(job.payload_json or "{}")
    shot_id = payload.get("shot_id")
    workflow_id = payload.get("workflow_id") or DEFAULT_WORKFLOW
    user_params = dict(payload.get("params") or {})
    settings = ctx.settings

    # -- load shot + guards: approved, finished picked still ------------------
    with ctx.session_factory() as session:
        shot = session.get(Shot, shot_id)
        if shot is None:
            raise RuntimeError(f"shot {shot_id} not found")
        if shot.status != "approved":
            raise RuntimeError("shot must be approved before rendering video")
        if shot.picked_take_id is None:
            raise RuntimeError("shot has no picked still to animate")
        picked = session.get(Take, shot.picked_take_id)
        if picked is None or picked.kind != "image" or picked.status != "done":
            raise RuntimeError("picked take is not a finished still")
        if not picked.file_path:
            raise RuntimeError("picked take has no image file")
        still_path = (settings.data_path / picked.file_path).resolve()
        if not still_path.is_file():
            raise RuntimeError("picked still is missing on disk")
        scene = session.get(Scene, shot.scene_id)
        project = session.get(Project, scene.project_id) if scene is not None else None
        if project is None:
            raise RuntimeError("shot has no parent project")
        project_id = project.id

        prompt_source = shot.motion_prompt or shot.description or ""
        handles = parse_mentions(prompt_source)
        rows = (
            session.exec(
                select(Character).where(Character.handle.in_(handles))  # type: ignore[attr-defined]
            ).all()
            if handles
            else []
        )
        by_handle = {c.handle: c for c in rows}
        comfy_url = effective_setting(session, settings, "comfyui_url")

    # -- workflow pack ---------------------------------------------------------
    pack = registry.get_pack(settings, workflow_id)
    if pack is None:
        raise RuntimeError(f"unknown workflow '{workflow_id}'")
    manifest = pack.manifest
    if manifest.get("kind") != "video":
        raise RuntimeError(f"workflow '{workflow_id}' is not a video workflow")
    base_graph = pack.load_graph()

    seed = int(user_params["seed"]) if user_params.get("seed") is not None else (
        random.randrange(SEED_MAX)
    )
    params = dict(user_params)
    params["prompt"] = substitute_mentions(
        str(user_params.get("prompt") or prompt_source), by_handle
    )
    params["seed"] = seed

    # -- pending take row --------------------------------------------------------
    take = Take(
        shot_id=shot_id,
        kind="video",
        status="pending",
        workflow_id=pack.id,
        params_json=json.dumps(params),
        seed=seed,
    )
    with ctx.session_factory() as session:
        session.add(take)
        session.commit()
        session.refresh(take)
    take_id = take.id
    ctx.publish("take", jsonable_encoder(take))

    client = _make_client(comfy_url)
    prompt_id: str | None = None
    try:
        ctx.raise_if_cancelled()
        ctx.update_progress(progress=0.05, detail="uploading first frame")
        upload = await client.upload_image(
            still_path.read_bytes(), f"storybored_take_{take_id}_first_frame.png"
        )
        params["first_frame"] = _uploaded_name(upload)

        graph = apply_parameters(base_graph, manifest, params)
        set_filename_prefix(graph, f"storybored/take_{take_id}")

        ctx.raise_if_cancelled()
        ctx.update_progress(progress=0.1, detail="rendering video — this can take a few minutes")
        prompt_id = await client.submit(graph)

        def on_status(pos):
            if pos is None or pos == 0:
                ctx.update_progress(detail="rendering video — this can take a few minutes")
            else:
                ctx.update_progress(detail=f"engine queue position {pos}")

        entry = await asyncio.wait_for(
            client.wait_for(prompt_id, on_status=on_status, should_cancel=ctx.cancelled),
            timeout=VIDEO_TIMEOUT_S,
        )

        ctx.update_progress(progress=0.9, detail="downloading video")
        video_ref = _pick_video_output(entry, manifest.get("output_node"))
        dest_dir = settings.media_path / str(project_id) / str(shot_id)
        dest = dest_dir / f"take_{take_id}.mp4"
        await client.download(
            video_ref.get("filename", ""),
            video_ref.get("subfolder", ""),
            video_ref.get("type", "output"),
            dest,
        )
        if not dest.is_file() or dest.stat().st_size == 0:
            raise ComfyError("downloaded video is empty")
        thumb = dest_dir / f"take_{take_id}_thumb.png"
        make_video_thumbnail(dest, thumb)
    except (JobCancelled, ComfyCancelled, asyncio.CancelledError) as exc:
        _update_take(ctx, take_id, status="failed", error="cancelled")
        if prompt_id is not None:
            try:
                await client.cancel(prompt_id)
            except BaseException:  # noqa: BLE001 - best effort during cancellation
                pass
        raise JobCancelled(str(exc)) from exc
    except TimeoutError as exc:
        _update_take(ctx, take_id, status="failed", error="video render timed out")
        if prompt_id is not None:
            try:
                await client.cancel(prompt_id)
            except BaseException:  # noqa: BLE001 - best effort after timeout
                pass
        raise RuntimeError(
            f"video render timed out after {int(VIDEO_TIMEOUT_S)}s"
        ) from exc
    except Exception as exc:
        error = str(exc) if isinstance(exc, ComfyError) else f"{type(exc).__name__}: {exc}"
        _update_take(ctx, take_id, status="failed", error=error)
        raise

    # -- finish: take done, shot.video_take_id ----------------------------------
    _update_take(
        ctx,
        take_id,
        status="done",
        file_path=str(dest.relative_to(settings.data_path)),
        thumb_path=str(thumb.relative_to(settings.data_path)),
    )
    with ctx.session_factory() as session:
        shot = session.get(Shot, shot_id)
        if shot is not None:
            shot.video_take_id = take_id
            session.add(shot)
            session.commit()
            ctx.publish("shot", jsonable_encoder(shot))
    ctx.update_progress(progress=1.0, detail="video take ready")
    return {"take_id": take_id, "file_path": str(dest.relative_to(settings.data_path))}
