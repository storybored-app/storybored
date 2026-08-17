# OWNED-BY: engine-agent
"""The in-app import wizard: graph analysis heuristics, pack creation and
removal. The three shipped packs are the regression anchor — analysis must
re-derive each one's own manifest mappings from its graph alone."""

import json
from pathlib import Path

import pytest
from fake_comfy import fake_comfy  # noqa: F401 - pytest fixture

REPO_WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"


def load_pack_files(pack_id: str) -> tuple[dict, dict]:
    d = REPO_WORKFLOWS / pack_id
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    graph = json.loads((d / "graph.json").read_text(encoding="utf-8"))
    return manifest, graph


def analyze(client, graph: dict) -> dict:
    r = client.post("/api/workflows/analyze", json={"graph": graph})
    assert r.status_code == 200, r.text
    return r.json()


def use_engine(client, fake) -> None:
    r = client.put("/api/settings", json={"values": {"comfyui_url": fake.url}})
    assert r.status_code == 200, r.text


def param(manifest: dict, key: str) -> dict:
    return next(p for p in manifest["parameters"] if p["key"] == key)


# -- analysis heuristics vs the shipped packs ---------------------------------


@pytest.mark.parametrize("pack_id", ["krea2-basic", "krea2-realism", "minimax-h3-i2v"])
def test_analyze_rederives_shipped_manifest(client, pack_id):
    """Each shipped graph must analyze back to its own manifest's mappings."""
    manifest, graph = load_pack_files(pack_id)
    a = analyze(client, graph)
    roles = a["roles"]

    assert a["kind"] == manifest["kind"]
    assert a["node_count"] == len(graph)

    p = param(manifest, "prompt")
    assert roles["prompt"]["suggested"]["node"] == p["node"]
    assert roles["prompt"]["suggested"]["input"] == p["input"]
    assert roles["prompt"]["suggested"]["confidence"] == "high"

    s = param(manifest, "seed")
    assert roles["seed"]["suggested"]["node"] == s["node"]
    assert roles["seed"]["suggested"]["input"] == s["input"]

    assert roles["size"]["suggested"]["node"] == param(manifest, "width")["node"]
    assert roles["output"]["suggested"]["node"] == manifest["output_node"]

    slots = [(s["node"], s["input"]) for s in a["model_slots"]]
    assert slots == [(s["node"], s["input"]) for s in manifest["model_slots"]]

    seam = roles["seam"]["suggested"]
    injection = manifest.get("character_injection") or manifest.get("lora_injection")
    assert seam["after_node"] == injection["after_node"]
    expected_field = (
        "character_injection" if "character_injection" in manifest else "lora_injection"
    )
    assert seam["field"] == expected_field
    if "class_type" in injection:
        assert seam["class_type"] == injection["class_type"]

    if manifest["kind"] == "video":
        first = param(manifest, "first_frame")
        assert roles["image"]["suggested"]["node"] == first["node"]
        assert roles["image"]["suggested"]["input"] == first["input"]
        length = param(manifest, "length")
        assert roles["length"]["suggested"]["node"] == length["node"]
        assert roles["length"]["suggested"]["input"] == length["input"]
        assert a["frame_conditioning"] == manifest["frame_conditioning"]

    # required_models derivation must agree with each manifest (validate-pack
    # keeps the shipped packs drift-free, so equality is exact)
    assert a["required_models"] == manifest["required_models"]
    assert a["warnings"] == []


def test_analyze_two_text_encodes_reports_roles(client):
    """The docs' worked SDXL example: positive/negative resolved from the
    sampler's conditioning links; both encodes surface as candidates."""
    graph = {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "my_model.safetensors"}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "PROMPT HERE", "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "blurry, low quality", "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": 1216, "height": 832, "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": 0, "steps": 25, "model": ["1", 0],
                         "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "storybored", "images": ["6", 0]}},
    }
    a = analyze(client, graph)
    assert a["kind"] == "image"
    roles = {c["node"]: c["role"] for c in a["roles"]["prompt"]["candidates"]}
    assert roles == {"2": "positive", "3": "negative"}
    assert a["roles"]["prompt"]["suggested"] == {
        "node": "2", "input": "text", "confidence": "high",
    }
    # no LoRA chain → the seam falls back to the checkpoint loader
    assert a["roles"]["seam"]["suggested"]["after_node"] == "1"
    assert a["model_slots"][0]["input"] == "ckpt_name"
    assert a["required_models"] == {
        "CheckpointLoaderSimple.ckpt_name": ["my_model.safetensors"]
    }


def test_analyze_surfaces_ambiguity_as_warnings(client):
    """No save node + an unresolvable prompt pair → warnings, not errors."""
    graph = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "one"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "two"}},
    }
    a = analyze(client, graph)
    assert len(a["roles"]["prompt"]["candidates"]) == 2
    assert a["roles"]["prompt"]["suggested"]["confidence"] == "low"
    assert a["roles"]["output"]["suggested"] is None
    assert any("save node" in w for w in a["warnings"])
    assert any("text inputs" in w for w in a["warnings"])


def test_analyze_rejects_ui_format(client):
    """Editor-format exports get the how-to-export message, wording intact."""
    ui = {"nodes": [{"id": 1, "type": "KSampler"}], "links": [], "version": 0.4}
    r = client.post("/api/workflows/analyze", json={"graph": ui})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert 'Save (API Format)' in detail
    assert 'Enable Dev mode Options' in detail


def test_analyze_rejects_non_graph_json(client):
    r = client.post("/api/workflows/analyze", json={"graph": {"a": "not a node"}})
    assert r.status_code == 400
    assert "Save (API Format)" in r.json()["detail"]
    r = client.post("/api/workflows/analyze", json={"graph": []})
    assert r.status_code == 400


# -- import --------------------------------------------------------------------


def import_body(pack_id: str = "my-look", graph: dict | None = None) -> dict:
    """A confirmed wizard draft built from the krea2-basic graph."""
    _, base_graph = load_pack_files("krea2-basic")
    return {
        "id": pack_id,
        "name": "My look",
        "kind": "image",
        "description": "Imported in a test.",
        "graph": graph if graph is not None else base_graph,
        "parameters": [
            {"key": "prompt", "label": "Prompt", "type": "prompt", "node": "6", "input": "text"},
            {"key": "seed", "type": "seed", "node": "3", "input": "seed"},
            {"key": "width", "type": "int", "node": "5", "input": "width", "default": 1728},
            {"key": "height", "type": "int", "node": "5", "input": "height", "default": 1152},
        ],
        "output_node": "9",
        "character_injection": {"after_node": "lora_distill"},
        "model_slots": [
            {"key": "unet", "label": "Base model", "node": "4", "input": "unet_name"}
        ],
    }


def test_import_roundtrip(client, settings, fake_comfy):  # noqa: F811
    use_engine(client, fake_comfy)
    r = client.post("/api/workflows/import", json=import_body())
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["id"] == "my-look"
    assert created["removable"] is True
    # models + node classes match the krea2 graph the fake already allows
    assert created["available"] is True
    assert created["missing_models"] == []

    pack_dir = settings.data_path / "workflows" / "my-look"
    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "my-look"
    # required_models was derived from the graph, not trusted from the client
    assert manifest["required_models"]["UNETLoader.unet_name"] == [
        "krea2_raw_fp8_scaled.safetensors"
    ]
    assert (pack_dir / "graph.json").is_file()

    # the registry picks the pack up like any DATA_DIR pack
    rows = {w["id"]: w for w in client.get("/api/workflows").json()}
    assert rows["my-look"]["available"] is True
    assert rows["my-look"]["removable"] is True
    assert rows["my-look"]["supports_characters"] is True
    # shipped packs are not removable
    assert rows["krea2-basic"]["removable"] is False


def test_import_reports_missing_models(client, fake_comfy):  # noqa: F811
    use_engine(client, fake_comfy)
    _, graph = load_pack_files("krea2-basic")
    graph = json.loads(json.dumps(graph))
    graph["4"]["inputs"]["unet_name"] = "not_on_the_engine.safetensors"
    r = client.post("/api/workflows/import", json=import_body("needy", graph))
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["available"] is False
    assert created["missing_models"] == ["not_on_the_engine.safetensors"]


def test_import_id_collisions(client, fake_comfy):  # noqa: F811
    use_engine(client, fake_comfy)
    # colliding with a shipped pack is refused
    r = client.post("/api/workflows/import", json=import_body("krea2-basic"))
    assert r.status_code == 409
    # importing twice under the same id is refused
    assert client.post("/api/workflows/import", json=import_body("dupe")).status_code == 201
    r = client.post("/api/workflows/import", json=import_body("dupe"))
    assert r.status_code == 409


@pytest.mark.parametrize(
    "bad_id", ["../evil", "..", "a/b", "UPPER", "spa ce", "", "-leading", ".hidden"]
)
def test_import_rejects_unsafe_ids(client, settings, bad_id):
    r = client.post("/api/workflows/import", json=import_body(bad_id))
    assert r.status_code == 400
    assert "slug" in r.json()["detail"]
    # nothing may land on disk — not under workflows, not beside it
    workflows_dir = settings.data_path / "workflows"
    assert not workflows_dir.is_dir() or list(workflows_dir.glob("*/manifest.json")) == []
    assert not (settings.data_path / "evil").exists()


def test_import_runs_validate_pack_checks(client, settings):
    """A draft referencing a node the graph doesn't have fails validation and
    leaves nothing behind."""
    body = import_body("broken")
    body["output_node"] = "999"
    r = client.post("/api/workflows/import", json=body)
    assert r.status_code == 400
    assert "999" in r.json()["detail"]
    assert not (settings.data_path / "workflows" / "broken").exists()
    # staging leftovers are cleaned up too
    workflows_dir = settings.data_path / "workflows"
    assert not workflows_dir.is_dir() or list(workflows_dir.iterdir()) == []


def test_import_rejects_ui_format_graph(client):
    body = import_body("ui-format")
    body["graph"] = {"nodes": [], "links": []}
    r = client.post("/api/workflows/import", json=body)
    assert r.status_code == 400
    assert "Save (API Format)" in r.json()["detail"]


# -- delete --------------------------------------------------------------------


def test_delete_imported_pack(client, settings, fake_comfy):  # noqa: F811
    use_engine(client, fake_comfy)
    assert client.post("/api/workflows/import", json=import_body("goner")).status_code == 201
    assert (settings.data_path / "workflows" / "goner").is_dir()

    r = client.delete("/api/workflows/goner")
    assert r.status_code == 204
    assert not (settings.data_path / "workflows" / "goner").exists()
    assert "goner" not in {w["id"] for w in client.get("/api/workflows").json()}


def test_delete_builtin_pack_forbidden(client):
    r = client.delete("/api/workflows/krea2-basic")
    assert r.status_code == 403
    assert (REPO_WORKFLOWS / "krea2-basic" / "manifest.json").is_file()


def test_delete_unknown_pack_404(client):
    assert client.delete("/api/workflows/nope").status_code == 404
