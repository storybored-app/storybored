# OWNED-BY: lifecycle-agent
"""The .storybored archive: export job, download, import (merge/rename/zip-slip)."""

import io
import json
import time
import zipfile

from sqlmodel import Session, select

from storybored.models import Character, ShotCharacter, Take

# -- helpers ------------------------------------------------------------------


def wait_job(client, job_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"done", "failed", "cancelled"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {job}")


def fabricate_take(app, settings, project_id, shot_id, kind="image", workflow_id=""):
    ext = "png" if kind == "image" else "mp4"
    with Session(app.state.engine, expire_on_commit=False) as session:
        take = Take(shot_id=shot_id, kind=kind, status="done", workflow_id=workflow_id)
        session.add(take)
        session.commit()
        session.refresh(take)
        dest_dir = settings.media_path / str(project_id) / str(shot_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        file = dest_dir / f"take_{take.id}.{ext}"
        thumb = dest_dir / f"take_{take.id}_thumb.png"
        file.write_bytes(f"media-{take.id}".encode())
        thumb.write_bytes(f"thumb-{take.id}".encode())
        take.file_path = str(file.relative_to(settings.data_path))
        take.thumb_path = str(thumb.relative_to(settings.data_path))
        session.add(take)
        session.commit()
    return take


def install_user_pack(settings, pack_id="userpack"):
    pack_dir = settings.data_path / "workflows" / pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "manifest.json").write_text(
        json.dumps({"id": pack_id, "name": "User pack", "kind": "image", "parameters": []})
    )
    (pack_dir / "graph.json").write_text("{}")
    return pack_dir


def build_project(client, app, settings):
    """A project with a character mention, two takes (one picked), an animatic."""
    r = client.post(
        "/api/characters",
        json={
            "name": "Keeper",
            "handle": "keeper",
            "trigger": "zxkeeper",
            "lora_name": "characters/keeper_v1.safetensors",
        },
    )
    assert r.status_code == 201, r.text
    character = r.json()
    project = client.post(
        "/api/projects", json={"title": "Lighthouse", "description": "two scenes"}
    ).json()
    scene = client.post(
        f"/api/projects/{project['id']}/scenes", json={"title": "Dawn", "slugline": "EXT. SEA"}
    ).json()
    shot = client.post(
        f"/api/scenes/{scene['id']}/shots",
        json={"description": "WIDE: @keeper on the rocks", "motion_prompt": "@keeper turns"},
    ).json()
    install_user_pack(settings)
    take1 = fabricate_take(app, settings, project["id"], shot["id"], workflow_id="userpack")
    take2 = fabricate_take(
        app, settings, project["id"], shot["id"], kind="video", workflow_id="krea2-basic"
    )
    assert client.post(f"/api/takes/{take1.id}/pick").status_code == 200
    assert client.post(f"/api/takes/{take2.id}/pick").status_code == 200
    export_dir = settings.exports_path / str(project["id"])
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / "animatic_1.mp4").write_bytes(b"fake-animatic")
    return character, project, scene, shot, take1, take2


def run_export(client, project_id):
    r = client.post(f"/api/projects/{project_id}/export")
    assert r.status_code == 200, r.text
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == "done", job
    return job


# -- export -------------------------------------------------------------------


def test_export_archive_contents(client, app, settings):
    character, project, scene, shot, take1, take2 = build_project(client, app, settings)
    job = run_export(client, project["id"])
    assert job["lane"] == "io"
    assert job["project_id"] == project["id"]
    result = json.loads(job["result_json"])
    archive = settings.data_path / result["file_path"]
    assert archive.is_file()
    assert archive.name == f"project-{project['id']}.storybored"

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["schema_version"] == 1
        assert manifest["format"] == "storybored-project"
        assert manifest["app_version"]
        board = manifest["project"]
        assert board["title"] == "Lighthouse"
        assert board["scenes"][0]["shots"][0]["picked_take_id"] == take1.id
        # characters travel as soft references — never LoRA weight files
        chars = {c["handle"]: c for c in manifest["characters"]}
        assert chars["keeper"]["lora_name"] == "characters/keeper_v1.safetensors"
        assert chars["keeper"]["trigger"] == "zxkeeper"
        assert not any(n.endswith(".safetensors") for n in names)
        # media + thumbs + animatic all present, DATA_DIR-relative
        assert take1.file_path in names
        assert take1.thumb_path in names
        assert take2.file_path in names
        assert f"exports/{project['id']}/animatic_1.mp4" in names
        # user pack bundled; repo pack only noted
        assert "workflows/userpack/manifest.json" in names
        assert not any(n.startswith("workflows/krea2-basic/") for n in names)
        assert manifest["workflow_packs"]["bundled"] == ["userpack"]
        assert manifest["workflow_packs"]["builtin"] == ["krea2-basic"]


def test_export_download_endpoint(client, app, settings):
    _, project, *_ = build_project(client, app, settings)
    # nothing exported yet → 404
    assert client.get(f"/api/projects/{project['id']}/export/download").status_code == 404
    run_export(client, project["id"])
    r = client.get(f"/api/projects/{project['id']}/export/download")
    assert r.status_code == 200
    assert "storybored" in r.headers.get("content-disposition", "")
    assert zipfile.ZipFile(io.BytesIO(r.content)).testzip() is None
    # unknown project → 404
    assert client.get("/api/projects/999999/export/download").status_code == 404


# -- import -------------------------------------------------------------------


def export_bytes(client, settings, project_id):
    run_export(client, project_id)
    return client.get(f"/api/projects/{project_id}/export/download").content


def do_import(client, data: bytes, mode=None):
    form = {"mode": mode} if mode else {}
    return client.post(
        "/api/projects/import",
        files={"file": ("project.storybored", io.BytesIO(data), "application/zip")},
        data=form,
    )


def test_import_merge_roundtrip(client, app, settings):
    character, project, scene, shot, take1, take2 = build_project(client, app, settings)
    data = export_bytes(client, settings, project["id"])

    r = do_import(client, data)  # default mode = merge
    assert r.status_code == 201, r.text
    body = r.json()
    new_id = body["project"]["id"]
    assert new_id != project["id"]
    assert body["characters"]["linked"] == ["keeper"]
    assert body["characters"]["created"] == []
    assert any("keeper_v1" not in w for w in body["warnings"]) or body["warnings"] == []

    board = client.get(f"/api/projects/{new_id}").json()
    assert board["title"] == "Lighthouse"
    assert len(board["scenes"]) == 1
    new_shot = board["scenes"][0]["shots"][0]
    assert new_shot["description"] == "WIDE: @keeper on the rocks"
    takes = {t["kind"]: t for t in new_shot["takes"]}
    assert len(new_shot["takes"]) == 2
    # pick pointers remapped onto the NEW take ids
    assert new_shot["picked_take_id"] == takes["image"]["id"]
    assert new_shot["video_take_id"] == takes["video"]["id"]
    assert new_shot["picked_take_id"] not in (take1.id, take2.id)
    # media extracted under the new ids with rewritten relative paths
    for take in new_shot["takes"]:
        expected_dir = f"media/{new_id}/{new_shot['id']}/"
        assert take["file_path"].startswith(expected_dir)
        assert (settings.data_path / take["file_path"]).is_file()
        assert (settings.data_path / take["thumb_path"]).is_file()
    # original files carried over byte-for-byte
    assert (
        settings.data_path / takes["image"]["file_path"]
    ).read_bytes() == f"media-{take1.id}".encode()
    # animatic travels along
    assert (settings.exports_path / str(new_id) / "animatic_1.mp4").is_file()
    # merge links the existing character row — no duplicate
    with Session(app.state.engine) as session:
        chars = session.exec(select(Character)).all()
        assert len(chars) == 1
        links = session.exec(
            select(ShotCharacter).where(ShotCharacter.shot_id == new_shot["id"])
        ).all()
        assert [link.character_id for link in links] == [character["id"]]


def test_import_merge_creates_missing_characters(client, app, settings):
    character, project, *_ = build_project(client, app, settings)
    data = export_bytes(client, settings, project["id"])
    assert client.delete(f"/api/characters/{character['id']}").status_code == 204

    r = do_import(client, data, mode="merge")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["characters"]["created"] == ["keeper"]
    assert any("keeper_v1" in w for w in body["warnings"])  # missing-LoRA warning
    with Session(app.state.engine) as session:
        keeper = session.exec(select(Character).where(Character.handle == "keeper")).one()
        assert keeper.trigger == "zxkeeper"
        assert keeper.lora_name == "characters/keeper_v1.safetensors"


def test_import_rename_suffixes_and_rewrites_mentions(client, app, settings):
    character, project, *_ = build_project(client, app, settings)
    data = export_bytes(client, settings, project["id"])

    r = do_import(client, data, mode="rename")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["characters"]["renamed"] == {"keeper": "keeper2"}
    assert body["characters"]["created"] == ["keeper2"]

    new_id = body["project"]["id"]
    board = client.get(f"/api/projects/{new_id}").json()
    new_shot = board["scenes"][0]["shots"][0]
    assert new_shot["description"] == "WIDE: @keeper2 on the rocks"
    assert new_shot["motion_prompt"] == "@keeper2 turns"
    with Session(app.state.engine) as session:
        keeper2 = session.exec(select(Character).where(Character.handle == "keeper2")).one()
        links = session.exec(
            select(ShotCharacter).where(ShotCharacter.shot_id == new_shot["id"])
        ).all()
        assert [link.character_id for link in links] == [keeper2.id]
        assert keeper2.id != character["id"]


def test_import_bundled_pack_installed_once(client, app, settings):
    _, project, *_ = build_project(client, app, settings)
    data = export_bytes(client, settings, project["id"])

    # pack already present → bundled copy ignored, warning raised
    r = do_import(client, data)
    assert r.status_code == 201
    assert any("userpack" in w and "already installed" in w for w in r.json()["warnings"])

    # pack absent → bundled copy extracted into DATA_DIR/workflows
    import shutil

    shutil.rmtree(settings.data_path / "workflows" / "userpack")
    r = do_import(client, data)
    assert r.status_code == 201
    assert (settings.data_path / "workflows" / "userpack" / "manifest.json").is_file()
    assert not any("already installed" in w for w in r.json()["warnings"])


def make_archive(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def minimal_manifest(**overrides) -> dict:
    manifest = {
        "format": "storybored-project",
        "schema_version": 1,
        "app_version": "0.0.0",
        "project": {"id": 1, "title": "Evil", "scenes": []},
        "characters": [],
        "workflow_packs": {"builtin": [], "bundled": [], "not_installed": []},
    }
    manifest.update(overrides)
    return manifest


def test_import_rejects_zip_slip(client, settings):
    evil = make_archive(
        {
            "manifest.json": json.dumps(
                minimal_manifest(
                    workflow_packs={"builtin": [], "bundled": ["evil"], "not_installed": []}
                )
            ).encode(),
            "workflows/evil/../../../pwned.txt": b"gotcha",
        }
    )
    r = do_import(client, evil)
    assert r.status_code == 400
    assert "unsafe path" in r.json()["detail"]
    # nothing escaped DATA_DIR, and the half-import left no project behind
    assert not (settings.data_path.parent / "pwned.txt").exists()
    assert not (settings.data_path / "pwned.txt").exists()
    assert client.get("/api/projects").json() == []


def test_import_rejects_newer_schema_and_junk(client, settings):
    future = make_archive(
        {"manifest.json": json.dumps(minimal_manifest(schema_version=99)).encode()}
    )
    r = do_import(client, future)
    assert r.status_code == 400
    assert "newer" in r.json()["detail"]

    r = do_import(client, b"this is not a zip file")
    assert r.status_code == 400
    assert "not a .storybored archive" in r.json()["detail"]

    no_manifest = make_archive({"readme.txt": b"hi"})
    r = do_import(client, no_manifest)
    assert r.status_code == 400
    assert "manifest" in r.json()["detail"]

    r = do_import(client, make_archive({"manifest.json": b"[]"}), mode="merge")
    assert r.status_code == 400

    r = do_import(client, make_archive({"manifest.json": b"{}"}), mode="sideways")
    assert r.status_code == 400
    assert "mode" in r.json()["detail"]
