# OWNED-BY: lifecycle-agent
"""Project lifecycle: FK enforcement, delete cleanup on disk, job ownership."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from storybored.models import Job, Scene, Shot, ShotCharacter, Take

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
