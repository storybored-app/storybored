"""LLM breakdown: client/parse behavior via a fake OpenAI server + apply-breakdown."""

import json

import pytest
from fake_llm import FakeLLM
from sqlmodel import Session, select

from storybored.models import Character, Scene, Shot, ShotCharacter

VALID_DRAFT = {
    "scenes": [
        {
            "title": "Lamp Room",
            "slugline": "INT. LIGHTHOUSE LAMP ROOM - NIGHT",
            "shots": [
                {
                    "description": "Wide of the lamp room, @ava silhouetted at the glass.",
                    "shot_type": "WIDE",
                    "camera": "slow push in",
                    "dialogue": "",
                    "duration_s": 5,
                    "characters": ["ava"],
                },
                {
                    "description": "Close on the dead bulb, dust drifting.",
                    "shot_type": "CU",
                    "camera": "static",
                    "dialogue": "It's out again.",
                    "duration_s": 3,
                    "characters": ["@ava", "ghost"],
                },
            ],
        }
    ]
}


@pytest.fixture
def llm():
    server = FakeLLM().start()
    yield server
    server.stop()


@pytest.fixture
def project_id(client) -> int:
    r = client.post("/api/projects", json={"title": "Test Film"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def configure_llm(client, llm):
    r = client.put(
        "/api/settings",
        json={"values": {"llm_base_url": llm.base_url, "llm_model": "fake-model"}},
    )
    assert r.status_code == 200, r.text


def add_character(app, handle="ava", name="Ava"):
    with Session(app.state.engine, expire_on_commit=False) as session:
        character = Character(name=name, handle=handle, trigger=f"{handle}x7")
        session.add(character)
        session.commit()
        session.refresh(character)
        return character


# -- POST /api/breakdown ------------------------------------------------------


def test_breakdown_unconfigured_returns_503(client, project_id):
    r = client.post(
        "/api/breakdown", json={"project_id": project_id, "script_text": "INT. HOUSE - DAY"}
    )
    assert r.status_code == 503
    assert "Settings" in r.json()["detail"]


def test_breakdown_unknown_project_404(client, llm):
    configure_llm(client, llm)
    r = client.post("/api/breakdown", json={"project_id": 9999, "script_text": "x"})
    assert r.status_code == 404


def test_breakdown_empty_script_400(client, llm, project_id):
    configure_llm(client, llm)
    r = client.post("/api/breakdown", json={"project_id": project_id, "script_text": "  "})
    assert r.status_code == 400


def test_breakdown_vibes_mode(client, llm, project_id):
    configure_llm(client, llm)
    llm.queue(json.dumps(VALID_DRAFT))
    r = client.post(
        "/api/breakdown",
        json={"project_id": project_id, "script_text": "a story about a drifter", "mode": "vibes"},
    )
    assert r.status_code == 200, r.text
    system = llm.requests[-1]["messages"][0]["content"]
    assert "render-ready" in system  # vibes prompt, not the 1st-AD one
    assert "@handle" in system

    r = client.post(
        "/api/breakdown",
        json={"project_id": project_id, "script_text": "x", "mode": "freestyle"},
    )
    assert r.status_code == 400


def test_breakdown_valid_json(client, app, llm, project_id):
    configure_llm(client, llm)
    add_character(app, "ava")
    llm.queue(json.dumps(VALID_DRAFT))
    r = client.post(
        "/api/breakdown",
        json={"project_id": project_id, "script_text": "INT. LIGHTHOUSE - NIGHT\nAva climbs."},
    )
    assert r.status_code == 200, r.text
    draft = r.json()
    assert len(draft["scenes"]) == 1
    assert len(draft["scenes"][0]["shots"]) == 2
    assert draft["scenes"][0]["shots"][0]["characters"] == ["ava"]

    # one request; system prompt embeds schema + known handles; temperature 0.3
    assert len(llm.requests) == 1
    req = llm.requests[0]
    assert req["temperature"] == 0.3
    system = req["messages"][0]["content"]
    assert "1st Assistant Director" in system
    assert '"scenes"' in system
    assert "ava" in system
    assert req["messages"][1]["content"].startswith("INT. LIGHTHOUSE")


def test_breakdown_strips_code_fences(client, llm, project_id):
    configure_llm(client, llm)
    llm.queue("```json\n" + json.dumps(VALID_DRAFT) + "\n```")
    r = client.post("/api/breakdown", json={"project_id": project_id, "script_text": "x"})
    assert r.status_code == 200, r.text
    assert r.json()["scenes"][0]["title"] == "Lamp Room"


def test_breakdown_retries_once_on_garbage(client, llm, project_id):
    configure_llm(client, llm)
    llm.queue("Sure! Here is your shot list: * wide * close ...", json.dumps(VALID_DRAFT))
    r = client.post("/api/breakdown", json={"project_id": project_id, "script_text": "x"})
    assert r.status_code == 200, r.text
    assert len(llm.requests) == 2
    retry_msgs = llm.requests[1]["messages"]
    assert "only valid JSON" in retry_msgs[-1]["content"]
    # the retry keeps the failed assistant turn for context
    assert retry_msgs[-2]["role"] == "assistant"


def test_breakdown_gives_up_after_retry_502(client, llm, project_id):
    configure_llm(client, llm)
    llm.queue("not json at all", "STILL not json { broken")
    r = client.post("/api/breakdown", json={"project_id": project_id, "script_text": "x"})
    assert r.status_code == 502
    assert len(llm.requests) == 2


# -- POST /api/projects/{id}/apply-breakdown ----------------------------------


def test_apply_breakdown_appends_and_links(client, app, project_id):
    character = add_character(app, "ava")
    # pre-existing scene → draft scenes must append after it
    r = client.post(f"/api/projects/{project_id}/scenes", json={"title": "Existing"})
    assert r.status_code == 201

    r = client.post(f"/api/projects/{project_id}/apply-breakdown", json={"draft": VALID_DRAFT})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scenes_created"] == 1
    assert body["shots_created"] == 2
    assert body["characters_linked"] == 2  # 'ava' + '@ava'; 'ghost' skipped

    with Session(app.state.engine) as session:
        scenes = session.exec(
            select(Scene).where(Scene.project_id == project_id).order_by(Scene.idx)  # type: ignore[attr-defined]
        ).all()
        assert [s.title for s in scenes] == ["Existing", "Lamp Room"]
        assert [s.idx for s in scenes] == [0, 1]

        new_scene = scenes[1]
        shots = session.exec(
            select(Shot).where(Shot.scene_id == new_scene.id).order_by(Shot.idx)  # type: ignore[attr-defined]
        ).all()
        assert len(shots) == 2
        assert shots[0].shot_type == "WIDE"
        assert shots[0].duration_s == 5.0
        assert shots[1].dialogue == "It's out again."

        links = session.exec(select(ShotCharacter)).all()
        assert {(link.shot_id, link.character_id) for link in links} == {
            (shots[0].id, character.id),
            (shots[1].id, character.id),
        }


def test_apply_breakdown_empty_draft_400(client, project_id):
    r = client.post(
        f"/api/projects/{project_id}/apply-breakdown", json={"draft": {"scenes": []}}
    )
    assert r.status_code == 400


def test_apply_breakdown_unknown_project_404(client):
    r = client.post("/api/projects/424242/apply-breakdown", json={"draft": VALID_DRAFT})
    assert r.status_code == 404
