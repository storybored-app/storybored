# OWNED-BY: llm-agent
"""generate-motion (PromptSmith) endpoint: fake-LLM behavior, frame awareness."""

import pytest
from fake_llm import FakeLLM

from storybored.llm.motion import build_motion_notes

MOTION = (
    "@nova lifts the mug and glances toward the window as the camera pushes in "
    'slowly. Audio: rain taps the glass over low diner hum.'
)


@pytest.fixture
def llm():
    server = FakeLLM().start()
    yield server
    server.stop()


@pytest.fixture
def shot_id(client, app) -> int:
    r = client.post("/api/projects", json={"title": "Motion Film"})
    project_id = r.json()["id"]
    r = client.post(
        f"/api/projects/{project_id}/scenes",
        json={
            "title": "Late Shift",
            "slugline": "INT. ROADSIDE DINER - NIGHT",
            "description": "Buzzing fluorescents, rain on the windows.",
        },
    )
    scene_id = r.json()["id"]
    r = client.post(
        f"/api/scenes/{scene_id}/shots",
        json={
            "description": "@nova nurses a coffee, tired.",
            "shot_type": "MCU",
            "camera": "slow push in",
            "dialogue": "Another night like this.",
        },
    )
    return r.json()["id"]


def configure_llm(client, llm):
    r = client.put(
        "/api/settings",
        json={"values": {"llm_base_url": llm.base_url, "llm_model": "fake-model"}},
    )
    assert r.status_code == 200, r.text


def test_generate_motion_returns_prompt_without_persisting(client, llm, shot_id):
    configure_llm(client, llm)
    llm.queue(MOTION)
    r = client.post(f"/api/shots/{shot_id}/generate-motion", json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"motion_prompt": MOTION}
    # nothing persisted — the client puts it in the visible Motion field
    assert client.get(f"/api/shots/{shot_id}").json()["motion_prompt"] == ""
    # the notes carried the shot's structured context + the frame anchor
    user_msg = llm.requests[-1]["messages"][-1]["content"]
    assert "@nova nurses a coffee" in user_msg
    assert "Shot type: MCU" in user_msg
    assert "Camera: slow push in" in user_msg
    assert 'Dialogue: Another night like this.' in user_msg
    assert "ROADSIDE DINER" in user_msg
    assert "FIRST frame" in user_msg


def test_generate_motion_last_frame_notes(client, llm, shot_id):
    configure_llm(client, llm)
    llm.queue(MOTION)
    r = client.post(
        f"/api/shots/{shot_id}/generate-motion", json={"frame_position": "last"}
    )
    assert r.status_code == 200
    user_msg = llm.requests[-1]["messages"][-1]["content"]
    assert "LAST frame" in user_msg and "arrive" in user_msg


def test_generate_motion_uses_stored_frame_position(client, llm, shot_id):
    configure_llm(client, llm)
    client.patch(f"/api/shots/{shot_id}", json={"frame_position": "last"})
    llm.queue(MOTION)
    r = client.post(f"/api/shots/{shot_id}/generate-motion", json={})
    assert r.status_code == 200
    assert "LAST frame" in llm.requests[-1]["messages"][-1]["content"]


def test_generate_motion_keeps_rough_note_handles(client, llm, shot_id):
    """@handles from the author's own motion notes survive via one nudge retry."""
    configure_llm(client, llm)
    llm.queue(
        "The woman walks away as the camera holds. Audio: wind.",  # dropped @nova
        "@nova walks away as the camera holds. Audio: wind.",
    )
    r = client.post(
        f"/api/shots/{shot_id}/generate-motion",
        json={"motion_prompt": "@nova walks away"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["motion_prompt"].startswith("@nova walks away as")
    # both calls hit the LLM (original + nudge)
    assert len(llm.requests) == 2

    # dropped twice → clean 502
    llm.queue("She leaves. Audio: wind.", "She leaves. Audio: wind.")
    r = client.post(
        f"/api/shots/{shot_id}/generate-motion",
        json={"motion_prompt": "@nova walks away"},
    )
    assert r.status_code == 502
    assert "@nova" in r.json()["detail"]


def test_generate_motion_editor_state_wins(client, llm, shot_id):
    configure_llm(client, llm)
    llm.queue(MOTION)
    r = client.post(
        f"/api/shots/{shot_id}/generate-motion",
        json={"description": "@nova bolts out the door", "camera": "whip pan"},
    )
    assert r.status_code == 200
    user_msg = llm.requests[-1]["messages"][-1]["content"]
    assert "@nova bolts out the door" in user_msg
    assert "Camera: whip pan" in user_msg
    assert "slow push in" not in user_msg


def test_generate_motion_empty_description_400_and_unconfigured_503(
    client, llm, shot_id
):
    configure_llm(client, llm)
    r = client.post(
        f"/api/shots/{shot_id}/generate-motion", json={"description": "   "}
    )
    assert r.status_code == 400

    client.put("/api/settings", json={"values": {"llm_base_url": ""}})
    r = client.post(f"/api/shots/{shot_id}/generate-motion", json={})
    assert r.status_code == 503


def test_build_motion_notes_shapes():
    notes = build_motion_notes(
        "@hero stands at the cliff edge",
        shot_type="WIDE",
        camera="crane up",
        motion_prompt="he turns at the end",
        dialogue="It's over.",
        duration_s=5.0,
        scene_slugline="EXT. CLIFF - DUSK",
        frame_position="last",
    )
    lines = notes.splitlines()
    assert lines[0].endswith("@hero stands at the cliff edge")
    assert "Rough motion notes: he turns at the end" in lines
    assert "Clip length: about 5 seconds" in lines
    assert notes.splitlines()[-1].startswith("The still is the LAST frame")
    # sparse shot: only the description + frame line
    sparse = build_motion_notes("a quiet street")
    assert sparse.splitlines() == [
        "Image prompt (what the still shows): a quiet street",
        "The still is the FIRST frame of the clip — motion flows forward from it.",
    ]
