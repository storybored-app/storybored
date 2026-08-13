# OWNED-BY: engine-agent
"""Graph transforms: param application, mention parsing, LoRA injection rewiring."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from storybored.config import Settings
from storybored.engine import registry
from storybored.engine.graph import (
    added_engine_loras,
    apply_engine_lora_overrides,
    apply_model_overrides,
    apply_parameters,
    inject_characters,
    inject_style_loras,
    lora_chain,
    lora_injection_spec,
    parse_engine_loras,
    parse_engine_models,
    parse_mentions,
    parse_style_loras,
    set_filename_prefix,
    set_frame_position,
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


# -- style LoRA injection --------------------------------------------------------


def test_style_loras_land_between_stack_and_characters():
    manifest, graph = load_pack("krea2-realism")
    injection = manifest["character_injection"]
    (char_node,) = inject_characters(graph, injection, [char()])
    (style_node,) = inject_style_loras(
        graph, injection, [{"lora_name": "styles/noir.safetensors", "strength": 0.6}]
    )
    # chain: lora_7 -> style -> char -> {sage, 6}
    assert graph[style_node]["inputs"]["model"] == ["lora_7", 0]
    assert graph[style_node]["inputs"]["clip"] == ["lora_7", 1]
    assert graph[char_node]["inputs"]["model"] == [style_node, 0]
    assert graph[char_node]["inputs"]["clip"] == [style_node, 1]
    assert graph["sage"]["inputs"]["model"] == [char_node, 0]
    assert graph["6"]["inputs"]["clip"] == [char_node, 1]
    assert graph[style_node]["inputs"]["lora_name"] == "styles/noir.safetensors"
    assert graph[style_node]["inputs"]["strength_model"] == 0.6
    assert graph[style_node]["inputs"]["strength_clip"] == 0.6


def test_style_loras_without_characters():
    manifest, graph = load_pack("krea2-realism")
    s1, s2 = inject_style_loras(
        graph,
        manifest["character_injection"],
        [
            {"lora_name": "styles/noir.safetensors", "strength": 0.6},
            {"lora_name": "styles/anamorphic.safetensors", "strength": 1.0},
        ],
    )
    assert graph[s1]["inputs"]["model"] == ["lora_7", 0]
    assert graph[s2]["inputs"]["model"] == [s1, 0]
    assert graph["sage"]["inputs"]["model"] == [s2, 0]
    assert graph["6"]["inputs"]["clip"] == [s2, 1]
    # style LoRAs never touch disable_nodes — that's a character-identity concern
    assert graph["lora_3"]["inputs"]["strength_model"] == 1


def test_style_loras_noop_when_empty():
    manifest, graph = load_pack("krea2-realism")
    before = json.dumps(graph, sort_keys=True)
    assert inject_style_loras(graph, manifest["character_injection"], []) == []
    assert inject_style_loras(graph, manifest["character_injection"], [{"lora_name": ""}]) == []
    assert inject_style_loras(graph, None, [{"lora_name": "x.safetensors"}]) == []
    assert json.dumps(graph, sort_keys=True) == before


def test_parse_style_loras():
    raw = json.dumps(
        [
            {"lora_name": "a.safetensors", "strength": 0.5, "enabled": True},
            {"lora_name": "b.safetensors", "enabled": False},
            {"lora_name": "c.safetensors"},
            {"lora_name": "", "strength": 1.0},
            {"lora_name": "d.safetensors", "strength": "bad"},
            "not a dict",
        ]
    )
    assert parse_style_loras(raw) == [
        {"lora_name": "a.safetensors", "strength": 0.5},
        {"lora_name": "c.safetensors", "strength": 1.0},
        {"lora_name": "d.safetensors", "strength": 1.0},
    ]
    assert parse_style_loras("") == []
    assert parse_style_loras("not json") == []
    assert parse_style_loras('{"lora_name": "x"}') == []


# -- engine LoRA overrides -------------------------------------------------------


def test_lora_chain_order():
    _, graph = load_pack("krea2-realism")
    assert lora_chain(graph) == [f"lora_{i}" for i in range(8)]
    _, basic = load_pack("krea2-basic")
    assert lora_chain(basic) == ["lora_distill"]


def test_parse_engine_loras():
    raw = json.dumps(
        {
            "krea2-realism": [
                {"node": "lora_2", "strength": 1.5},
                {"node": "lora_6", "enabled": False},
                {"lora_name": "extra.safetensors", "strength": 0.7},
                {"lora_name": "off.safetensors", "enabled": False},
                {"strength": 1.0},  # neither node nor lora_name — dropped
            ],
            "bad-pack": "not a list",
        }
    )
    parsed = parse_engine_loras(raw)
    assert set(parsed) == {"krea2-realism"}
    entries = parsed["krea2-realism"]
    assert entries[0] == {"node": "lora_2", "strength": 1.5, "enabled": True}
    assert entries[1] == {"node": "lora_6", "enabled": False}
    assert added_engine_loras(entries) == [{"lora_name": "extra.safetensors", "strength": 0.7}]
    assert parse_engine_loras("") == {}
    assert parse_engine_loras("not json") == {}
    assert parse_engine_loras("[]") == {}


def test_apply_engine_lora_overrides():
    _, graph = load_pack("krea2-realism")
    apply_engine_lora_overrides(
        graph,
        [
            {"node": "lora_2", "strength": 1.5, "enabled": True},
            {"node": "lora_6", "enabled": False},
            {"node": "ghost_node", "strength": 1.0, "enabled": True},  # ignored
            {"node": "6", "strength": 1.0, "enabled": True},  # not a LoraLoader — ignored
        ],
    )
    assert graph["lora_2"]["inputs"]["strength_model"] == 1.5
    assert graph["lora_2"]["inputs"]["strength_clip"] == 1.5
    assert graph["lora_6"]["inputs"]["strength_model"] == 0
    assert graph["lora_6"]["inputs"]["strength_clip"] == 0
    assert graph["lora_7"]["inputs"]["strength_model"] == 0.8  # untouched
    assert "strength_model" not in graph["6"]["inputs"]


def test_added_engine_loras_splice_before_styles():
    manifest, graph = load_pack("krea2-realism")
    injection = manifest["character_injection"]
    (char_node,) = inject_characters(graph, injection, [char()])
    (style_node,) = inject_style_loras(
        graph, injection, [{"lora_name": "styles/noir.safetensors", "strength": 0.6}]
    )
    (added_node,) = inject_style_loras(
        graph,
        injection,
        [{"lora_name": "added.safetensors", "strength": 0.5}],
        id_prefix="engine_lora_",
    )
    # chain: lora_7 -> engine addition -> style -> char -> {sage, 6}
    assert added_node == "engine_lora_0"
    assert graph[added_node]["inputs"]["model"] == ["lora_7", 0]
    assert graph[style_node]["inputs"]["model"] == [added_node, 0]
    assert graph[char_node]["inputs"]["model"] == [style_node, 0]
    assert graph["sage"]["inputs"]["model"] == [char_node, 0]


# -- video LoRAs (model-only loaders) ------------------------------------------


def test_lora_injection_spec_prefers_explicit_over_character():
    manifest, _ = load_pack("minimax-h3-i2v")
    spec = lora_injection_spec(manifest)
    assert spec == {"after_node": "1", "class_type": "LoraLoaderModelOnly"}
    image_manifest, _ = load_pack("krea2-realism")
    assert lora_injection_spec(image_manifest) is image_manifest["character_injection"]


def test_model_only_lora_splice_on_video_pack():
    manifest, graph = load_pack("minimax-h3-i2v")
    injected = inject_style_loras(
        graph,
        lora_injection_spec(manifest),
        [
            {"lora_name": "mm/style_a.safetensors", "strength": 0.7},
            {"lora_name": "mm/style_b.safetensors", "strength": 1.0},
        ],
        id_prefix="engine_lora_",
    )
    assert injected == ["engine_lora_0", "engine_lora_1"]
    node = graph["engine_lora_0"]
    assert node["class_type"] == "LoraLoaderModelOnly"
    assert node["inputs"]["model"] == ["1", 0]
    assert node["inputs"]["strength_model"] == 0.7
    # model-only: no clip path, no clip strength
    assert "clip" not in node["inputs"] and "strength_clip" not in node["inputs"]
    # chain: 1 → engine_lora_0 → engine_lora_1 → sage-attention patch
    assert graph["engine_lora_1"]["inputs"]["model"] == ["engine_lora_0", 0]
    assert graph["7"]["inputs"]["model"] == ["engine_lora_1", 0]
    # the CLIP path never routed through the chain and stays untouched
    assert graph["6"]["inputs"]["clip"] == ["2", 0]


def test_engine_lora_overrides_on_model_only_node():
    graph = {
        "vl": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"lora_name": "x.safetensors", "strength_model": 1.0, "model": ["1", 0]},
        }
    }
    apply_engine_lora_overrides(graph, [{"node": "vl", "enabled": False}])
    assert graph["vl"]["inputs"]["strength_model"] == 0
    assert "strength_clip" not in graph["vl"]["inputs"]
    apply_engine_lora_overrides(graph, [{"node": "vl", "strength": 0.4, "enabled": True}])
    assert graph["vl"]["inputs"]["strength_model"] == 0.4


def test_lora_chain_includes_model_only_loaders():
    manifest, graph = load_pack("minimax-h3-i2v")
    inject_style_loras(
        graph, lora_injection_spec(manifest),
        [{"lora_name": "a.safetensors", "strength": 1.0}], id_prefix="vid_",
    )
    assert lora_chain(graph) == ["vid_0"]


# -- model slots ---------------------------------------------------------------


def test_parse_engine_models_defensive():
    assert parse_engine_models("") == {}
    assert parse_engine_models("not json") == {}
    assert parse_engine_models('["list"]') == {}
    assert parse_engine_models('{"pack": "not an object"}') == {}
    raw = '{"minimax-h3-i2v": {"unet": "pink.safetensors", "bad": 3, "blank": "  "}}'
    assert parse_engine_models(raw) == {"minimax-h3-i2v": {"unet": "pink.safetensors"}}


def test_apply_model_overrides():
    manifest, graph = load_pack("minimax-h3-i2v")
    apply_model_overrides(graph, manifest, {"unet": "pinkcherryMMH3_06Beta.safetensors"})
    assert graph["1"]["inputs"]["unet_name"] == "pinkcherryMMH3_06Beta.safetensors"
    # unknown slot keys and empty overrides are ignored
    before = json.dumps(graph, sort_keys=True)
    apply_model_overrides(graph, manifest, {"ghost": "x.safetensors", "unet": ""})
    assert json.dumps(graph, sort_keys=True) == before
    # image packs expose the UNET slot too
    manifest, graph = load_pack("krea2-realism")
    apply_model_overrides(graph, manifest, {"unet": "krea2_turbo_int8_convrot.safetensors"})
    assert graph["4"]["inputs"]["unet_name"] == "krea2_turbo_int8_convrot.safetensors"


# -- frame position ------------------------------------------------------------


def test_set_frame_position_first_is_noop():
    manifest, graph = load_pack("minimax-h3-i2v")
    before = json.dumps(graph, sort_keys=True)
    set_frame_position(graph, manifest, "first")
    assert json.dumps(graph, sort_keys=True) == before


def test_set_frame_position_last_moves_the_image_input():
    manifest, graph = load_pack("minimax-h3-i2v")
    set_frame_position(graph, manifest, "last")
    assert "first_frame" not in graph["6"]["inputs"]
    assert graph["6"]["inputs"]["last_frame"] == ["5", 0]


def test_set_frame_position_last_without_support_raises():
    manifest, graph = load_pack("krea2-basic")  # no frame_conditioning
    with pytest.raises(ValueError):
        set_frame_position(graph, manifest, "last")


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
