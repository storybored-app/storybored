# OWNED-BY: lifecycle-agent
"""Project lifecycle: FK enforcement, delete cleanup on disk, job ownership."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from storybored.models import Character, Job, Scene, Shot, ShotCharacter, Take

# -- foreign keys are enforced -------------------------------------------------


def test_foreign_keys_are_enforced(app, client):
    with Session(app.state.engine) as session:
        for orphan in (
            Scene(project_id=999_999, title="orphan"),
            Shot(scene_id=999_999),
            Take(shot_id=999_999),
            ShotCharacter(shot_id=999_999, character_id=999_999),
        ):
            session.add(orphan)
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()


# -- job.project_id is stamped at enqueue --------------------------------------


def make_board(client, description="a quiet hallway"):
    project = client.post("/api/projects", json={"title": "Lifecycle"}).json()
    scene = client.post(f"/api/projects/{project['id']}/scenes", json={"title": "One"}).json()
    shot = client.post(
        f"/api/scenes/{scene['id']}/shots", json={"description": description}
    ).json()
    return project, scene, shot


def test_animatic_job_carries_project_id(app, client):
    project, _, _ = make_board(client)
    r = client.post(f"/api/projects/{project['id']}/animatic")
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    with Session(app.state.engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.project_id == project["id"]


def test_training_style_jobs_stay_project_less(app, client):
    """Jobs enqueued without a project (character/training) keep project_id null."""
    runner = app.state.runner
    job = runner.enqueue("lora_train", {"character_id": 1})
    with Session(app.state.engine) as session:
        row = session.exec(select(Job).where(Job.id == job.id)).one()
        assert row.project_id is None


# -- delete cleanup on disk ----------------------------------------------------


def fabricate_take(app, settings, project_id: int, shot_id: int, kind="image") -> dict:
    """A finished take with real (tiny) files on disk, no engine involved."""
    ext = "png" if kind == "image" else "mp4"
    with Session(app.state.engine, expire_on_commit=False) as session:
        take = Take(shot_id=shot_id, kind=kind, status="done")
        session.add(take)
        session.commit()
        session.refresh(take)
        dest_dir = settings.media_path / str(project_id) / str(shot_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        file = dest_dir / f"take_{take.id}.{ext}"
        thumb = dest_dir / f"take_{take.id}_thumb.png"
        file.write_bytes(b"fake-media")
        thumb.write_bytes(b"fake-thumb")
        take.file_path = str(file.relative_to(settings.data_path))
        take.thumb_path = str(thumb.relative_to(settings.data_path))
        session.add(take)
        session.commit()
    return {"id": take.id, "file": file, "thumb": thumb, "file_path": take.file_path}


def test_delete_shot_unlinks_take_files(app, client, settings):
    project, _, shot = make_board(client)
    t = fabricate_take(app, settings, project["id"], shot["id"])
    assert t["file"].is_file() and t["thumb"].is_file()

    assert client.delete(f"/api/shots/{shot['id']}").status_code == 204
    assert not t["file"].exists()
    assert not t["thumb"].exists()


def test_delete_scene_unlinks_take_files(app, client, settings):
    project, scene, shot = make_board(client)
    t1 = fabricate_take(app, settings, project["id"], shot["id"])
    t2 = fabricate_take(app, settings, project["id"], shot["id"], kind="video")

    assert client.delete(f"/api/scenes/{scene['id']}").status_code == 204
    for t in (t1, t2):
        assert not t["file"].exists()
        assert not t["thumb"].exists()


def test_delete_project_removes_trees_jobs_and_thumbnails(app, client, settings):
    project, _, shot = make_board(client, description="CU: @keeper smiles")
    pid = project["id"]
    t = fabricate_take(app, settings, pid, shot["id"])

    # an export on disk, a finished job row and a queued one on a dormant lane
    export_dir = settings.exports_path / str(pid)
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / "animatic_1.mp4").write_bytes(b"fake-mp4")
    with Session(app.state.engine, expire_on_commit=False) as session:
        session.add(Job(type="image_gen", status="done", project_id=pid))
        session.add(Job(type="video_gen", status="queued", lane="hold", project_id=pid))
        session.commit()

    # a character whose auto-thumbnail lives inside this project's media
    r = client.post(
        "/api/characters",
        json={"name": "Keeper", "handle": "keeper", "trigger": "zxkeeper"},
    )
    assert r.status_code == 201
    char_id = r.json()["id"]
    with Session(app.state.engine) as session:
        char = session.get(Character, char_id)
        char.thumbnail_path = t["file_path"]
        session.add(char)
        session.commit()

    # a second project must be untouched by the delete
    other, _, other_shot = make_board(client)
    keep = fabricate_take(app, settings, other["id"], other_shot["id"])

    assert client.delete(f"/api/projects/{pid}").status_code == 204

    assert not (settings.media_path / str(pid)).exists()
    assert not (settings.exports_path / str(pid)).exists()
    assert keep["file"].is_file()  # other project untouched
    with Session(app.state.engine) as session:
        assert session.exec(select(Job).where(Job.project_id == pid)).all() == []
        char = session.get(Character, char_id)
        assert char is not None  # characters are global — never deleted with a project
        assert char.thumbnail_path is None  # ...but the dangling thumb is cleared


def test_delete_project_cancels_queued_jobs(app, client, settings):
    project, _, _ = make_board(client)
    pid = project["id"]
    cancelled: list[int] = []
    runner = app.state.runner
    real_cancel = runner.cancel
    runner.cancel = lambda job_id: (cancelled.append(job_id), real_cancel(job_id))[1]
    try:
        with Session(app.state.engine, expire_on_commit=False) as session:
            job = Job(type="video_gen", status="queued", lane="hold", project_id=pid)
            session.add(job)
            session.commit()
        assert client.delete(f"/api/projects/{pid}").status_code == 204
    finally:
        runner.cancel = real_cancel
    assert cancelled == [job.id]


def test_image_gen_fails_cleanly_when_scene_is_gone(app, client, settings):
    """No media/0 orphan bucket: a shot without its scene fails the job."""
    import sqlite3
    import time

    _, scene, shot = make_board(client)
    # FKs are ON for the app; simulate legacy/corrupt data via a raw connection
    raw = sqlite3.connect(settings.db_path)
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("DELETE FROM scene WHERE id=?", (scene["id"],))
    raw.commit()
    raw.close()

    job = app.state.runner.enqueue(
        "image_gen", {"shot_id": shot["id"], "workflow_id": "krea2-basic"}
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = client.get(f"/api/jobs/{job.id}").json()
        if row["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.05)
    assert row["status"] == "failed"
    assert "scene" in (row["error"] or "")
    assert not (settings.media_path / "0").exists()
