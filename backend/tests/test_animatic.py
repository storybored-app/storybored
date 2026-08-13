# OWNED-BY: export-agent
"""Animatic export e2e: real handler run over 2 tiny clips + 1 still, all
generated with imageio-ffmpeg's bundled binary (no ComfyUI, no system ffmpeg)."""

import json
import re
import subprocess
import time

import imageio_ffmpeg
import pytest
from PIL import Image
from sqlmodel import Session

from storybored.models import Project, Scene, Shot, Take

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _ffmpeg(args: list[str]) -> None:
    subprocess.run([FFMPEG, *args], check=True, capture_output=True)


def make_clip(path, duration=0.5, color="red", audio=True) -> None:
    """Tiny test clip: solid color, 320x240 (NOT the target res), optional tone."""
    args = ["-y", "-f", "lavfi", "-i", f"color=c={color}:s=320x240:r=24:d={duration}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    _ffmpeg(args + [str(path)])


def probe(path) -> tuple[float, str]:
    """(duration_s, raw `ffmpeg -i` stderr) using the bundled binary."""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True
    )
    m = DURATION_RE.search(proc.stderr)
    assert m, f"no duration in ffmpeg output: {proc.stderr[-500:]}"
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s), proc.stderr


def wait_for(client, job_id, statuses, timeout=120.0):
    deadline = time.monotonic() + timeout
    job = None
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} never reached {statuses}: {job}")


@pytest.fixture
def board(client, app, settings):
    """Project with 4 shots in board order:
    0: video take (with audio), clip 0.5s but duration_s=1.0 → freeze-pad
    1: video take (no audio), duration_s=0.5 → exact
    2: picked still, duration_s=1.0 → hold with silence
    3: nothing → skipped
    """
    media = settings.data_path / "media" / "seed"
    media.mkdir(parents=True, exist_ok=True)
    clip_a, clip_b = media / "clip_a.mp4", media / "clip_b.mp4"
    still = media / "still.png"
    make_clip(clip_a, duration=0.5, color="red", audio=True)
    make_clip(clip_b, duration=0.5, color="blue", audio=False)
    Image.new("RGB", (640, 360), (200, 160, 40)).save(still)

    with Session(app.state.engine, expire_on_commit=False) as session:
        project = Project(title="Animatic Test")
        session.add(project)
        session.flush()
        scene = Scene(project_id=project.id, idx=0, title="Scene 1")
        session.add(scene)
        session.flush()
        shots = [
            Shot(scene_id=scene.id, idx=0, description="clip padded", duration_s=1.0),
            Shot(scene_id=scene.id, idx=1, description="clip exact", duration_s=0.5),
            Shot(scene_id=scene.id, idx=2, description="still hold", duration_s=1.0),
            Shot(scene_id=scene.id, idx=3, description="empty", duration_s=1.0),
        ]
        session.add_all(shots)
        session.flush()

        def add_take(shot, kind, path):
            take = Take(
                shot_id=shot.id,
                kind=kind,
                status="done",
                file_path=str(path.relative_to(settings.data_path)),
            )
            session.add(take)
            session.flush()
            return take

        shots[0].video_take_id = add_take(shots[0], "video", clip_a).id
        shots[1].video_take_id = add_take(shots[1], "video", clip_b).id
        shots[2].picked_take_id = add_take(shots[2], "image", still).id
        shots[2].status = "generated"
        session.add_all(shots)
        session.commit()
        return {"project_id": project.id, "skipped_shot_id": shots[3].id}


def test_animatic_e2e(client, settings, board):
    project_id = board["project_id"]
    r = client.post(f"/api/projects/{project_id}/animatic")
    assert r.status_code == 200
    job = wait_for(client, r.json()["job_id"], {"done", "failed"})
    assert job["status"] == "done", job["error"]

    result = json.loads(job["result_json"])
    out = settings.data_path / result["file_path"]
    assert out.is_file() and out.stat().st_size > 0
    assert result["file_path"].startswith(f"exports/{project_id}/")
    assert f"animatic_{job['id']}.mp4" in result["file_path"]

    # exactly the empty shot was skipped, three shots included
    assert result["shots"] == 3
    assert [s["shot_id"] for s in result["skipped"]] == [board["skipped_shot_id"]]

    # duration ≈ sum of shot durations (1.0 + 0.5 + 1.0), fps + resolution normalized
    duration, info = probe(out)
    expected = 2.5
    assert abs(duration - expected) <= 0.3, f"duration {duration} vs {expected}\n{info[-500:]}"
    assert "1920x1080" in info
    assert re.search(r"\b24 fps\b", info)
    assert "yuv420p" in info
    # audio track present end-to-end (silence for stills / silent clips)
    assert re.search(r"Stream #\d+:\d+.*Audio: aac", info)

    # the export shows up in the project exports listing and is media-servable
    listing = client.get(f"/api/projects/{project_id}/exports").json()
    assert [e["file_path"] for e in listing] == [result["file_path"]]
    assert client.get(f"/api/media/{result['file_path']}").status_code == 200


def test_animatic_empty_project_fails_cleanly(client, app):
    with Session(app.state.engine, expire_on_commit=False) as session:
        project = Project(title="Empty")
        session.add(project)
        session.commit()
        pid = project.id
    r = client.post(f"/api/projects/{pid}/animatic")
    job = wait_for(client, r.json()["job_id"], {"done", "failed"})
    assert job["status"] == "failed"
    assert "nothing to export" in job["error"]
    assert client.get(f"/api/projects/{pid}/exports").json() == []


def test_animatic_unknown_project_404(client):
    assert client.post("/api/projects/424242/animatic").status_code == 404
    assert client.get("/api/projects/424242/exports").status_code == 404


def test_concat_escape_quotes_apostrophe():
    from pathlib import Path

    from storybored.export.animatic import _concat_escape

    # ' → '\'' inside the single-quoted concat line
    assert _concat_escape(Path("/tmp/o'brien/seg.mp4")) == "/tmp/o'\\''brien/seg.mp4"
    assert _concat_escape(Path("/tmp/plain/seg.mp4")) == "/tmp/plain/seg.mp4"


def test_concat_handles_apostrophe_in_path(tmp_path):
    # a DATA_DIR / workdir containing an apostrophe (e.g. /Users/O'Brien/...)
    # must not break the concat demuxer join step.
    import asyncio

    from storybored.export.animatic import _concat_segments

    workdir = tmp_path / "o'brien data"
    workdir.mkdir(parents=True)
    seg0, seg1 = workdir / "seg_0000.mp4", workdir / "seg_0001.mp4"
    make_clip(seg0, duration=0.3, color="red", audio=True)
    make_clip(seg1, duration=0.3, color="blue", audio=True)
    dest = workdir / "out.mp4"

    asyncio.run(_concat_segments([seg0, seg1], dest, workdir))
    assert dest.is_file() and dest.stat().st_size > 0
    duration, _ = probe(dest)
    assert duration > 0.4  # both segments joined, not truncated


def test_zero_duration_shot_clamps_not_default(client, app, settings):
    # duration_s == 0 must clamp to the 0.1s floor, NOT balloon to the 4.0s
    # default (`or 4.0` swallowed 0).
    from PIL import Image

    media = settings.data_path / "media" / "z"
    media.mkdir(parents=True, exist_ok=True)
    still = media / "s.png"
    Image.new("RGB", (320, 240), (10, 20, 30)).save(still)

    with Session(app.state.engine, expire_on_commit=False) as session:
        project = Project(title="Zero")
        session.add(project)
        session.flush()
        scene = Scene(project_id=project.id, idx=0)
        session.add(scene)
        session.flush()
        shot = Shot(scene_id=scene.id, idx=0, duration_s=0.0, status="generated")
        session.add(shot)
        session.flush()
        take = Take(
            shot_id=shot.id,
            kind="image",
            status="done",
            file_path=str(still.relative_to(settings.data_path)),
        )
        session.add(take)
        session.flush()
        shot.picked_take_id = take.id
        session.add(shot)
        session.commit()
        pid = project.id

    r = client.post(f"/api/projects/{pid}/animatic")
    assert r.status_code == 200
    job = wait_for(client, r.json()["job_id"], {"done", "failed"})
    assert job["status"] == "done", job.get("error")
    result = json.loads(job["result_json"])
    assert result["duration_s"] == 0.1  # clamped floor, not the 4.0 default
    duration, _ = probe(settings.data_path / result["file_path"])
    assert duration < 1.0  # definitely not a 4-second hold
