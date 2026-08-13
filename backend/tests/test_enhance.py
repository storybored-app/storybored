# OWNED-BY: llm-agent
"""Enhance (PromptSmith) endpoint: fake-LLM behavior, handle guard, cleanup."""

import pytest
from fake_llm import FakeLLM
from sqlmodel import Session

from storybored.llm.enhance import _clean, build_notes
from storybored.models import Shot

ENHANCED = (
    "Medium close-up of @nova at a rain-streaked diner window, head and "
    "shoulders framing, cool fluorescent light, shallow depth of field."
)


@pytest.fixture
def llm():
    server = FakeLLM().start()
    yield server
    server.stop()


@pytest.fixture
def shot_id(client, app) -> int:
    r = client.post("/api/projects", json={"title": "Enhance Film"})
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
        json={"description": "@nova nurses a coffee, tired.", "shot_type": "MCU"},
    )
    return r.json()["id"]


def configure_llm(client, llm):
    r = client.put(
        "/api/settings",
        json={"values": {"llm_base_url": llm.base_url, "llm_model": "fake-model"}},
    )
    assert r.status_code == 200, r.text


def test_enhance_returns_cleaned_prompt_without_persisting(client, llm, shot_id):
    configure_llm(client, llm)
    llm.queue(f"<think>hmm framing...</think>\n{ENHANCED}")
    r = client.post(f"/api/shots/{shot_id}/enhance")
    assert r.status_code == 200, r.text
    assert r.json()["description"] == ENHANCED

    # scene context and structured fields reached the model
    sent = llm.requests[-1]["messages"][-1]["content"]
    assert "Shot type: MCU" in sent
    assert "INT. ROADSIDE DINER - NIGHT" in sent

    # nothing persisted — the visible editor owns the save
    assert client.get(f"/api/shots/{shot_id}").json()["description"] == (
        "@nova nurses a coffee, tired."
    )


def test_enhance_retries_when_handles_dropped_then_fails(client, llm, shot_id):
    configure_llm(client, llm)
    # first reply loses @nova, retry still loses it → 502 with a clear message
    llm.queue("A woman nurses a coffee.", "Still no handle here.")
    r = client.post(f"/api/shots/{shot_id}/enhance")
    assert r.status_code == 502
    assert "@nova" in r.json()["detail"]

    # retry that recovers the handle succeeds
    llm.queue("A woman nurses a coffee.", ENHANCED)
    r = client.post(f"/api/shots/{shot_id}/enhance")
    assert r.status_code == 200
    assert "@nova" in r.json()["description"]


def test_enhance_uses_unsaved_editor_state(client, llm, shot_id):
    configure_llm(client, llm)
    llm.queue(ENHANCED)
    r = client.post(
        f"/api/shots/{shot_id}/enhance",
        json={"description": "@nova slams the mug down", "shot_type": "ECU"},
    )
    assert r.status_code == 200
    sent = llm.requests[-1]["messages"][-1]["content"]
    assert "@nova slams the mug down" in sent
    assert "Shot type: ECU" in sent


def test_enhance_empty_description_400_and_unconfigured_503(client, llm, shot_id, app):
    with Session(app.state.engine, expire_on_commit=False) as session:
        shot = session.get(Shot, shot_id)
        shot.description = ""
        session.add(shot)
        session.commit()
    r = client.post(f"/api/shots/{shot_id}/enhance")
    assert r.status_code == 400

    with Session(app.state.engine, expire_on_commit=False) as session:
        shot = session.get(Shot, shot_id)
        shot.description = "@nova waits."
        session.add(shot)
        session.commit()
    r = client.post(f"/api/shots/{shot_id}/enhance")
    assert r.status_code == 503  # no LLM configured


def test_clean_and_notes_helpers():
    assert _clean('```\n"A  prompt\nwith noise"\n```') == "A prompt with noise"
    assert _clean("<think>reasoning</think>final text") == "final text"
    notes = build_notes("desc", shot_type="WIDE", scene_slugline="EXT. ROAD - DAWN")
    assert notes == "desc\nShot type: WIDE\nScene: EXT. ROAD - DAWN"
