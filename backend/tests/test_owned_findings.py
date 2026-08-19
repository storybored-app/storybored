"""Regression tests for the shots/breakdown/demo findings:

- uppercase @mentions cast a character (handles are stored lowercase);
- character tags applied via apply-breakdown survive to generation's built graph;
- POST /api/demo returns the nested board payload.
"""

import json
from pathlib import Path

from sqlmodel import Session, select

from storybored.casting import refresh_shot_characters
from storybored.engine.graph import inject_characters, parse_mentions
from storybored.models import Character, Scene, Shot, ShotCharacter

WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"


def add_character(app, handle="ava", name="Ava", lora="characters/ava_v1.safetensors"):
    with Session(app.state.engine, expire_on_commit=False) as session:
        character = Character(
            name=name,
            handle=handle,
            trigger=f"{handle}x7",
            class_word="person",
            lora_name=lora,
            lora_strength=1.0,
        )
        session.add(character)
        session.commit()
        session.refresh(character)
        return character


def make_scene(client, project_id) -> int:
    r = client.post(f"/api/projects/{project_id}/scenes", json={"title": "S"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def new_project(client) -> int:
    r = client.post("/api/projects", json={"title": "Test Film"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# -- finding 1: uppercase mention casts the (lowercase-stored) character -------


def test_uppercase_mention_casts_character(client, app):
    character = add_character(app, "testchar", "Test Char")
    project_id = new_project(client)
    scene_id = make_scene(client, project_id)

    r = client.post(
        f"/api/scenes/{scene_id}/shots",
        json={"description": "A hallway, @TestChar steps into frame."},
    )
    assert r.status_code == 201, r.text
    shot_id = r.json()["id"]

    with Session(app.state.engine) as session:
        links = session.exec(
            select(ShotCharacter).where(ShotCharacter.shot_id == shot_id)
        ).all()
        assert [link.character_id for link in links] == [character.id]


# -- finding 2: applied tags survive to the built graph -----------------------


def test_apply_breakdown_character_survives_to_built_graph(client, app):
    character = add_character(app, "ava", "Ava")
    project_id = new_project(client)

    draft = {
        "scenes": [
            {
                "title": "Lamp Room",
                "slugline": "INT. LAMP ROOM - NIGHT",
                "shots": [
                    {
                        # description does NOT mention @ava — the tag alone must
                        # carry the character through to generation.
                        "description": "Close on the dead bulb, dust drifting.",
                        "shot_type": "CU",
                        "duration_s": 3,
                        "characters": ["Ava"],  # uppercase tag, known character
                    }
                ],
            }
        ]
    }

    r = client.post(f"/api/projects/{project_id}/apply-breakdown", json={"draft": draft})
    assert r.status_code == 200, r.text
    assert r.json()["characters_linked"] == 1

    manifest = json.loads((WORKFLOWS / "krea2-basic" / "manifest.json").read_text())
    graph = json.loads((WORKFLOWS / "krea2-basic" / "graph.json").read_text())

    with Session(app.state.engine) as session:
        scene = session.exec(
            select(Scene).where(Scene.project_id == project_id)
        ).one()
        shot = session.exec(select(Shot).where(Shot.scene_id == scene.id)).one()

        # the applied tag was injected into the description as an editable @handle
        assert "@ava" in shot.description

        # reproduce what image.py does to build the graph's character list
        refresh_shot_characters(session, shot)
        session.commit()
        handles = parse_mentions(shot.description or "")
        rows = session.exec(
            select(Character).where(Character.handle.in_(handles))  # type: ignore[attr-defined]
        ).all()
        by_handle = {c.handle: c for c in rows}
        characters = [
            by_handle[h] for h in handles if h in by_handle and by_handle[h].lora_name
        ]
        assert [c.handle for c in characters] == ["ava"]

        injected = inject_characters(graph, manifest["character_injection"], characters)
        assert injected  # the character produced real node(s) in the built graph
        assert character.id is not None


# -- finding 4: demo returns the nested board payload -------------------------


def test_demo_returns_nested_board(client):
    r = client.post("/api/demo")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["title"] == "The Last Lighthouse"
    assert "scenes" in body and len(body["scenes"]) == 2
    first = body["scenes"][0]
    assert "shots" in first and len(first["shots"]) >= 1
    assert "takes" in first["shots"][0]
