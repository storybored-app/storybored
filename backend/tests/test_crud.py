"""Projects / scenes / shots lifecycle, reorder, board payload, approve guard."""

from sqlmodel import Session

from storybored.models import Take


def make_project(client, title="Test Film"):
    r = client.post("/api/projects", json={"title": title})
    assert r.status_code == 201
    return r.json()


def make_scene(client, project_id, title="Scene"):
    r = client.post(f"/api/projects/{project_id}/scenes", json={"title": title})
    assert r.status_code == 201
    return r.json()


def make_shot(client, scene_id, **fields):
    r = client.post(f"/api/scenes/{scene_id}/shots", json=fields)
    assert r.status_code == 201
    return r.json()


def make_take(app, shot_id, status="done", kind="image"):
    with Session(app.state.engine, expire_on_commit=False) as session:
        take = Take(shot_id=shot_id, kind=kind, status=status, workflow_id="test", seed=7)
        session.add(take)
        session.commit()
        return take.id


def test_project_lifecycle(client):
    project = make_project(client)
    assert project["aspect_ratio"] == "16:9"

    r = client.get("/api/projects")
    assert r.status_code == 200
    assert [p["id"] for p in r.json()] == [project["id"]]

    r = client.patch(f"/api/projects/{project['id']}", json={"description": "a film"})
    assert r.status_code == 200
    assert r.json()["description"] == "a film"

    r = client.delete(f"/api/projects/{project['id']}")
    assert r.status_code == 204
    assert client.get(f"/api/projects/{project['id']}").status_code == 404
    assert client.get("/api/projects").json() == []


def test_scene_and_shot_crud(client):
    project = make_project(client)
    scene = make_scene(client, project["id"], title="Opening")
    assert scene["idx"] == 0

    r = client.patch(f"/api/scenes/{scene['id']}", json={"slugline": "EXT. FIELD - DAY"})
    assert r.status_code == 200
    assert r.json()["slugline"] == "EXT. FIELD - DAY"

    shot = make_shot(client, scene["id"], description="a wide field", shot_type="WIDE")
    assert shot["status"] == "draft"
    assert shot["idx"] == 0
    assert shot["duration_s"] == 4.0

    r = client.patch(f"/api/shots/{shot['id']}", json={"duration_s": 6.5, "camera": "dolly"})
    assert r.status_code == 200
    assert r.json()["duration_s"] == 6.5

    r = client.delete(f"/api/shots/{shot['id']}")
    assert r.status_code == 204
    assert client.get(f"/api/shots/{shot['id']}").status_code == 404

    r = client.delete(f"/api/scenes/{scene['id']}")
    assert r.status_code == 204


def test_reorder_scenes_and_shots(client):
    project = make_project(client)
    a = make_scene(client, project["id"], "A")
    b = make_scene(client, project["id"], "B")
    c = make_scene(client, project["id"], "C")

    r = client.post(
        f"/api/projects/{project['id']}/scenes/reorder",
        json={"scene_ids": [c["id"], a["id"], b["id"]]},
    )
    assert r.status_code == 200
    board = client.get(f"/api/projects/{project['id']}").json()
    assert [s["title"] for s in board["scenes"]] == ["C", "A", "B"]

    s1 = make_shot(client, a["id"], description="one")
    s2 = make_shot(client, a["id"], description="two")
    s3 = make_shot(client, a["id"], description="three")
    r = client.post(
        f"/api/scenes/{a['id']}/shots/reorder",
        json={"shot_ids": [s3["id"], s1["id"], s2["id"]]},
    )
    assert r.status_code == 200
    assert r.json()["shot_ids"] == [s3["id"], s1["id"], s2["id"]]

    # cross-scene move: drag shot "one" into scene B
    r = client.post(f"/api/scenes/{b['id']}/shots/reorder", json={"shot_ids": [s1["id"]]})
    assert r.status_code == 200
    moved = client.get(f"/api/shots/{s1['id']}").json()
    assert moved["scene_id"] == b["id"]
    assert moved["idx"] == 0

    # unknown scene id in reorder → 400
    r = client.post(
        f"/api/projects/{project['id']}/scenes/reorder", json={"scene_ids": [999999]}
    )
    assert r.status_code == 400


def test_board_payload_shape(client, app):
    project = make_project(client, title="Board Film")
    scene = make_scene(client, project["id"], title="S1")
    shot = make_shot(client, scene["id"], description="hero shot", shot_type="CU")
    take_id = make_take(app, shot["id"])

    board = client.get(f"/api/projects/{project['id']}").json()
    assert board["title"] == "Board Film"
    assert len(board["scenes"]) == 1
    scene_data = board["scenes"][0]
    assert scene_data["title"] == "S1"
    assert len(scene_data["shots"]) == 1
    shot_data = scene_data["shots"][0]
    assert shot_data["shot_type"] == "CU"
    assert [t["id"] for t in shot_data["takes"]] == [take_id]
    assert shot_data["takes"][0]["status"] == "done"

    r = client.get(f"/api/shots/{shot['id']}/takes")
    assert r.status_code == 200
    assert [t["id"] for t in r.json()] == [take_id]


def test_approve_requires_picked_take(client, app):
    project = make_project(client)
    scene = make_scene(client, project["id"])
    shot = make_shot(client, scene["id"], description="approve me")

    # approve without a picked take → rejected
    r = client.post(f"/api/shots/{shot['id']}/approve")
    assert r.status_code == 409

    # a pending take cannot be picked
    pending = make_take(app, shot["id"], status="pending")
    assert client.post(f"/api/takes/{pending}/pick").status_code == 409

    done = make_take(app, shot["id"], status="done")
    r = client.post(f"/api/takes/{done}/pick")
    assert r.status_code == 200
    assert r.json()["picked_take_id"] == done

    r = client.post(f"/api/shots/{shot['id']}/approve")
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    r = client.post(f"/api/shots/{shot['id']}/unapprove")
    assert r.status_code == 200
    assert r.json()["status"] == "generated"

    # deleting the picked take clears the pick
    r = client.delete(f"/api/takes/{done}")
    assert r.status_code == 204
    assert client.get(f"/api/shots/{shot['id']}").json()["picked_take_id"] is None


def test_demo_seed(client):
    r = client.post("/api/demo")
    assert r.status_code == 201
    project_id = r.json()["id"]
    board = client.get(f"/api/projects/{project_id}").json()
    assert board["title"] == "The Last Lighthouse"
    assert len(board["scenes"]) == 2
    assert sum(len(s["shots"]) for s in board["scenes"]) == 6
