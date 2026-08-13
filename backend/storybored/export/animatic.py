# OWNED-BY: export-agent
"""animatic job handler — stitch the board into a single MP4.

Per docs/CONTRACT.md:
- ffmpeg = imageio-ffmpeg's bundled static binary (never assume system ffmpeg).
- Board order (scene.idx, then shot.idx). Per shot: video take if present,
  else picked image take, else SKIP (skips are collected into result_json).
- Optional payload keys: `scene_id` limits the export to one scene;
  `title_cards` inserts a 2s Pillow-rendered card (title + slugline) before
  each scene's shots.
- Normalize every segment: scale+pad to the project resolution (16:9 → 1920x1080),
  24 fps CFR, yuv420p, 48 kHz stereo AAC audio.
- Respect shot.duration_s: longer clips are trimmed, shorter clips freeze their
  last frame to pad; stills hold for duration_s with silent audio. Clip audio
  is preserved (silence-padded when it runs short).
- Segments are encoded identically then concatenated with the concat demuxer
  (stream copy) → DATA_DIR/exports/{project_id}/animatic_{jobid}.mp4.
"""

import asyncio
import json
import re
import shutil
import tempfile
from pathlib import Path

import imageio_ffmpeg
from sqlmodel import select

from storybored.jobs.registry import register
from storybored.models import Project, Scene, Shot, Take

FPS = 24
AUDIO_RATE = 48000
TITLE_CARD_S = 2.0

#: candidate title-card fonts, first hit wins (Pillow falls back to its default)
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]

#: project aspect_ratio → output resolution (fallback 16:9)
RESOLUTIONS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
    "2.39:1": (1920, 804),
}

DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?:\s*Audio:")


def ffmpeg_exe() -> str:
    """Resolved path of the bundled ffmpeg binary (also reported by /api/health)."""
    return imageio_ffmpeg.get_ffmpeg_exe()


async def _run_ffmpeg(args: list[str]) -> str:
    """Run the bundled ffmpeg; raise with the stderr tail on failure."""
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_exe(),
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    text = (err or b"").decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}): …{text[-800:]}")
    return text


async def _probe(path: Path) -> tuple[float | None, bool]:
    """(duration_s, has_audio) parsed from `ffmpeg -i` output (no ffprobe in the
    imageio-ffmpeg bundle)."""
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_exe(),
        "-hide_banner",
        "-i",
        str(path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    text = (err or b"").decode(errors="replace")
    duration = None
    m = DURATION_RE.search(text)
    if m:
        h, mi, s = m.groups()
        duration = int(h) * 3600 + int(mi) * 60 + float(s)
    return duration, bool(AUDIO_RE.search(text))


def _vf(width: int, height: int) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={FPS},format=yuv420p"
    )


ENCODE_ARGS = [
    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
    "-c:a", "aac", "-b:a", "128k", "-ar", str(AUDIO_RATE), "-ac", "2",
    "-movflags", "+faststart",
]  # fmt: skip


async def _encode_still_segment(
    src: Path, dest: Path, duration_s: float, width: int, height: int
) -> None:
    dur = f"{duration_s:.3f}"
    args = [
        "-y",
        "-loop", "1", "-framerate", str(FPS), "-t", dur, "-i", str(src),
        "-f", "lavfi", "-t", dur,
        "-i", f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE}",
        "-filter_complex", f"[0:v]{_vf(width, height)}[v]",
        "-map", "[v]", "-map", "1:a",
        *ENCODE_ARGS,
        "-t", dur, str(dest),
    ]  # fmt: skip
    await _run_ffmpeg(args)


async def _encode_clip_segment(
    src: Path, dest: Path, duration_s: float, width: int, height: int
) -> None:
    clip_dur, has_audio = await _probe(src)
    # freeze-frame pad when the clip runs short (tpad clones the last frame);
    # `-t` below trims when it runs long. Pad generously if probing failed.
    pad_s = max(0.0, duration_s - clip_dur + 1.0) if clip_dur is not None else duration_s
    dur = f"{duration_s:.3f}"
    vchain = f"[0:v]{_vf(width, height)},tpad=stop_mode=clone:stop_duration={pad_s:.3f}[v]"
    if has_audio:
        filter_complex = f"{vchain};[0:a]aresample={AUDIO_RATE},apad[a]"
        args = [
            "-y", "-i", str(src),
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
            *ENCODE_ARGS,
            "-t", dur, str(dest),
        ]  # fmt: skip
    else:
        args = [
            "-y", "-i", str(src),
            "-f", "lavfi", "-t", dur,
            "-i", f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE}",
            "-filter_complex", vchain,
            "-map", "[v]", "-map", "1:a",
            *ENCODE_ARGS,
            "-t", dur, str(dest),
        ]  # fmt: skip
    await _run_ffmpeg(args)


def _load_font(size: int):
    from PIL import ImageFont

    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _render_title_card(
    dest: Path, title: str, slugline: str, width: int, height: int
) -> None:
    """Dark scene title card: big centered title, slugline in muted gray below."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), "#101014")
    draw = ImageDraw.Draw(img)
    title_font = _load_font(max(28, height // 12))
    slug_font = _load_font(max(18, height // 24))

    def centered(text, font, y, fill):
        left, _top, right, _bottom = draw.textbbox((0, 0), text, font=font)
        draw.text(((width - (right - left)) / 2, y), text, font=font, fill=fill)

    y_title = height / 2 - height // 12
    centered(title or "Untitled scene", title_font, y_title, "#e8e8ee")
    if slugline:
        centered(slugline.upper(), slug_font, y_title + height // 8, "#8b8b98")
    img.save(dest)


def _concat_escape(seg: Path) -> str:
    """Escape a path for a single-quoted ffmpeg concat list line ('→'\\'')."""
    return seg.as_posix().replace("'", r"'\''")


async def _concat_segments(segments: list[Path], dest: Path, workdir: Path) -> None:
    list_file = workdir / "concat.txt"
    # ffmpeg's concat demuxer treats a bare ' as end-of-token, so an apostrophe
    # in the path (e.g. a home dir like /Users/O'Brien/...) must be escaped as
    # '\'' inside the quoted filename or the join step fails.
    lines = "".join(f"file '{_concat_escape(seg)}'\n" for seg in segments)
    list_file.write_text(lines)
    await _run_ffmpeg(
        ["-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(dest)]
    )


@register("animatic")
async def animatic(job, ctx):
    payload = json.loads(job.payload_json or "{}")
    project_id = payload["project_id"]
    scene_id = payload.get("scene_id")
    title_cards = bool(payload.get("title_cards"))
    settings = ctx.settings

    # -- collect board-order sources --
    # (shot_id, kind, src, duration_s); kind "card" carries (title, slugline) in src
    entries: list[tuple[int | None, str, object, float]] = []
    skipped: list[dict] = []
    scene_label = ""
    with ctx.session_factory() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise RuntimeError(f"project {project_id} not found")
        width, height = RESOLUTIONS.get(project.aspect_ratio, RESOLUTIONS["16:9"])
        scene_q = select(Scene).where(Scene.project_id == project_id).order_by(Scene.idx)
        if scene_id is not None:
            scene_q = scene_q.where(Scene.id == scene_id)
        scenes = session.exec(scene_q).all()
        if scene_id is not None and not scenes:
            raise RuntimeError(f"scene {scene_id} not found in project {project_id}")
        if scene_id is not None:
            scene_label = f"s{scenes[0].idx + 1}_"
        for scene in scenes:
            if title_cards:
                entries.append((None, "card", (scene.title, scene.slugline), TITLE_CARD_S))
            shots = session.exec(
                select(Shot).where(Shot.scene_id == scene.id).order_by(Shot.idx)
            ).all()
            for shot in shots:
                src: Path | None = None
                kind = ""
                for take_id, take_kind in (
                    (shot.video_take_id, "video"),
                    (shot.picked_take_id, "still"),
                ):
                    if take_id is None:
                        continue
                    take = session.get(Take, take_id)
                    if take is None or take.status != "done" or not take.file_path:
                        continue
                    candidate = (settings.data_path / take.file_path).resolve()
                    if candidate.is_file():
                        src, kind = candidate, take_kind
                        break
                if src is None:
                    skipped.append(
                        {
                            "shot_id": shot.id,
                            "scene_id": scene.id,
                            "reason": "no video take or picked still",
                        }
                    )
                    continue
                # only None means "unset" → default 4s; an explicit 0 (or any
                # small value) is honored, clamped to a sane 0.1s floor rather
                # than silently ballooning into a 4s hold via `or 4.0`.
                raw_duration = shot.duration_s
                duration_s = 4.0 if raw_duration is None else float(raw_duration)
                entries.append((shot.id, kind, src, max(0.1, duration_s)))

    if not any(kind != "card" for _, kind, _, _ in entries):
        raise RuntimeError("no shots with a video take or picked still — nothing to export")

    export_dir = settings.exports_path / str(project_id)
    export_dir.mkdir(parents=True, exist_ok=True)
    out_path = export_dir / f"animatic_{scene_label}{job.id}.mp4"

    workdir = Path(tempfile.mkdtemp(prefix=f"animatic_{job.id}_", dir=settings.data_path))
    try:
        segments: list[Path] = []
        total = len(entries) + 1
        for i, (shot_id, kind, src, duration_s) in enumerate(entries):
            ctx.raise_if_cancelled()
            ctx.update_progress(i / total, f"rendering segment {i + 1} of {len(entries)}")
            seg = workdir / f"seg_{i:04d}.mp4"
            if kind == "card":
                title, slugline = src
                card_png = workdir / f"card_{i:04d}.png"
                _render_title_card(card_png, title, slugline, width, height)
                await _encode_still_segment(card_png, seg, duration_s, width, height)
            elif kind == "video":
                await _encode_clip_segment(src, seg, duration_s, width, height)
            else:
                await _encode_still_segment(src, seg, duration_s, width, height)
            segments.append(seg)

        ctx.raise_if_cancelled()
        ctx.update_progress(len(entries) / total, "joining shots")
        await _concat_segments(segments, out_path, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    duration_total = sum(d for _, _, _, d in entries)
    shot_entries = [e for e in entries if e[1] != "card"]
    ctx.update_progress(1.0, "animatic ready")
    return {
        "file_path": str(out_path.relative_to(settings.data_path)),
        "duration_s": round(duration_total, 3),
        "shots": len(shot_entries),
        "shot_ids": [e[0] for e in shot_entries],
        "scene_id": scene_id,
        "title_cards": title_cards,
        "skipped": skipped,
    }
