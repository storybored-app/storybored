# OWNED-BY: engine-agent (video handler) / export-agent (render endpoints)
"""video_gen handler + render endpoints, with a faked engine client
(no ComfyUI, no network): asserts graph params, approval guards, take wiring."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import imageio_ffmpeg
import pytest
from PIL import Image
from sqlmodel import Session, select

import storybored.engine.video as video_mod
from storybored.models import Character, Project, Scene, Shot, ShotCharacter, Take

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def wait_for(client, job_id, statuses, timeout=60.0):
    deadline = time.monotonic() + timeout
    job = None
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {statuses}: {job}")


class FakeComfyClient:
    """Duck-typed stand-in for engine.comfy_client.ComfyClient."""

    def __init__(self, result_mp4: Path):
        self.result_mp4 = result_mp4
        self.uploaded_bytes: bytes | None = None
        self.uploaded_name: str | None = None
        self.graph: dict | None = None
        self.downloaded: tuple | None = None
        self.cancelled: list[str] = []

    async def upload_image(self, data, name, subfolder="", overwrite=True):
        self.uploaded_bytes = data
        self.uploaded_name = name
        return {"name": name, "subfolder": "", "type": "input"}

    async def submit(self, graph):
        self.graph = graph
        return "fake-prompt-id"

    async def wait_for(self, prompt_id, poll_interval=1.0, on_status=None, should_cancel=None):
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {
                "16": {
                    "images": [
                        {"filename": "take.mp4", "subfolder": "storybored", "type": "output"}
                    ]
                }
            },
        }

    async def download(self, filename, subfolder="", folder_type="output", dest=None):
        self.downloaded = (filename, subfolder, folder_type)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.result_mp4, dest)
        return dest.read_bytes()

    async def cancel(self, prompt_id):
        self.cancelled.append(prompt_id)


@pytest.fixture
def fake_client(tmp_path, monkeypatch):
    """Monkeypatch the client seam; downloads deliver a real tiny MP4 so the
    handler's imageio thumbnail step runs for real."""
    mp4 = tmp_path / "fake_result.mp4"
    subprocess.run(
        [
            FFMPEG, "-y",
            "-f", "lavfi", "-i", "color=c=green:s=320x240:r=24:d=0.25",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(mp4),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    fake = FakeComfyClient(mp4)
    monkeypatch.setattr(video_mod, "_make_client", lambda comfy_url: fake)
    return fake


def seed_shot(app, settings, *, approved=True, motion_prompt="@rex turns slowly toward camera"):
    """Project/scene/shot + character @rex + a finished picked still."""
    media = settings.data_path / "media" / "seed"
    media.mkdir(parents=True, exist_ok=True)
    still = media / "still.png"
    if not still.is_file():
        Image.new("RGB", (864, 576), (30, 30, 30)).save(still)

    with Session(app.state.engine, expire_on_commit=False) as session:
        rex = session.exec(select(Character).where(Character.handle == "rex")).first()
        if rex is None:
            rex = Character(
                name="Rex", handle="rex", trigger="zxqdog", class_word="dog", status="ready"
            )
            session.add(rex)
            session.flush()
        char_id = rex.id
        project = Project(title="Video Test")
        session.add(project)
        session.flush()
        scene = Scene(project_id=project.id, idx=0)
        session.add(scene)
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            idx=0,
            description="Hero @rex stands in the doorway",
            motion_prompt=motion_prompt,
            duration_s=4.0,
        )
        session.add(shot)
        session.flush()
        session.add(ShotCharacter(shot_id=shot.id, character_id=char_id))
        take = Take(
            shot_id=shot.id,
            kind="image",
            status="done",
            file_path=str(still.relative_to(settings.data_path)),
        )
        session.add(take)
        session.flush()
        shot.picked_take_id = take.id
        shot.status = "approved" if approved else "generated"
        session.add(shot)
        session.commit()
        return {"project_id": project.id, "shot_id": shot.id, "picked_path": still}


def test_video_gen_happy_path(client, app, settings, fake_client):
    ids = seed_shot(app, settings)
    shot_id = ids["shot_id"]

    r = client.post(f"/api/shots/{shot_id}/render-video", json={})
    assert r.status_code == 200
    job = wait_for(client, r.json()["job_id"], {"done", "failed"})
    assert job["status"] == "done", job["error"]

    # take row: kind=video, done, real files on disk, shot points at it
    takes = client.get(f"/api/shots/{shot_id}/takes").json()
    video_takes = [t for t in takes if t["kind"] == "video"]
    assert len(video_takes) == 1
    take = video_takes[0]
    assert take["status"] == "done"
    assert take["workflow_id"] == "minimax-h3-i2v"
    file_path = settings.data_path / take["file_path"]
    thumb_path = settings.data_path / take["thumb_path"]
    assert file_path.is_file() and file_path.suffix == ".mp4"
    assert f"take_{take['id']}.mp4" in take["file_path"]
    assert thumb_path.is_file()  # first-frame thumb via imageio
    with Image.open(thumb_path) as img:
        assert max(img.size) <= 384
    shot = client.get(f"/api/shots/{shot_id}").json()
    assert shot["video_take_id"] == take["id"]
    assert json.loads(job["result_json"])["take_id"] == take["id"]

    # the picked still was uploaded, and the graph got the right params
    assert fake_client.uploaded_bytes == ids["picked_path"].read_bytes()
    graph = fake_client.graph
    assert graph is not None
    # prompt: motion_prompt with @rex → "zxqdog dog"
    assert graph["6"]["inputs"]["prompt"] == "zxqdog dog turns slowly toward camera"
    # first_frame param = the uploaded image's server-side name
    assert graph["5"]["inputs"]["image"] == fake_client.uploaded_name
    # the handler downloaded the engine's reported output file
    assert fake_client.downloaded == ("take.mp4", "storybored", "output")
    # manifest defaults applied
    assert graph["6"]["inputs"]["length"] == 124
    assert graph["6"]["inputs"]["width"] == 864
    assert graph["6"]["inputs"]["height"] == 576
    # seed wired to node 10 and persisted on the take
    assert graph["10"]["inputs"]["noise_seed"] == take["seed"]
    # unambiguous output naming
    assert graph["16"]["inputs"]["filename_prefix"] == f"storybored/take_{take['id']}"


def test_video_gen_falls_back_to_description_prompt(client, app, settings, fake_client):
    ids = seed_shot(app, settings, motion_prompt="")
    r = client.post(f"/api/shots/{ids['shot_id']}/render-video", json={})
    job = wait_for(client, r.json()["job_id"], {"done", "failed"})
    assert job["status"] == "done", job["error"]
    assert fake_client.graph["6"]["inputs"]["prompt"] == "Hero zxqdog dog stands in the doorway"


def test_render_video_updates_motion_prompt(client, app, settings, fake_client):
    ids = seed_shot(app, settings)
    r = client.post(
        f"/api/shots/{ids['shot_id']}/render-video",
        json={"motion_prompt": "@rex sprints away"},
    )
    assert r.status_code == 200
    assert client.get(f"/api/shots/{ids['shot_id']}").json()["motion_prompt"] == (
        "@rex sprints away"
    )
    job = wait_for(client, r.json()["job_id"], {"done", "failed"})
    assert job["status"] == "done", job["error"]
    assert fake_client.graph["6"]["inputs"]["prompt"] == "zxqdog dog sprints away"


def test_render_video_requires_approval(client, app, settings, fake_client):
    ids = seed_shot(app, settings, approved=False)
    r = client.post(f"/api/shots/{ids['shot_id']}/render-video", json={})
    assert r.status_code == 409
    assert "approved" in r.json()["detail"]

    # the handler enforces the same guard even on a directly-enqueued job
    job = app.state.runner.enqueue("video_gen", {"shot_id": ids["shot_id"]})
    row = wait_for(client, job.id, {"done", "failed"})
    assert row["status"] == "failed"
    assert "approved" in row["error"]
    # and no video take was created
    takes = client.get(f"/api/shots/{ids['shot_id']}/takes").json()
    assert [t for t in takes if t["kind"] == "video"] == []


def test_render_video_missing_shot_404(client, fake_client):
    assert client.post("/api/shots/424242/render-video", json={}).status_code == 404


def test_render_videos_queues_only_approved_without_video(client, app, settings, fake_client):
    ids = seed_shot(app, settings)  # approved, no video take yet
    project_id = ids["project_id"]

    # second shot in the same project: approved but ALREADY has a video take
    with Session(app.state.engine, expire_on_commit=False) as session:
        scene = session.exec(select(Scene).where(Scene.project_id == project_id)).first()
        shot2 = Shot(scene_id=scene.id, idx=1, description="already rendered")
        session.add(shot2)
        session.flush()
        still_rel = str(ids["picked_path"].relative_to(settings.data_path))
        picked2 = Take(shot_id=shot2.id, kind="image", status="done", file_path=still_rel)
        session.add(picked2)
        session.flush()
        video2 = Take(shot_id=shot2.id, kind="video", status="done", file_path=still_rel)
        session.add(video2)
        session.flush()
        shot2.picked_take_id = picked2.id
        shot2.video_take_id = video2.id
        shot2.status = "approved"
        session.add(shot2)
        # third shot: draft, never queued
        shot3 = Shot(scene_id=scene.id, idx=2, description="draft")
        session.add(shot3)
        session.commit()

    r = client.post(f"/api/projects/{project_id}/render-videos")
    assert r.status_code == 200
    body = r.json()
    assert body["queued"] == 1
    job = wait_for(client, body["job_ids"][0], {"done", "failed"})
    assert job["status"] == "done", job["error"]
    assert client.get(f"/api/shots/{ids['shot_id']}").json()["video_take_id"] is not None

    # everything now has a video take → nothing further to queue
    assert client.post(f"/api/projects/{project_id}/render-videos").json()["queued"] == 0
