# OWNED-BY: engine-agent
"""`python -m storybored validate-pack` — the offline pack linter."""

import json
from pathlib import Path

from storybored.engine.validate import derive_required_models, main, validate_pack

REPO_WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"


def write_pack(tmp_path, manifest: dict, graph: dict, name: str | None = None) -> Path:
    pack_dir = tmp_path / (name or manifest.get("id", "pack"))
    pack_dir.mkdir(parents=True)
    (pack_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (pack_dir / "graph.json").write_text(json.dumps(graph))
    return pack_dir


GOOD_GRAPH = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "my.safetensors"}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "x", "clip": ["1", 1]}},
    "3": {
        "class_type": "KSampler",
        "inputs": {"seed": 0, "model": ["1", 0], "positive": ["2", 0]},
    },
    "4": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["1", 2]}},
    "5": {"class_type": "SaveImage", "inputs": {"filename_prefix": "sb", "images": ["4", 0]}},
}

GOOD_MANIFEST = {
    "id": "good-pack",
    "name": "Good pack",
    "kind": "image",
    "graph": "graph.json",
    "parameters": [
        {"key": "prompt", "type": "prompt", "node": "2", "input": "text"},
        {"key": "seed", "type": "seed", "node": "3", "input": "seed"},
    ],
    "output_node": "5",
    "character_injection": {"after_node": "1"},
    "required_models": {"CheckpointLoaderSimple.ckpt_name": ["my.safetensors"]},
}


def test_shipped_packs_validate_clean():
    for pack_dir in sorted(p for p in REPO_WORKFLOWS.iterdir() if p.is_dir()):
        report = validate_pack(pack_dir)
        assert report.ok, (pack_dir.name, report.errors)
        assert not report.has_drift, (pack_dir.name, report.drift_missing, report.drift_extra)


def test_good_pack_passes(tmp_path):
    pack_dir = write_pack(tmp_path, GOOD_MANIFEST, GOOD_GRAPH)
    report = validate_pack(pack_dir)
    assert report.ok, report.errors
    assert not report.has_drift
    assert main([str(pack_dir)]) == 0


def test_broken_refs_and_types_fail(tmp_path):
    manifest = json.loads(json.dumps(GOOD_MANIFEST))
    manifest["id"] = "broken"
    manifest["output_node"] = "99"  # not in the graph
    manifest["parameters"].append(
        {"key": "zoom", "type": "percentage", "node": "42", "input": "zoom"}
    )
    manifest["character_injection"] = {"after_node": "nope", "disable_nodes": ["also_nope"]}
    manifest["model_slots"] = [{"key": "base", "node": "77", "input": "ckpt_name"}]
    pack_dir = write_pack(tmp_path, manifest, GOOD_GRAPH)

    report = validate_pack(pack_dir)
    text = "\n".join(report.errors)
    assert "output_node" in text and "'99'" in text
    assert "unknown type 'percentage'" in text
    assert "'42'" in text  # parameter node ref
    assert "character_injection.after_node" in text
    assert "disable_nodes" in text
    assert "model slot 'base'" in text
    assert main([str(pack_dir)]) == 1


def test_id_must_match_folder(tmp_path):
    pack_dir = write_pack(tmp_path, GOOD_MANIFEST, GOOD_GRAPH, name="other-folder")
    report = validate_pack(pack_dir)
    assert any("does not match folder name" in e for e in report.errors)


def test_editor_format_graph_rejected(tmp_path):
    manifest = dict(GOOD_MANIFEST)
    pack_dir = write_pack(tmp_path, manifest, {}, name="good-pack")
    (pack_dir / "graph.json").write_text(json.dumps({"nodes": [], "links": []}))
    report = validate_pack(pack_dir)
    assert any("API-format" in e for e in report.errors)


def test_required_models_drift_detected_and_written(tmp_path, capsys):
    graph = json.loads(json.dumps(GOOD_GRAPH))
    graph["6"] = {
        "class_type": "LoraLoader",
        "inputs": {
            "lora_name": "style.safetensors",
            "strength_model": 1,
            "strength_clip": 1,
            "model": ["1", 0],
            "clip": ["1", 1],
        },
    }
    manifest = json.loads(json.dumps(GOOD_MANIFEST))
    manifest["required_models"] = {
        "CheckpointLoaderSimple.ckpt_name": ["stale_old_name.safetensors"]
    }
    pack_dir = write_pack(tmp_path, manifest, graph)

    report = validate_pack(pack_dir)
    assert report.ok  # drift is not a shape error…
    assert report.drift_missing == {
        "CheckpointLoaderSimple.ckpt_name": ["my.safetensors"],
        "LoraLoader.lora_name": ["style.safetensors"],
    }
    assert report.drift_extra == {
        "CheckpointLoaderSimple.ckpt_name": ["stale_old_name.safetensors"]
    }
    assert main([str(pack_dir)]) == 1  # …but it fails CI

    # --write regenerates required_models from the graph and goes green
    assert main([str(pack_dir), "--write"]) == 0
    updated = json.loads((pack_dir / "manifest.json").read_text())
    assert updated["required_models"] == {
        "CheckpointLoaderSimple.ckpt_name": ["my.safetensors"],
        "LoraLoader.lora_name": ["style.safetensors"],
    }
    assert not validate_pack(pack_dir).has_drift
    out = capsys.readouterr().out
    assert "DRIFT" in out and "wrote required_models" in out


def test_unknown_loader_class_warns(tmp_path):
    graph = json.loads(json.dumps(GOOD_GRAPH))
    graph["7"] = {
        "class_type": "MysteryModelLoader",
        "inputs": {"mystery_name": "weights.safetensors"},
    }
    pack_dir = write_pack(tmp_path, GOOD_MANIFEST, graph, name="good-pack")
    report = validate_pack(pack_dir)
    assert report.ok
    assert any("MysteryModelLoader" in w and "required_models" in w for w in report.warnings)


def test_missing_dir_exits_2(tmp_path):
    assert main([str(tmp_path / "nope")]) == 2


def test_derive_required_models_skips_links():
    graph = {
        "1": {"class_type": "LoraLoader", "inputs": {"lora_name": ["0", 0]}},  # link
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": "vae.safetensors"}},
    }
    assert derive_required_models(graph) == {"VAELoader.vae_name": ["vae.safetensors"]}
