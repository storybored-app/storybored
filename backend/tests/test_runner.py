"""Job runner: lifecycle with a fake handler, cancel, startup recovery."""

import asyncio
import json
import time

from fastapi.testclient import TestClient
from sqlmodel import Session

from storybored.config import Settings
from storybored.db import init_db
from storybored.jobs.registry import register
from storybored.main import create_app
from storybored.models import Job, Project, Scene, Shot, Take


@register("t_ok")
async def _t_ok(job, ctx):
    ctx.update_progress(0.5, "halfway")
    return {"answer": 42}


@register("t_fail")
async def _t_fail(job, ctx):
    raise RuntimeError("boom")


@register("t_slow")
async def _t_slow(job, ctx):
    for _ in range(400):
        ctx.raise_if_cancelled()
        await asyncio.sleep(0.025)
    return {}


def wait_for(client, job_id, statuses, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {statuses}: {job}")


def test_job_success_lifecycle(client, app):
    job = app.state.runner.enqueue("t_ok", {"x": 1})
    row = wait_for(client, job.id, {"done", "failed"})
    assert row["status"] == "done"
    assert row["progress"] == 1.0
    assert json.loads(row["result_json"]) == {"answer": 42}
    assert row["started_at"] is not None
    assert row["finished_at"] is not None
    assert row["detail"] == "halfway"


def test_job_failure(client, app):
    job = app.state.runner.enqueue("t_fail")
    row = wait_for(client, job.id, {"done", "failed"})
    assert row["status"] == "failed"
    assert "boom" in row["error"]


def test_unknown_job_type_fails(client, app):
    job = app.state.runner.enqueue("no_such_type")
    row = wait_for(client, job.id, {"failed"})
    assert "no handler" in row["error"]


def test_cancel_running_job(client, app):
    job = app.state.runner.enqueue("t_slow")
    wait_for(client, job.id, {"running"})
    r = client.post(f"/api/jobs/{job.id}/cancel")
    assert r.status_code == 200
    row = wait_for(client, job.id, {"cancelled"})
    assert row["status"] == "cancelled"


def test_cancel_queued_job_and_lane_serialization(client, app):
    slow = app.state.runner.enqueue("t_slow")
    queued = app.state.runner.enqueue("t_ok")
    wait_for(client, slow.id, {"running"})
    # single gpu lane → second job still queued behind the slow one
    assert client.get(f"/api/jobs/{queued.id}").json()["status"] == "queued"

    r = client.post(f"/api/jobs/{queued.id}/cancel")
    assert r.status_code == 200
    assert wait_for(client, queued.id, {"cancelled"})["status"] == "cancelled"

    client.post(f"/api/jobs/{slow.id}/cancel")
    wait_for(client, slow.id, {"cancelled"})

    # cancelling a finished job → 409
    assert client.post(f"/api/jobs/{slow.id}/cancel").status_code == 409


def test_startup_recovery(settings):
    app = create_app(settings)
    init_db(app.state.engine)
    with Session(app.state.engine, expire_on_commit=False) as session:
        interrupted = Job(type="t_slow", status="running", lane="gpu")
        resumed = Job(type="t_ok", status="queued", lane="gpu")
        session.add(interrupted)
        session.add(resumed)
        session.commit()
        interrupted_id, resumed_id = interrupted.id, resumed.id

    with TestClient(app) as client:
        row = client.get(f"/api/jobs/{interrupted_id}").json()
        assert row["status"] == "failed"
        assert row["error"] == "interrupted by restart"
        row = wait_for(client, resumed_id, {"done"})
        assert row["status"] == "done"


def test_queued_cancel_is_not_reclaimed(client, app):
    # A job cancelled while queued must never be silently re-claimed and run by
    # the worker (the lost-update race). Occupy the lane, cancel the job waiting
    # behind it, then free the lane and confirm it stays cancelled + never ran.
    slow = app.state.runner.enqueue("t_slow")
    queued = app.state.runner.enqueue("t_ok")
    wait_for(client, slow.id, {"running"})
    assert client.get(f"/api/jobs/{queued.id}").json()["status"] == "queued"

    assert client.post(f"/api/jobs/{queued.id}/cancel").status_code == 200
    assert wait_for(client, queued.id, {"cancelled"})["status"] == "cancelled"

    # free the lane; the worker gets a wake + a claim pass — it must skip the
    # cancelled row rather than flip it to running.
    client.post(f"/api/jobs/{slow.id}/cancel")
    wait_for(client, slow.id, {"cancelled"})
    time.sleep(0.4)

    row = client.get(f"/api/jobs/{queued.id}").json()
    assert row["status"] == "cancelled"
    assert row["started_at"] is None  # never claimed → running
    assert row["result_json"] in (None, "")  # handler never executed


def test_mode_switch_failure_fails_job_and_invalidates_family(tmp_path):
    # A non-zero mode-switch command must FAIL the job (never run against the
    # wrong engine mode) and leave _last_family unresolved so the next job
    # retries the switch instead of trusting a switch that never happened.
    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        comfy_mode_image_cmd="echo switching to image; exit 7",
        comfy_mode_video_cmd="",
        comfy_flush_cmd="",
        llm_base_url="",
        lora_factory_dir="",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        runner = app.state.runner
        runner._last_family = "video"  # pretend we were last in video mode

        job = runner.enqueue("image_gen", {"shot_id": 999999})
        row = wait_for(client, job.id, {"failed", "done", "cancelled"})
        assert row["status"] == "failed", row
        assert "switch command failed" in row["error"]
        assert "exit 7" in row["detail"]  # captured command output preserved
        # NOT advanced to "image" and NOT left at the stale "video": unresolved.
        assert runner._last_family is None


def test_mode_switch_polls_effective_comfy_url(tmp_path, monkeypatch):
    # The post-switch readiness poll must honor the DB-overridden ComfyUI URL
    # (what the Settings UI writes), not just the env value — otherwise a URL
    # configured only in Settings shows health "ok" but every job times out.
    from fake_comfy import FakeComfy

    import storybored.jobs.runner as runner_mod

    monkeypatch.setattr(runner_mod, "COMFY_WAIT_S", 2.0)  # fast failure on regression
    fake = FakeComfy()
    fake.start()
    try:
        settings = Settings(
            _env_file=None,
            data_dir=str(tmp_path / "data"),
            comfyui_url="http://127.0.0.1:9",  # env points at a dead port
            comfy_mode_image_cmd="echo switching",  # forces the readiness poll
            comfy_mode_video_cmd="",
            comfy_flush_cmd="",
            llm_base_url="",
            lora_factory_dir="",
        )
        app = create_app(settings)
        with TestClient(app) as client:
            client.put(
                "/api/settings", json={"values": {"comfyui_url": fake.url}}
            ).raise_for_status()
            runner = app.state.runner
            runner._last_family = "video"  # force an image-mode switch

            job = runner.enqueue("image_gen", {"shot_id": 999999})
            row = wait_for(client, job.id, {"failed", "done", "cancelled"})
            # The job fails on the missing shot — meaning the readiness poll hit
            # the overridden (live) URL and let the job through. With the old
            # env-only URL it would fail with "not reachable" instead.
            assert row["status"] == "failed"
            assert "not found" in row["error"]
            assert "not reachable" not in row["error"]
    finally:
        fake.stop()


def test_startup_recovery_settles_takes_and_shots(settings):
    # A crash mid-generation leaves a 'pending' take and a 'queued' shot. Startup
    # recovery must fail the interrupted job AND settle its side effects.
    app = create_app(settings)
    init_db(app.state.engine)
    with Session(app.state.engine, expire_on_commit=False) as s:
        project = Project(title="Recover")
        s.add(project)
        s.flush()
        scene = Scene(project_id=project.id, idx=0)
        s.add(scene)
        s.flush()
        shot_draft = Shot(scene_id=scene.id, idx=0, status="queued")  # no done take
        shot_gen = Shot(scene_id=scene.id, idx=1, status="queued")  # has a done take
        s.add(shot_draft)
        s.add(shot_gen)
        s.flush()
        pending = Take(shot_id=shot_draft.id, kind="image", status="pending")
        done = Take(shot_id=shot_gen.id, kind="image", status="done", file_path="x.png")
        s.add(pending)
        s.add(done)
        s.flush()
        job = Job(
            type="image_gen",
            status="running",
            lane="gpu",
            payload_json=json.dumps({"shot_id": shot_draft.id}),
        )
        s.add(job)
        s.commit()
        ids = {
            "job": job.id,
            "pending": pending.id,
            "done": done.id,
            "shot_draft": shot_draft.id,
            "shot_gen": shot_gen.id,
        }

    with TestClient(app):  # lifespan runs _recover on startup
        pass

    with Session(app.state.engine, expire_on_commit=False) as s:
        assert s.get(Job, ids["job"]).status == "failed"
        assert s.get(Take, ids["pending"]).status == "failed"  # orphan take settled
        assert s.get(Take, ids["done"]).status == "done"  # untouched
        # queued shots un-stuck: draft when no done take, generated when one exists
        assert s.get(Shot, ids["shot_draft"]).status == "draft"
        assert s.get(Shot, ids["shot_gen"]).status == "generated"


def test_jobs_listing_filter(client, app):
    ok = app.state.runner.enqueue("t_ok")
    wait_for(client, ok.id, {"done"})
    fail = app.state.runner.enqueue("t_fail")
    wait_for(client, fail.id, {"failed"})

    done_ids = [j["id"] for j in client.get("/api/jobs", params={"status": "done"}).json()]
    assert ok.id in done_ids
    assert fail.id not in done_ids
