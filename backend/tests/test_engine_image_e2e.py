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


def test_engine_lora_overrides_reach_render(client, fake_comfy):  # noqa: F811
    r = client.put(
        "/api/settings",
        json={
            "values": {
                "engine_loras": json.dumps(
                    {
                        "krea2-realism": [
                            {"node": "lora_2", "strength": 1.5},
                            {"node": "lora_6", "enabled": False},
                            {"lora_name": "added.safetensors", "strength": 0.5},
                            {"lora_name": "off.safetensors", "enabled": False},
                        ]
                    }
                )
            }
        },
    )
    assert r.status_code == 200, r.text

    _, _, shot = make_board(client, description="a quiet hallway")
    r = client.post(
        f"/api/shots/{shot['id']}/generate",
        json={"workflow_id": "krea2-realism", "n_takes": 1},
    )
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == "done", job

    (prompt_id,) = fake_comfy.state.order
    graph = fake_comfy.state.prompts[prompt_id]
    assert graph["lora_2"]["inputs"]["strength_model"] == 1.5
    assert graph["lora_6"]["inputs"]["strength_model"] == 0
    assert graph["engine_lora_0"]["inputs"]["lora_name"] == "added.safetensors"
    assert graph["engine_lora_0"]["inputs"]["model"] == ["lora_7", 0]
    assert graph["sage"]["inputs"]["model"] == ["engine_lora_0", 0]
    assert not any(
        n["inputs"].get("lora_name") == "off.safetensors"
        for n in graph.values()
        if n["class_type"] == "LoraLoader"
    )


def test_workflows_payload_reports_stack_and_default(client, fake_comfy):  # noqa: F811
    r = client.put(
        "/api/settings",
        json={
            "values": {
                "default_image_workflow": "krea2-realism",
                "engine_loras": json.dumps(
                    {"krea2-realism": [{"node": "lora_6", "enabled": False}]}
                ),
            }
        },
    )
    assert r.status_code == 200, r.text

    workflows = {w["id"]: w for w in client.get("/api/workflows").json()}
    realism = workflows["krea2-realism"]
    assert realism["default"] is True
    assert workflows["krea2-basic"]["default"] is False
    assert realism["loras_modified"] is True
    stack = {row["node"]: row for row in realism["loras"]}
    assert [row["node"] for row in realism["loras"]] == [f"lora_{i}" for i in range(8)]
    assert stack["lora_6"]["enabled"] is False
    assert stack["lora_7"]["strength"] == 0.8
    assert stack["lora_3"]["disabled_with_character"] is True
    assert stack["lora_0"]["disabled_with_character"] is False
    basic = workflows["krea2-basic"]
    assert [row["node"] for row in basic["loras"]] == ["lora_distill"]
    assert basic["loras_modified"] is False


def test_workflows_payload_reports_model_slots(client, fake_comfy):  # noqa: F811
    r = client.put(
        "/api/settings",
        json={
            "values": {
                "engine_models": json.dumps(
                    {"krea2-realism": {"unet": "minimax_h3_fl2va_pruned_nvfp4.safetensors"}}
                )
            }
        },
    )
    assert r.status_code == 200, r.text

    workflows = {w["id"]: w for w in client.get("/api/workflows").json()}
    realism = workflows["krea2-realism"]
    assert realism["models_modified"] is True
    slot = {m["key"]: m for m in realism["models"]}["unet"]
    assert slot["baked"] == "krea2_raw_fp8_scaled.safetensors"
    assert slot["value"] == "minimax_h3_fl2va_pruned_nvfp4.safetensors"  # override wins
    assert slot["node"] == "4" and slot["input"] == "unet_name"
    assert "krea2_raw_fp8_scaled.safetensors" in slot["options"]

    minimax = workflows["minimax-h3-i2v"]
    assert minimax["models_modified"] is False
    mm_slot = {m["key"]: m for m in minimax["models"]}["unet"]
    assert mm_slot["value"] == mm_slot["baked"] == "minimax_h3_fl2va_pruned_nvfp4.safetensors"
    # capability flags: video pack takes LoRAs + last-frame anchoring, no @characters
    assert minimax["supports_loras"] is True
    assert minimax["supports_characters"] is False
    assert minimax["supports_frame_position"] is True
    assert realism["supports_loras"] is True
    assert realism["supports_frame_position"] is False


def test_character_thumbnail_generation(client, app, settings, fake_comfy):  # noqa: F811
    hero = make_hero(client)

    r = client.post(f"/api/characters/{hero['id']}/generate-thumbnail", json={})
    assert r.status_code == 200, r.text
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == "done", job

    result = json.loads(job["result_json"])
    expected_thumb = f"media/characters/{hero['id']}/portrait_{job['id']}_thumb.png"
    assert result["thumbnail_path"] == expected_thumb
    assert (settings.data_path / result["thumbnail_path"]).is_file()
    assert (settings.data_path / result["file_path"]).is_file()

    char = client.get("/api/characters").json()[0]
    assert char["thumbnail_path"] == expected_thumb
    assert client.get(f"/api/media/{expected_thumb}").status_code == 200

    # submitted graph: square portrait, default prompt with the trigger phrase,
    # and the character's LoRA spliced in
    (prompt_id,) = fake_comfy.state.order
    graph = fake_comfy.state.prompts[prompt_id]
    assert graph["5"]["inputs"]["width"] == 1024
    assert graph["5"]["inputs"]["height"] == 1024
    prompt_text = graph["6"]["inputs"]["text"]
    assert prompt_text.startswith("portrait photograph of zxqhero woman")
    assert "wearing" in prompt_text
    assert any(
        n["inputs"].get("lora_name") == "characters/hero_v1.safetensors"
        for n in graph.values()
        if n["class_type"] == "LoraLoader"
    )


def test_character_thumbnail_requires_lora(client, fake_comfy):  # noqa: F811
    r = client.post(
        "/api/characters",
        json={"name": "Extra", "handle": "extra", "trigger": "zxextra"},
    )
    assert r.status_code == 201, r.text
    r = client.post(f"/api/characters/{r.json()['id']}/generate-thumbnail", json={})
    assert r.status_code == 400
    assert "no LoRA" in r.json()["detail"]


HERO_BIO = "Retired jazz trumpeter; wry, unhurried, dresses sharp."
HERO_PORTRAIT = (
    "portrait photograph of @hero with a wry half-smile and an unhurried gaze, "
    "wearing a charcoal three-piece suit with a burgundy tie, collar visible, "
    "chest-up framing, warm club lighting, softly blurred stage backdrop, "
    "sharp focus on the face, photorealistic"
)


def test_character_thumbnail_bio_drives_prompt_via_llm(  # noqa: F811
    client, app, settings, fake_comfy
):
    from fake_llm import FakeLLM

    hero = make_hero(client)
    r = client.patch(f"/api/characters/{hero['id']}", json={"bio": HERO_BIO})
    assert r.status_code == 200 and r.json()["bio"] == HERO_BIO

    llm = FakeLLM().start()
    try:
        r = client.put(
            "/api/settings",
            json={"values": {"llm_base_url": llm.base_url, "llm_model": "fake-model"}},
        )
        assert r.status_code == 200, r.text
        llm.queue(HERO_PORTRAIT)

        r = client.post(f"/api/characters/{hero['id']}/generate-thumbnail", json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["bio_used"] is True
        assert body["prompt"] == HERO_PORTRAIT

        # the bio (not just the name) reached the model
        sent = llm.requests[-1]["messages"][-1]["content"]
        assert "jazz trumpeter" in sent and "@hero" in sent

        job = wait_job(client, body["job_id"])
        assert job["status"] == "done", job
        (prompt_id,) = fake_comfy.state.order
        graph = fake_comfy.state.prompts[prompt_id]
        text = graph["6"]["inputs"]["text"]
        # @hero was substituted with the trigger phrase; the drafted wardrobe held
        assert "zxqhero" in text and "charcoal three-piece suit" in text
    finally:
        llm.stop()


def test_character_thumbnail_bio_falls_back_when_llm_unconfigured(  # noqa: F811
    client, settings, fake_comfy
):
    hero = make_hero(client)
    client.patch(f"/api/characters/{hero['id']}", json={"bio": HERO_BIO})

    r = client.post(f"/api/characters/{hero['id']}/generate-thumbnail", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bio_used"] is False and body["prompt"] is None

    job = wait_job(client, body["job_id"])
    assert job["status"] == "done", job
    (prompt_id,) = fake_comfy.state.order
    graph = fake_comfy.state.prompts[prompt_id]
    assert graph["6"]["inputs"]["text"].startswith("portrait photograph of zxqhero woman")


def test_character_thumbnail_bio_falls_back_when_handle_dropped(  # noqa: F811
    client, settings, fake_comfy
):
    from fake_llm import FakeLLM

    hero = make_hero(client)
    client.patch(f"/api/characters/{hero['id']}", json={"bio": HERO_BIO})

    llm = FakeLLM().start()
    try:
        client.put(
            "/api/settings",
            json={"values": {"llm_base_url": llm.base_url, "llm_model": "fake-model"}},
        )
        # both the draft and the nudge retry drop @hero → LLMError → stock portrait
        llm.queue("portrait photograph of a nameless stranger in a gray hoodie")
        llm.queue("portrait photograph of somebody else entirely, navy jacket")

        r = client.post(f"/api/characters/{hero['id']}/generate-thumbnail", json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["bio_used"] is False and body["prompt"] is None
        job = wait_job(client, body["job_id"])
        assert job["status"] == "done", job
    finally:
        llm.stop()


def test_character_thumbnail_editor_bio_override(client, settings, fake_comfy):  # noqa: F811
    hero = make_hero(client)
    client.patch(f"/api/characters/{hero['id']}", json={"bio": HERO_BIO})

    # "" override: render the stock portrait even though a bio is saved —
    # unsaved editor state wins, and no LLM is consulted
    r = client.post(f"/api/characters/{hero['id']}/generate-thumbnail", json={"bio": ""})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bio_used"] is False and body["prompt"] is None
    job = wait_job(client, body["job_id"])
    assert job["status"] == "done", job


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


NOIR_LOOK = "a rain-slicked neon alley at night, teal and sodium light, wet asphalt reflections"


def test_continuity_appends_scene_look(client, fake_comfy):  # noqa: F811
    project, scene, shot = make_board(client, description="a quiet hallway")
    client.patch(f"/api/projects/{project['id']}", json={"continuity_enabled": True})
    client.patch(f"/api/scenes/{scene['id']}", json={"look": NOIR_LOOK})

    r = client.post(
        f"/api/shots/{shot['id']}/generate", json={"workflow_id": "krea2-basic", "n_takes": 1}
    )
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == "done", job
    (pid,) = fake_comfy.state.order
    text = fake_comfy.state.prompts[pid]["6"]["inputs"]["text"]
    assert text == f"a quiet hallway. Scene environment: {NOIR_LOOK}"


def test_continuity_off_leaves_prompt_alone(client, fake_comfy):  # noqa: F811
    _, scene, shot = make_board(client, description="a quiet hallway")
    client.patch(f"/api/scenes/{scene['id']}", json={"look": NOIR_LOOK})

    r = client.post(
        f"/api/shots/{shot['id']}/generate", json={"workflow_id": "krea2-basic", "n_takes": 1}
    )
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == "done", job
    (pid,) = fake_comfy.state.order
    assert fake_comfy.state.prompts[pid]["6"]["inputs"]["text"] == "a quiet hallway"


def test_continuity_skips_when_look_already_in_prompt(client, fake_comfy):  # noqa: F811
    # story-vibes boards bake the environment into each description — no doubling
    baked = f"a quiet hallway leading into {NOIR_LOOK}"
    project, scene, shot = make_board(client, description=baked)
    client.patch(f"/api/projects/{project['id']}", json={"continuity_enabled": True})
    client.patch(f"/api/scenes/{scene['id']}", json={"look": NOIR_LOOK})

    r = client.post(
        f"/api/shots/{shot['id']}/generate", json={"workflow_id": "krea2-basic", "n_takes": 1}
    )
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == "done", job
    (pid,) = fake_comfy.state.order
    assert fake_comfy.state.prompts[pid]["6"]["inputs"]["text"] == baked
