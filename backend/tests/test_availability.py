# OWNED-BY: engine-agent
"""Effective availability: engine_models slot swaps and engine_loras toggles
change what a render actually loads, so they must change what availability
checks — plus node-class validation and the /object_info cache flushes."""

import json

from fake_comfy import fake_comfy  # noqa: F401 - pytest fixture

from storybored.engine.comfy_client import clear_object_info_cache


def use_engine(client, fake):
    r = client.put("/api/settings", json={"values": {"comfyui_url": fake.url}})
    assert r.status_code == 200, r.text


def workflows(client, refresh=True):
    url = "/api/workflows?refresh=true" if refresh else "/api/workflows"
    return {w["id"]: w for w in client.get(url).json()}


def make_shot(client):
    p = client.post("/api/projects", json={"title": "Avail"}).json()
    sc = client.post(f"/api/projects/{p['id']}/scenes", json={"title": "S1"}).json()
    return client.post(f"/api/scenes/{sc['id']}/shots", json={"description": "a door"}).json()


# -- override-aware availability (the effective model set) ---------------------


def test_swapped_model_slot_restores_availability(client, fake_comfy):  # noqa: F811
    use_engine(client, fake_comfy)
    # the baked UNET is gone from the engine, but the user's finetune is there
    fake_comfy.state.models["UNETLoader.unet_name"] = ["my_finetune.safetensors"]

    rows = workflows(client)
    assert rows["krea2-basic"]["available"] is False
    assert "krea2_raw_fp8_scaled.safetensors" in rows["krea2-basic"]["missing_models"]

    r = client.put(
        "/api/settings",
        json={
            "values": {
                "engine_models": json.dumps(
                    {"krea2-basic": {"unet": "my_finetune.safetensors"}}
                )
            }
        },
    )
    assert r.status_code == 200, r.text
    rows = workflows(client)
    # the override replaces the baked file in the required set → renderable
    assert rows["krea2-basic"]["available"] is True
    assert rows["krea2-basic"]["missing_models"] == []
    # other packs still require the baked UNET they'd actually load
    assert rows["krea2-realism"]["available"] is False


def test_missing_slot_override_still_flags(client, fake_comfy):  # noqa: F811
    """An override pointing at a file the engine doesn't have must flag it."""
    use_engine(client, fake_comfy)
    client.put(
        "/api/settings",
        json={
            "values": {
                "engine_models": json.dumps({"krea2-basic": {"unet": "gone.safetensors"}})
            }
        },
    )
    rows = workflows(client)
    assert rows["krea2-basic"]["available"] is False
    assert "gone.safetensors" in rows["krea2-basic"]["missing_models"]
    # the baked file is NOT reported missing — the render wouldn't load it
    assert "krea2_raw_fp8_scaled.safetensors" not in rows["krea2-basic"]["missing_models"]


def test_disabled_baked_lora_drops_out_of_required_set(client, fake_comfy):  # noqa: F811
    use_engine(client, fake_comfy)
    enum = fake_comfy.state.models["LoraLoader.lora_name"]
    enum.remove("lenovo_krea2.safetensors")

    rows = workflows(client)
    assert rows["krea2-realism"]["available"] is False
    assert rows["krea2-realism"]["missing_models"] == ["lenovo_krea2.safetensors"]

    # lora_4 is the loader baked with lenovo_krea2 — toggled off, it renders
    # at strength 0, so the file is no longer needed
    r = client.put(
        "/api/settings",
        json={
            "values": {
                "engine_loras": json.dumps(
                    {"krea2-realism": [{"node": "lora_4", "enabled": False}]}
                )
            }
        },
    )
    assert r.status_code == 200, r.text
    rows = workflows(client)
    assert rows["krea2-realism"]["available"] is True
    assert rows["krea2-realism"]["missing_models"] == []


def test_generate_succeeds_with_swapped_model(client, fake_comfy):  # noqa: F811
    """The blocking bug: a user who swapped their own UNET must not get a 409."""
    use_engine(client, fake_comfy)
    shot = make_shot(client)
    fake_comfy.state.models["UNETLoader.unet_name"] = ["my_finetune.safetensors"]
    clear_object_info_cache()

    r = client.post(f"/api/shots/{shot['id']}/generate", json={"workflow_id": "krea2-basic"})
    assert r.status_code == 409  # baked model really is gone

    client.put(
        "/api/settings",
        json={
            "values": {
                "engine_models": json.dumps(
                    {"krea2-basic": {"unet": "my_finetune.safetensors"}}
                )
            }
        },
    )
    r = client.post(f"/api/shots/{shot['id']}/generate", json={"workflow_id": "krea2-basic"})
    assert r.status_code == 200, r.text


# -- node-class validation -----------------------------------------------------


def test_missing_custom_node_flags_pack(client, fake_comfy):  # noqa: F811
    use_engine(client, fake_comfy)
    fake_comfy.state.node_classes.discard("PathchSageAttentionKJ")

    rows = workflows(client)
    assert rows["krea2-basic"]["available"] is False
    assert rows["krea2-basic"]["missing_nodes"] == ["PathchSageAttentionKJ"]
    assert rows["krea2-basic"]["missing_models"] == []  # models are all fine
    # the video pack doesn't use that class → untouched
    assert rows["minimax-h3-i2v"]["available"] is True
    assert rows["minimax-h3-i2v"]["missing_nodes"] == []

    shot = make_shot(client)
    r = client.post(f"/api/shots/{shot['id']}/generate", json={"workflow_id": "krea2-basic"})
    assert r.status_code == 409
    assert "missing custom nodes" in r.json()["detail"]
    assert "PathchSageAttentionKJ" in r.json()["detail"]


def test_manifest_required_nodes_extras_checked(client, fake_comfy, settings):  # noqa: F811
    """A pack may declare extra classes beyond what its graph references."""
    use_engine(client, fake_comfy)
    pack_dir = settings.data_path / "workflows" / "extra-nodes"
    pack_dir.mkdir(parents=True)
    (pack_dir / "graph.json").write_text(
        json.dumps(
            {
                "1": {"class_type": "KSampler", "inputs": {}},
                "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}},
            }
        )
    )
    (pack_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "extra-nodes",
                "name": "Extra nodes",
                "kind": "image",
                "graph": "graph.json",
                "parameters": [],
                "output_node": "2",
                "required_nodes": ["SomeRuntimeOnlyClass"],
            }
        )
    )
    rows = workflows(client)
    assert rows["extra-nodes"]["available"] is False
    assert rows["extra-nodes"]["missing_nodes"] == ["SomeRuntimeOnlyClass"]


# -- /object_info cache flushes ------------------------------------------------


def test_refresh_param_flushes_object_info_cache(client, fake_comfy):  # noqa: F811
    use_engine(client, fake_comfy)
    assert workflows(client, refresh=False)["krea2-basic"]["available"] is True

    fake_comfy.state.models["UNETLoader.unet_name"] = []
    # without refresh the 60s cache still says available
    assert workflows(client, refresh=False)["krea2-basic"]["available"] is True
    # ?refresh=true drops the cache and asks the engine again
    assert workflows(client)["krea2-basic"]["available"] is False


def test_comfyui_url_change_flushes_object_info_cache(client, fake_comfy):  # noqa: F811
    use_engine(client, fake_comfy)
    assert workflows(client, refresh=False)["krea2-basic"]["available"] is True

    fake_comfy.state.models["UNETLoader.unet_name"] = []
    # re-PUT the engine URL: the cache must flush even though the value is
    # unchanged in shape — availability now reflects the engine's real state
    use_engine(client, fake_comfy)
    assert workflows(client, refresh=False)["krea2-basic"]["available"] is False


# -- license notes -------------------------------------------------------------


def test_license_note_surfaced_per_pack(client, fake_comfy):  # noqa: F811
    """Packs with real-world license caveats carry a license_note in the
    registry payload; clean Apache packs carry an empty one."""
    use_engine(client, fake_comfy)
    rows = workflows(client)
    # territory exclusion + Blackwell-only default file
    minimax = rows["minimax-h3-i2v"]["license_note"]
    assert "United States" in minimax and "Blackwell" in minimax
    # community license: revenue cap + revocability disclosed
    assert "$1M" in rows["krea2-basic"]["license_note"]
    assert "revocable" in rows["krea2-realism"]["license_note"]
    # Apache-2.0 packs have nothing to disclose
    for pack_id in ("z-image-turbo", "qwen-image-2512", "wan22-ti2v-5b", "wan22-i2v-14b"):
        assert rows[pack_id]["license_note"] == ""
