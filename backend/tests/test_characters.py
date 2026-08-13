# OWNED-BY: engine-agent
"""Characters API: CRUD, available-loras, import-lora, thumbnail upload."""

import pytest
from fake_comfy import fake_comfy  # noqa: F401 - fixture
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from storybored.config import Settings
from storybored.main import create_app
from storybored.models import ShotCharacter


@pytest.fixture
def loras_dir(tmp_path):
    d = tmp_path / "comfy-loras"
    d.mkdir()
    return d


@pytest.fixture
def settings(tmp_path, fake_comfy, loras_dir):  # noqa: F811 - overrides conftest settings
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        comfyui_url=fake_comfy.url,
        comfy_loras_dir=str(loras_dir),
        comfy_mode_image_cmd="",
        comfy_mode_video_cmd="",
        comfy_flush_cmd="",
        llm_base_url="",
        lora_factory_dir="",
    )


def hero_payload(**overrides):
    payload = {
        "name": "Hero",
        "handle": "hero",
        "trigger": "zxqhero",
        "class_word": "woman",
        "lora_name": "characters/hero_v1.safetensors",
        "lora_strength": 0.9,
        "notes": "lead",
    }
    payload.update(overrides)
    return payload


def test_crud_roundtrip(client):
    r = client.post("/api/characters", json=hero_payload())
    assert r.status_code == 201, r.text
    char = r.json()
    assert char["handle"] == "hero"
    assert char["status"] == "ready"
    assert char["lora_strength"] == 0.9

    rows = client.get("/api/characters").json()
    assert [c["handle"] for c in rows] == ["hero"]

    r = client.patch(
        f"/api/characters/{char['id']}", json={"trigger": "zxq2", "lora_strength": 1.0}
    )
    assert r.status_code == 200
    assert r.json()["trigger"] == "zxq2"
    assert r.json()["lora_strength"] == 1.0

    assert client.delete(f"/api/characters/{char['id']}").status_code == 204
    assert client.get("/api/characters").json() == []
    assert client.patch("/api/characters/999", json={"name": "x"}).status_code == 404


def test_handle_normalization_and_validation(client):
    r = client.post("/api/characters", json=hero_payload(handle="@Hero "))
    assert r.status_code == 201
    assert r.json()["handle"] == "hero"

    # duplicate → 409
    assert client.post("/api/characters", json=hero_payload(handle="hero")).status_code == 409

    # invalid characters → 422
    assert client.post(
        "/api/characters", json=hero_payload(handle="bad handle!")
    ).status_code == 422

    # bad status → 422
    assert client.post(
        "/api/characters", json=hero_payload(handle="other", status="bogus")
    ).status_code == 422

    # renaming onto a taken handle → 409
    rival = client.post("/api/characters", json=hero_payload(handle="rival")).json()
    assert (
        client.patch(f"/api/characters/{rival['id']}", json={"handle": "hero"}).status_code
        == 409
    )


def test_delete_removes_shot_links(client, app):
    char = client.post("/api/characters", json=hero_payload()).json()
    project = client.post("/api/projects", json={"title": "P"}).json()
    scene = client.post(f"/api/projects/{project['id']}/scenes", json={}).json()
    shot = client.post(
        f"/api/scenes/{scene['id']}/shots", json={"description": "CU: @hero smiles"}
    ).json()

    with Session(app.state.engine) as session:
        assert session.exec(
            select(ShotCharacter).where(ShotCharacter.shot_id == shot["id"])
        ).all()

    assert client.delete(f"/api/characters/{char['id']}").status_code == 204
    with Session(app.state.engine) as session:
        assert not session.exec(select(ShotCharacter)).all()


def test_available_loras(client, fake_comfy):  # noqa: F811
    loras = client.get("/api/characters/available-loras").json()
    assert "characters/hero_v1.safetensors" in loras
    assert "(Krea 2) 8-Step Turbo Distill Rank 64 V2026.1.safetensors" in loras


def test_import_lora_by_name(client):
    r = client.post(
        "/api/characters/import-lora",
        json={"lora_name": "characters/rival_v1.safetensors"},
    )
    assert r.status_code == 200
    assert r.json() == {"lora_name": "characters/rival_v1.safetensors", "imported": False}

    r = client.post("/api/characters/import-lora", json={"lora_name": "nope.safetensors"})
    assert r.status_code == 400
    assert "not in the engine's LoRA list" in r.json()["detail"]

    assert client.post("/api/characters/import-lora", json={}).status_code == 400


def test_import_lora_multipart(client, loras_dir):
    r = client.post(
        "/api/characters/import-lora",
        files={"file": ("my_char.safetensors", b"fake-weights", "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"lora_name": "my_char.safetensors", "imported": True}
    assert (loras_dir / "my_char.safetensors").read_bytes() == b"fake-weights"

    # wrong extension rejected
    r = client.post(
        "/api/characters/import-lora",
        files={"file": ("notes.txt", b"hi", "text/plain")},
    )
    assert r.status_code == 400


def test_import_lora_multipart_without_loras_dir(tmp_path, fake_comfy):  # noqa: F811
    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data2"),
        comfyui_url=fake_comfy.url,
        comfy_loras_dir="",
        llm_base_url="",
        lora_factory_dir="",
    )
    with TestClient(create_app(settings)) as client:
        r = client.post(
            "/api/characters/import-lora",
            files={"file": ("x.safetensors", b"w", "application/octet-stream")},
        )
        assert r.status_code == 400
        assert "COMFY_LORAS_DIR" in r.json()["detail"]


def test_thumbnail_upload(client, settings):
    char = client.post("/api/characters", json=hero_payload()).json()
    r = client.post(
        f"/api/characters/{char['id']}/thumbnail",
        files={"file": ("face.png", b"\x89PNG fake", "image/png")},
    )
    assert r.status_code == 200, r.text
    thumb_path = r.json()["thumbnail_path"]
    assert thumb_path == f"media/characters/{char['id']}.png"
    assert (settings.data_path / thumb_path).is_file()
    assert client.get(f"/api/media/{thumb_path}").status_code == 200

    # bad extension → 400
    r = client.post(
        f"/api/characters/{char['id']}/thumbnail",
        files={"file": ("clip.mp4", b"xx", "video/mp4")},
    )
    assert r.status_code == 400
