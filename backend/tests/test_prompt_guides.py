# OWNED-BY: llm-agent
"""Per-engine prompt guides (manifest ``prompt_guide``).

Covers: lenient shape validation at pack load, the shipped packs' guides,
and that every LLM prompt-assembly path (Enhance, generate-motion, script
breakdown / story-vibes) injects the guide of the engine that will actually
render — and never another engine's.
"""

import json
import logging

import pytest
from fake_llm import FakeLLM

from storybored.engine.registry import load_packs
from storybored.llm.breakdown import GUIDE_LEAD_IN, build_system_prompt
from storybored.llm.enhance import system_prompt as enhance_system_prompt
from storybored.llm.guides import guide_block
from storybored.llm.motion import system_prompt as motion_system_prompt

# --------------------------------------------------------------- helpers


def write_pack(settings, pack_id: str, kind: str = "image", guide=None) -> None:
    """Drop a minimal user pack into DATA_DIR/workflows."""
    d = settings.data_path / "workflows" / pack_id
    d.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "id": pack_id,
        "name": pack_id.upper(),
        "kind": kind,
        "graph": "graph.json",
        "parameters": [],
        "output_node": "1",
    }
    if guide is not None:
        manifest["prompt_guide"] = guide
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (d / "graph.json").write_text("{}", encoding="utf-8")


GUIDE_A = {"style": "IMG-A-STYLE flowing prose.", "examples": ["IMG-A example one."]}
GUIDE_B = {"style": "IMG-B-STYLE terse lines.", "examples": []}
GUIDE_V = {"style": "VID-A-STYLE motion prose with an Audio line.", "examples": ["VID-A example."]}


# ------------------------------------------------- validation at pack load


def test_good_guide_is_normalized(settings):
    write_pack(
        settings,
        "img-a",
        guide={"style": "  padded style  ", "examples": ["  one  ", "two"]},
    )
    guide = load_packs(settings)["img-a"].manifest["prompt_guide"]
    assert guide == {"style": "padded style", "examples": ["one", "two"]}


def test_examples_capped_at_three(settings, caplog):
    write_pack(
        settings, "img-a", guide={"style": "s", "examples": ["1", "2", "3", "4", "5"]}
    )
    with caplog.at_level(logging.WARNING, logger="storybored.engine"):
        guide = load_packs(settings)["img-a"].manifest["prompt_guide"]
    assert guide["examples"] == ["1", "2", "3"]
    assert "keeping the first 3" in caplog.text


@pytest.mark.parametrize(
    "bad",
    [
        "just a string",
        ["style", "in", "a", "list"],
        {"examples": ["no style at all"]},
        {"style": ""},
        {"style": 42},
    ],
)
def test_malformed_guide_is_dropped_not_fatal(settings, caplog, bad):
    write_pack(settings, "img-a", guide=bad)
    with caplog.at_level(logging.WARNING, logger="storybored.engine"):
        packs = load_packs(settings)
    assert "img-a" in packs  # the pack itself still loads
    assert "prompt_guide" not in packs["img-a"].manifest
    assert "malformed prompt_guide" in caplog.text


def test_non_list_examples_ignored_style_kept(settings, caplog):
    write_pack(settings, "img-a", guide={"style": "s", "examples": "not a list"})
    with caplog.at_level(logging.WARNING, logger="storybored.engine"):
        guide = load_packs(settings)["img-a"].manifest["prompt_guide"]
    assert guide == {"style": "s", "examples": []}
    assert "must be a list" in caplog.text


def test_non_string_examples_filtered(settings):
    write_pack(settings, "img-a", guide={"style": "s", "examples": ["ok", 7, "", None]})
    guide = load_packs(settings)["img-a"].manifest["prompt_guide"]
    assert guide["examples"] == ["ok"]


# --------------------------------------------------------- shipped packs


def test_shipped_manifests_carry_valid_guides(settings):
    packs = load_packs(settings)
    for pack_id in ("krea2-basic", "krea2-realism", "minimax-h3-i2v"):
        guide = packs[pack_id].manifest.get("prompt_guide")
        assert guide, f"{pack_id} lost its prompt_guide"
        assert guide["style"].strip()
        assert 0 < len(guide["examples"]) <= 3
    # the video guide teaches the house "Audio:" convention
    assert "Audio:" in packs["minimax-h3-i2v"].manifest["prompt_guide"]["style"]


# ------------------------------------------------ system-prompt assembly


def test_guide_block_is_delimited_and_compact():
    block = guide_block({"engine": "My Engine", "style": "S.", "examples": ["E1", "E2"]})
    assert block.startswith("=== ACTIVE RENDER ENGINE: My Engine ===")
    assert block.endswith("=== END ACTIVE RENDER ENGINE ===")
    assert "S." in block and "1. E1" in block and "2. E2" in block
    assert guide_block(None) == ""


def test_pure_builders_append_guide_only_when_given():
    guide = {"engine": "X", "style": "X-STYLE", "examples": []}
    assert "X-STYLE" in enhance_system_prompt(guide)
    assert "ACTIVE RENDER ENGINE" not in enhance_system_prompt(None)
    assert "X-STYLE" in motion_system_prompt(guide)
    assert "ACTIVE RENDER ENGINE" not in motion_system_prompt(None)
    for mode in ("script", "vibes"):
        with_guide = build_system_prompt(["ava"], mode, guide)
        assert "X-STYLE" in with_guide and GUIDE_LEAD_IN in with_guide
        assert "ACTIVE RENDER ENGINE" not in build_system_prompt(["ava"], mode)


# ------------------------------------------------------- endpoint wiring


@pytest.fixture
def llm():
    server = FakeLLM().start()
    yield server
    server.stop()


@pytest.fixture
def board(client, settings, llm):
    """A shot to enhance, three guide-carrying user packs, LLM configured."""
    write_pack(settings, "img-a", guide=GUIDE_A)
    write_pack(settings, "img-b", guide=GUIDE_B)
    write_pack(settings, "vid-a", kind="video", guide=GUIDE_V)
    r = client.put(
        "/api/settings",
        json={"values": {"llm_base_url": llm.base_url, "llm_model": "fake-model"}},
    )
    assert r.status_code == 200
    project_id = client.post("/api/projects", json={"title": "Guides"}).json()["id"]
    scene_id = client.post(
        f"/api/projects/{project_id}/scenes", json={"title": "One"}
    ).json()["id"]
    shot_id = client.post(
        f"/api/scenes/{scene_id}/shots", json={"description": "A man walks."}
    ).json()["id"]
    return {"project_id": project_id, "shot_id": shot_id}


def sent_system(llm) -> str:
    return llm.requests[-1]["messages"][0]["content"]


def test_enhance_uses_selected_engines_guide(client, llm, board):
    llm.queue("A man walks down a rainy street.")
    r = client.post(
        f"/api/shots/{board['shot_id']}/enhance", json={"workflow_id": "img-a"}
    )
    assert r.status_code == 200, r.text
    system = sent_system(llm)
    assert "IMG-A-STYLE" in system and "IMG-A example one." in system
    assert "IMG-B-STYLE" not in system and "VID-A-STYLE" not in system


def test_enhance_falls_back_to_default_image_workflow(client, llm, board):
    client.put("/api/settings", json={"values": {"default_image_workflow": "img-b"}})
    llm.queue("A man walks down a rainy street.")
    r = client.post(f"/api/shots/{board['shot_id']}/enhance", json={})
    assert r.status_code == 200, r.text
    system = sent_system(llm)
    assert "IMG-B-STYLE" in system
    assert "IMG-A-STYLE" not in system and "VID-A-STYLE" not in system


def test_enhance_unknown_engine_means_no_guide(client, llm, board):
    llm.queue("A man walks down a rainy street.")
    r = client.post(
        f"/api/shots/{board['shot_id']}/enhance", json={"workflow_id": "gone"}
    )
    assert r.status_code == 200, r.text
    assert "ACTIVE RENDER ENGINE" not in sent_system(llm)


def test_generate_motion_uses_video_guide_not_image(client, llm, board):
    llm.queue("He keeps walking. Audio: rain.")
    r = client.post(
        f"/api/shots/{board['shot_id']}/generate-motion", json={"workflow_id": "vid-a"}
    )
    assert r.status_code == 200, r.text
    system = sent_system(llm)
    assert "VID-A-STYLE" in system
    assert "IMG-A-STYLE" not in system and "IMG-B-STYLE" not in system


def test_generate_motion_default_video_workflow(client, llm, board):
    client.put("/api/settings", json={"values": {"default_video_workflow": "vid-a"}})
    llm.queue("He keeps walking. Audio: rain.")
    r = client.post(f"/api/shots/{board['shot_id']}/generate-motion", json={})
    assert r.status_code == 200, r.text
    assert "VID-A-STYLE" in sent_system(llm)


@pytest.mark.parametrize("mode", ["script", "vibes"])
def test_breakdown_uses_default_image_guide(client, llm, board, mode):
    client.put("/api/settings", json={"values": {"default_image_workflow": "img-a"}})
    r = client.post(
        "/api/breakdown",
        json={
            "project_id": board["project_id"],
            "script_text": "A man walks into a bar.",
            "mode": mode,
        },
    )
    assert r.status_code == 200, r.text
    system = sent_system(llm)
    assert "IMG-A-STYLE" in system and GUIDE_LEAD_IN in system
    assert "VID-A-STYLE" not in system
