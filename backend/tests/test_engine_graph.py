# OWNED-BY: engine-agent
"""Graph transforms: param application, mention parsing, LoRA injection rewiring."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from storybored.config import Settings
from storybored.engine import registry
from storybored.engine.graph import (
    apply_parameters,
    inject_characters,
    parse_mentions,
    set_filename_prefix,
    substitute_mentions,
)

WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"


def load_pack(pack_id: str) -> tuple[dict, dict]:
    manifest = json.loads((WORKFLOWS / pack_id / "manifest.json").read_text())
    graph = json.loads((WORKFLOWS / pack_id / "graph.json").read_text())
    return manifest, graph


def char(handle="hero", lora="characters/hero_v1.safetensors", strength=1.0, **kw):
    return SimpleNamespace(
        handle=handle,
        lora_name=lora,
        lora_strength=strength,
        trigger=kw.get("trigger", "zxqhero"),
        class_word=kw.get("class_word", "person"),
    )


# -- parameters ----------------------------------------------------------------


def test_apply_parameters_overrides_and_defaults():
    manifest, graph = load_pack("krea2-realism")
    before = json.dumps(graph, sort_keys=True)
    out = apply_parameters(graph, manifest, {"prompt": "hello there", "width": "1024", "seed": 7})
    assert out["6"]["inputs"]["text"] == "hello there"
    assert out["5"]["inputs"]["width"] == 1024  # coerced int
    assert out["5"]["inputs"]["height"] == 1152  # manifest default applied
    assert out["3"]["inputs"]["seed"] == 7
    assert out["3"]["inputs"]["steps"] == 8
    # deep copy: source graph untouched
    assert json.dumps(graph, sort_keys=True) == before


def test_apply_parameters_unknown_node_raises():
    manifest = {"parameters": [{"key": "x", "type": "int", "node": "999", "input": "y"}]}
    with pytest.raises(ValueError, match="unknown graph node '999'"):
        apply_parameters({"1": {"inputs": {}}}, manifest, {"x": 1})


# -- mentions --------------------------------------------------------------------


def test_parse_mentions_ordered_unique():
    text = "WIDE: @hero walks past @rival, then @hero turns. email a@b not a handle-start"
    assert parse_mentions(text) == ["hero", "rival", "b"]
    assert parse_mentions("") == []


def test_substitute_mentions():
    chars = {
        "hero": char(trigger="zxqhero", class_word="woman"),
        "rival": char(handle="rival", trigger="vvkrival", class_word="man"),
    }
    out = substitute_mentions("@hero glares at @rival near @nobody", chars)
    assert out == "zxqhero woman glares at vvkrival man near @nobody"


# -- character injection -----------------------------------------------------------


def test_single_character_injection_rewires_exact_links():
    manifest, graph = load_pack("krea2-realism")
    injection = manifest["character_injection"]
    c = char(strength=0.9)
    injected = inject_characters(graph, injection, [c])
    assert len(injected) == 1
    nid = injected[0]
    # new node hangs off after_node's outputs 0/1
    assert graph[nid]["class_type"] == "LoraLoader"
    assert graph[nid]["inputs"]["model"] == ["lora_7", 0]
    assert graph[nid]["inputs"]["clip"] == ["lora_7", 1]
    assert graph[nid]["inputs"]["lora_name"] == "characters/hero_v1.safetensors"
    assert graph[nid]["inputs"]["strength_model"] == 0.9
    assert graph[nid]["inputs"]["strength_clip"] == 0.9
    # every other referent of lora_7 outputs is rewired to the new node
    assert graph["sage"]["inputs"]["model"] == [nid, 0]
    assert graph["6"]["inputs"]["clip"] == [nid, 1]
    # unrelated links untouched
    assert graph["3"]["inputs"]["model"] == ["sage", 0]
    assert graph["lora_7"]["inputs"]["model"] == ["lora_6", 0]
    # disable_nodes zeroed
    assert graph["lora_3"]["inputs"]["strength_model"] == 0
    assert graph["lora_3"]["inputs"]["strength_clip"] == 0
    assert graph["lora_4"]["inputs"]["strength_model"] == 0
    assert graph["lora_4"]["inputs"]["strength_clip"] == 0


def test_multi_character_chain():
    manifest, graph = load_pack("krea2-realism")
    c1 = char()
    c2 = char(handle="rival", lora="characters/rival_v1.safetensors", strength=0.7)
    n1, n2 = inject_characters(graph, manifest["character_injection"], [c1, c2])
    # chain: lora_7 -> n1 -> n2 -> {sage, 6}
    assert graph[n1]["inputs"]["model"] == ["lora_7", 0]
    assert graph[n1]["inputs"]["clip"] == ["lora_7", 1]
    assert graph[n2]["inputs"]["model"] == [n1, 0]
    assert graph[n2]["inputs"]["clip"] == [n1, 1]
    assert graph["sage"]["inputs"]["model"] == [n2, 0]
    assert graph["6"]["inputs"]["clip"] == [n2, 1]
    assert graph[n2]["inputs"]["lora_name"] == "characters/rival_v1.safetensors"
    assert graph[n2]["inputs"]["strength_model"] == 0.7


def test_injection_noop_without_characters():
    manifest, graph = load_pack("krea2-realism")
    before = json.dumps(graph, sort_keys=True)
    assert inject_characters(graph, manifest["character_injection"], []) == []
    # nothing changed — including disable_nodes strengths
    assert json.dumps(graph, sort_keys=True) == before
    assert graph["lora_3"]["inputs"]["strength_model"] == 1


def test_character_without_lora_is_skipped():
    manifest, graph = load_pack("krea2-realism")
    assert inject_characters(graph, manifest["character_injection"], [char(lora="")]) == []
    assert graph["lora_3"]["inputs"]["strength_model"] == 1  # not zeroed


def test_injection_on_basic_pack():
    manifest, graph = load_pack("krea2-basic")
    (nid,) = inject_characters(graph, manifest["character_injection"], [char()])
    assert graph[nid]["inputs"]["model"] == ["lora_distill", 0]
    assert graph["sage"]["inputs"]["model"] == [nid, 0]
    assert graph["6"]["inputs"]["clip"] == [nid, 1]


def test_set_filename_prefix():
    _, graph = load_pack("krea2-realism")
    set_filename_prefix(graph, "storybored/take_42")
    assert graph["9"]["inputs"]["filename_prefix"] == "storybored/take_42"


# -- pack sanity + registry scan -----------------------------------------------


def test_basic_pack_has_single_lora_chain():
    _, graph = load_pack("krea2-basic")
    lora_nodes = [k for k, v in graph.items() if v["class_type"] == "LoraLoader"]
    assert lora_nodes == ["lora_distill"]
    assert graph["lora_distill"]["inputs"]["strength_model"] == 1
    assert graph["lora_distill"]["inputs"]["model"] == ["4", 0]
    assert graph["lora_distill"]["inputs"]["clip"] == ["11", 0]
    assert graph["sage"]["inputs"]["model"] == ["lora_distill", 0]
    assert graph["6"]["inputs"]["clip"] == ["lora_distill", 1]


def test_registry_scans_repo_and_data_dir(tmp_path):
    settings = Settings(_env_file=None, data_dir=str(tmp_path / "data"))
    packs = registry.load_packs(settings)
    assert {"krea2-basic", "krea2-realism"} <= set(packs)
    assert registry.default_workflow_id(packs, "image") == "krea2-basic"

    # user pack in DATA_DIR/workflows is discovered too
    user_pack = tmp_path / "data" / "workflows" / "my-pack"
    user_pack.mkdir(parents=True)
    (user_pack / "manifest.json").write_text(
        json.dumps({"id": "my-pack", "kind": "image", "parameters": []})
    )
    (user_pack / "graph.json").write_text("{}")
    packs = registry.load_packs(settings)
    assert "my-pack" in packs
    assert packs["my-pack"].load_graph() == {}
