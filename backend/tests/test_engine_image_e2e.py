# OWNED-BY: engine-agent
"""Full image_gen pipeline against the fake ComfyUI: takes, files, shot status."""

import json
import time

import pytest
from fake_comfy import fake_comfy  # noqa: F401 - fixture
from sqlmodel import Session, select

from storybored.config import Settings
from storybored.engine.comfy_client import clear_object_info_cache
from storybored.models import ShotCharacter


@pytest.fixture
def settings(tmp_path, fake_comfy):  # noqa: F811 - overrides conftest settings
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        comfyui_url=fake_comfy.url,
        comfy_mode_image_cmd="",
        comfy_mode_video_cmd="",
        comfy_flush_cmd="",
        llm_base_url="",
        lora_factory_dir="",
    )


def wait_job(client, job_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"done", "failed", "cancelled"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {job}")


def make_board(client, description="WIDE: @hero waits by the door"):
    project = client.post("/api/projects", json={"title": "Test Film"}).json()
    scene = client.post(f"/api/projects/{project['id']}/scenes", json={"title": "One"}).json()
    shot = client.post(
        f"/api/scenes/{scene['id']}/shots", json={"description": description}
    ).json()
    return project, scene, shot


def make_hero(client):
    r = client.post(
        "/api/characters",
        json={
            "name": "Hero",
            "handle": "hero",
            "trigger": "zxqhero",
            "class_word": "woman",
            "lora_name": "characters/hero_v1.safetensors",
            "lora_strength": 0.9,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_image_gen_e2e(client, app, settings, fake_comfy):  # noqa: F811
    make_hero(client)
    project, scene, shot = make_board(client)

    r = client.post(
        f"/api/shots/{shot['id']}/generate",
        json={"workflow_id": "krea2-realism", "n_takes": 2},
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    # shot flips to queued at enqueue time
    assert client.get(f"/api/shots/{shot['id']}").json()["status"] == "queued"

    job = wait_job(client, job_id)
    assert job["status"] == "done", job
    result = json.loads(job["result_json"])
    assert len(result["take_ids"]) == 2
    assert result["failed"] == 0

    takes = client.get(f"/api/shots/{shot['id']}/takes").json()
    assert len(takes) == 2
    seeds = set()
    for take in takes:
        assert take["status"] == "done"
        assert take["workflow_id"] == "krea2-realism"
        seeds.add(take["seed"])
        # files on disk under DATA_DIR/media/{project}/{shot}/
        expected = f"media/{project['id']}/{shot['id']}/take_{take['id']}.png"
        assert take["file_path"] == expected
        assert (settings.data_path / take["file_path"]).is_file()
        assert (settings.data_path / take["thumb_path"]).is_file()
        # media endpoint serves both
        assert client.get(f"/api/media/{take['file_path']}").status_code == 200
    assert len(seeds) == 2  # fresh random seed per take

    assert client.get(f"/api/shots/{shot['id']}").json()["status"] == "generated"

    # the submitted graphs carry the injected character LoRA + zeroed style loras
    assert len(fake_comfy.state.prompts) == 2
    for take, prompt_id in zip(takes, fake_comfy.state.order):
        graph = fake_comfy.state.prompts[prompt_id]
        char_nodes = [
            n
            for n in graph.values()
            if n["class_type"] == "LoraLoader"
            and n["inputs"]["lora_name"] == "characters/hero_v1.safetensors"
        ]
        assert len(char_nodes) == 1
        assert char_nodes[0]["inputs"]["model"] == ["lora_7", 0]
        assert char_nodes[0]["inputs"]["strength_model"] == 0.9
        assert graph["lora_3"]["inputs"]["strength_model"] == 0
        assert graph["lora_4"]["inputs"]["strength_clip"] == 0
        # prompt has the mention substituted
        assert graph["6"]["inputs"]["text"] == "WIDE: zxqhero woman waits by the door"
        assert graph["9"]["inputs"]["filename_prefix"] == f"storybored/take_{take['id']}"
        assert graph["3"]["inputs"]["seed"] == take["seed"]

    # shotcharacter link refreshed from mentions
    with Session(app.state.engine) as session:
        links = session.exec(
            select(ShotCharacter).where(ShotCharacter.shot_id == shot["id"])
        ).all()
        assert len(links) == 1


def test_style_loras_setting_injected_into_render(client, fake_comfy):  # noqa: F811
    make_hero(client)
    r = client.put(
        "/api/settings",
        json={
            "values": {
                "style_loras": json.dumps(
                    [
                        {"lora_name": "styles/noir.safetensors", "strength": 0.6, "enabled": True},
                        {"lora_name": "styles/vhs.safetensors", "strength": 1.0, "enabled": False},
                    ]
                )
            }
        },
    )
    assert r.status_code == 200, r.text

    _, _, shot = make_board(client)
    r = client.post(
        f"/api/shots/{shot['id']}/generate",
        json={"workflow_id": "krea2-realism", "n_takes": 1},
    )
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == "done", job

    (prompt_id,) = fake_comfy.state.order
    graph = fake_comfy.state.prompts[prompt_id]
    by_lora = {
        n["inputs"]["lora_name"]: n
        for n in graph.values()
        if n["class_type"] == "LoraLoader"
    }
    # disabled entry never reaches the graph
    assert "styles/vhs.safetensors" not in by_lora
    # chain: lora_7 -> style -> char -> sage/6
    style = by_lora["styles/noir.safetensors"]
    char_node = by_lora["characters/hero_v1.safetensors"]
    assert style["inputs"]["model"] == ["lora_7", 0]
    assert style["inputs"]["strength_model"] == 0.6
    style_id = next(k for k, v in graph.items() if v is style)
    assert char_node["inputs"]["model"] == [style_id, 0]
    char_id = next(k for k, v in graph.items() if v is char_node)
    assert graph["sage"]["inputs"]["model"] == [char_id, 0]
    assert graph["6"]["inputs"]["clip"] == [char_id, 1]


def test_pinned_seed_repeats(client, fake_comfy):  # noqa: F811
    _, _, shot = make_board(client, description="a quiet hallway")
    r = client.post(
        f"/api/shots/{shot['id']}/generate",
        json={"workflow_id": "krea2-basic", "n_takes": 2, "params": {"seed": 1234}},
    )
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == "done"
    takes = client.get(f"/api/shots/{shot['id']}/takes").json()
    assert [t["seed"] for t in takes] == [1234, 1234]


def test_default_workflow_used_when_omitted(client, fake_comfy):  # noqa: F811
    _, _, shot = make_board(client, description="an empty street")
    r = client.post(f"/api/shots/{shot['id']}/generate", json={})
    assert r.status_code == 200, r.text
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert json.loads(job["payload_json"])["workflow_id"] == "krea2-basic"
    wait_job(client, job["id"])


def test_generate_unknown_workflow_404(client, fake_comfy):  # noqa: F811
    _, _, shot = make_board(client)
    r = client.post(f"/api/shots/{shot['id']}/generate", json={"workflow_id": "nope"})
    assert r.status_code == 404
    assert client.post(
        "/api/shots/99999/generate", json={"workflow_id": "krea2-basic"}
    ).status_code == 404


def test_generate_missing_models_409(client, fake_comfy):  # noqa: F811
    _, _, shot = make_board(client)
    fake_comfy.state.models["LoraLoader.lora_name"] = ["characters/hero_v1.safetensors"]
    clear_object_info_cache()
    r = client.post(f"/api/shots/{shot['id']}/generate", json={"workflow_id": "krea2-realism"})
    assert r.status_code == 409
    assert "realism_engine_krea2_v3.1.safetensors" in r.json()["detail"]
    # shot untouched
    assert client.get(f"/api/shots/{shot['id']}").json()["status"] == "draft"


def test_generate_failure_marks_take_and_job_failed(client, fake_comfy):  # noqa: F811
    _, _, shot = make_board(client, description="a dark room")
    fake_comfy.state.fail_submit_error = "CUDA out of memory"
    r = client.post(
        f"/api/shots/{shot['id']}/generate", json={"workflow_id": "krea2-basic", "n_takes": 1}
    )
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "CUDA out of memory" in job["error"]
    takes = client.get(f"/api/shots/{shot['id']}/takes").json()
    assert len(takes) == 1
    assert takes[0]["status"] == "failed"
    assert "CUDA out of memory" in takes[0]["error"]
    # the shot must not stay stuck on "queued" after a failed job
    assert wait_shot_status(client, shot["id"], "draft") == "draft"


def wait_shot_status(client, shot_id, expected, timeout=5.0):
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        status = client.get(f"/api/shots/{shot_id}").json()["status"]
        if status == expected:
            return status
        time.sleep(0.05)
    return status


def test_failed_regen_settles_shot_back_to_generated(client, fake_comfy):  # noqa: F811
    """A shot with a good take that later gets a failed generation must return
    to "generated", never stay "queued"."""
    _, _, shot = make_board(client, description="a lighthouse at dusk")
    r = client.post(
        f"/api/shots/{shot['id']}/generate", json={"workflow_id": "krea2-basic", "n_takes": 1}
    )
    assert wait_job(client, r.json()["job_id"])["status"] == "done"
    assert client.get(f"/api/shots/{shot['id']}").json()["status"] == "generated"

    fake_comfy.state.fail_submit_error = "CUDA out of memory"
    r = client.post(
        f"/api/shots/{shot['id']}/generate", json={"workflow_id": "krea2-basic", "n_takes": 1}
    )
    assert wait_job(client, r.json()["job_id"])["status"] == "failed"
    assert wait_shot_status(client, shot["id"], "generated") == "generated"


def test_workflows_endpoint_availability(client, fake_comfy):  # noqa: F811
    rows = {w["id"]: w for w in client.get("/api/workflows").json()}
    assert rows["krea2-basic"]["available"] is True
    assert rows["krea2-realism"]["available"] is True
    assert rows["krea2-realism"]["missing_models"] == []
    assert rows["krea2-realism"]["supports_characters"] is True
    assert any(p["key"] == "prompt" for p in rows["krea2-basic"]["parameters"])

    fake_comfy.state.models["UNETLoader.unet_name"] = []
    clear_object_info_cache()
    rows = {w["id"]: w for w in client.get("/api/workflows").json()}
    assert rows["krea2-basic"]["available"] is False
    assert "krea2_raw_fp8_scaled.safetensors" in rows["krea2-basic"]["missing_models"]
