# OWNED-BY: training-agent
"""Architecture-specific character-LoRA families, end to end.

Character LoRAs are model-family-bound (a Krea 2 LoRA is inert on Z-Image), so:
manifests may declare ``lora_family`` (surfaced by GET /api/workflows and
linted by validate-pack), characters carry a nullable ``lora_family`` column
(stamped by the training path, optional on import), renders pre-flight a
family match (409 on conflict, NULL/agnostic never blocks), training targets
the default image engine's family and passes ``--family`` to the trainer
scripts, and the setup probe reports honest per-tier training capability.
"""

import json
import os
import time
from pathlib import Path

import pytest
from fake_comfy import fake_comfy  # noqa: F401 - fixture
from fastapi.testclient import TestClient
from test_setup_probe import gpu_stats
from test_validate_pack import GOOD_GRAPH, GOOD_MANIFEST, write_pack

from storybored.config import Settings
from storybored.engine.validate import validate_pack
from storybored.main import create_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128


@pytest.fixture
def settings(tmp_path, fake_comfy):  # noqa: F811 - overrides conftest settings
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        comfyui_url=fake_comfy.url,
        comfy_mode_image_cmd="",
        comfy_mode_video_cmd="",
        comfy_flush_cmd="",
        llm_base_url="",
        lora_factory_dir="",
    )


def make_character(client, handle="mari", family=None, **extra):
    body = {
        "name": handle.title(),
        "handle": handle,
        "trigger": f"zx{handle}",
        "class_word": "person",
        "lora_name": "characters/hero_v1.safetensors",
        **extra,
    }
    if family is not None:
        body["lora_family"] = family
    r = client.post("/api/characters", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def make_shot(client, description):
    project = client.post("/api/projects", json={"title": "Film"}).json()
    scene = client.post(f"/api/projects/{project['id']}/scenes", json={"title": "S"}).json()
    return client.post(
        f"/api/scenes/{scene['id']}/shots", json={"description": description}
    ).json()


def wait_job(client, job_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"done", "failed", "cancelled"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {job}")


# -- manifest key: surfacing + validation -------------------------------------


def test_workflows_report_lora_family(client, fake_comfy):  # noqa: F811
    rows = {w["id"]: w for w in client.get("/api/workflows").json()}
    assert rows["krea2-basic"]["lora_family"] == "krea2"
    assert rows["krea2-realism"]["lora_family"] == "krea2"
    assert rows["z-image-turbo"]["lora_family"] == "z-image"
    assert rows["qwen-image-2512"]["lora_family"] == "qwen-image"
    # video packs stay family-agnostic
    assert rows["wan22-ti2v-5b"]["lora_family"] == ""
    assert rows["minimax-h3-i2v"]["lora_family"] == ""


def test_validate_pack_lora_family(tmp_path):
    # a good slug id passes silently
    manifest = json.loads(json.dumps(GOOD_MANIFEST))
    manifest["lora_family"] = "z-image"
    report = validate_pack(write_pack(tmp_path / "a", manifest, GOOD_GRAPH))
    assert report.ok, report.errors
    assert not any("lora_family" in w for w in report.warnings)

    # non-string / empty → error
    for bad in (42, "", "   "):
        manifest = json.loads(json.dumps(GOOD_MANIFEST))
        manifest["lora_family"] = bad
        report = validate_pack(write_pack(tmp_path / f"b{bad!r}", manifest, GOOD_GRAPH))
        assert any("lora_family" in e for e in report.errors), (bad, report.errors)

    # non-slug shape → warning (open vocabulary, but flag likely typos)
    manifest = json.loads(json.dumps(GOOD_MANIFEST))
    manifest["lora_family"] = "Krea 2"
    report = validate_pack(write_pack(tmp_path / "c", manifest, GOOD_GRAPH))
    assert report.ok
    assert any("lora_family" in w for w in report.warnings)


# -- character column: import flow --------------------------------------------


def test_import_character_family_persisted(client):
    char = make_character(client, "kes", family="z-image")
    assert char["lora_family"] == "z-image"
    # unspecified stays NULL (agnostic)
    plain = make_character(client, "ash")
    assert plain["lora_family"] is None
    # "" normalizes to NULL too
    blank = make_character(client, "rin", family="")
    assert blank["lora_family"] is None
    # PATCH can set and clear
    r = client.patch(f"/api/characters/{plain['id']}", json={"lora_family": "krea2"})
    assert r.status_code == 200 and r.json()["lora_family"] == "krea2"
    r = client.patch(f"/api/characters/{plain['id']}", json={"lora_family": ""})
    assert r.status_code == 200 and r.json()["lora_family"] is None


def test_family_column_added_to_existing_db(tmp_path):
    """Upgrade path: a pre-family sqlite gets the column, rows read as NULL."""
    import sqlite3

    from storybored.db import create_db_engine, init_db

    settings = Settings(_env_file=None, data_dir=str(tmp_path / "data"), llm_base_url="")
    settings.data_path.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.execute(
        "CREATE TABLE character (id INTEGER PRIMARY KEY, name VARCHAR, handle VARCHAR,"
        " trigger VARCHAR, class_word VARCHAR, lora_name VARCHAR, lora_strength FLOAT,"
        " thumbnail_path VARCHAR, notes VARCHAR, status VARCHAR)"
    )
    conn.execute(
        "INSERT INTO character (name, handle, trigger, class_word, lora_name,"
        " lora_strength, notes, status) VALUES ('Old', 'old', 't', 'person', 'x', 1.0,"
        " '', 'trained')"
    )
    conn.commit()
    conn.close()

    engine = create_db_engine(settings)
    init_db(engine)
    with engine.connect() as c:
        from sqlalchemy import text

        row = c.execute(text("SELECT lora_family FROM character WHERE handle='old'")).one()
        assert row[0] is None  # unknown-agnostic, never a fake family


# -- render pre-flight: mismatch 409, agnostic no-block ------------------------


def test_generate_blocks_family_mismatch(client, fake_comfy):  # noqa: F811
    make_character(client, "mari", family="z-image")
    shot = make_shot(client, "WIDE: @mari by the window")
    r = client.post(
        f"/api/shots/{shot['id']}/generate", json={"workflow_id": "krea2-basic"}
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "@mari" in detail
    assert "Z-Image" in detail and "Krea 2" in detail
    assert "switch engines or remove the mention" in detail
    # nothing queued, shot untouched
    assert client.get(f"/api/shots/{shot['id']}").json()["status"] == "draft"


def test_generate_allows_matching_and_agnostic_families(client, fake_comfy):  # noqa: F811
    make_character(client, "mari", family="z-image")
    make_character(client, "null_fam")  # NULL family — agnostic
    shot = make_shot(client, "@mari and @null_fam at dawn")
    # matching family renders
    r = client.post(
        f"/api/shots/{shot['id']}/generate", json={"workflow_id": "z-image-turbo"}
    )
    assert r.status_code == 200, r.text
    assert wait_job(client, r.json()["job_id"])["status"] == "done"
    # agnostic character alone never blocks a family pack either
    shot2 = make_shot(client, "@null_fam pours coffee")
    r = client.post(
        f"/api/shots/{shot2['id']}/generate", json={"workflow_id": "krea2-basic"}
    )
    assert r.status_code == 200, r.text
    wait_job(client, r.json()["job_id"])


def test_generate_names_every_conflicting_character(client, fake_comfy):  # noqa: F811
    make_character(client, "mari", family="z-image")
    make_character(client, "juno", family="qwen-image")
    shot = make_shot(client, "@mari argues with @juno")
    r = client.post(
        f"/api/shots/{shot['id']}/generate", json={"workflow_id": "krea2-basic"}
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "@mari" in detail and "@juno" in detail
    assert "remove the mentions" in detail


def test_thumbnail_blocks_family_mismatch(client, fake_comfy):  # noqa: F811
    char = make_character(client, "mari", family="qwen-image")
    r = client.post(
        f"/api/characters/{char['id']}/generate-thumbnail",
        json={"workflow_id": "z-image-turbo"},
    )
    assert r.status_code == 409
    assert "Qwen-Image" in r.json()["detail"]
    assert "pick a compatible engine" in r.json()["detail"]
    # matching engine renders
    r = client.post(
        f"/api/characters/{char['id']}/generate-thumbnail",
        json={"workflow_id": "qwen-image-2512"},
    )
    assert r.status_code == 200, r.text
    wait_job(client, r.json()["job_id"])


# -- training targets the render engine's family -------------------------------


ARG_RECORDING_PREP = """#!/usr/bin/env bash
set -e
raw="$1"
printf '%s\\n' "$@" > prep_args.txt
name=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) name="$2"; shift 2 ;;
    *) shift ;;
  esac
done
mkdir -p "jobs/$name"
echo "# report" > "jobs/$name/report.md"
echo "[prep] done"
"""

ARG_RECORDING_TRAIN = """#!/usr/bin/env bash
set -e
job="$1"
printf '%s\\n' "$@" > train_args.txt
mkdir -p "output/$job"
echo "step 3000/3000"
: > "output/$job/$job.safetensors"
echo "training complete"
"""


def write_recording_factory(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "prep.sh").write_text(ARG_RECORDING_PREP)
    (root / "train.sh").write_text(ARG_RECORDING_TRAIN)
    for name in ("prep.sh", "train.sh"):
        os.chmod(root / name, 0o755)
    return root


def make_training_app(tmp_path, factory: Path):
    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        lora_factory_dir=str(factory),
        comfy_mode_image_cmd="",
        comfy_mode_video_cmd="",
        comfy_flush_cmd="",
        llm_base_url="",
    )
    return create_app(settings)


def run_wizard(client, handle="hero"):
    return client.post(
        "/api/characters/wizard",
        data={
            "name": "Hero",
            "handle": handle,
            "trigger": "herox7",
            "class_word": "person",
            "image_urls": "",
        },
        files=[("images", ("a.png", PNG, "image/png"))],
    )


@pytest.mark.parametrize(
    ("default_workflow", "family"),
    [
        ("", "krea2"),  # no default set → first image pack (krea2-basic) → krea2
        ("z-image-turbo", "z-image"),
        ("qwen-image-2512", "qwen-image"),
        ("wan22-ti2v-5b", "krea2"),  # family-agnostic pack → historical default
    ],
)
def test_training_family_targets_default_engine(tmp_path, default_workflow, family):
    factory = write_recording_factory(tmp_path / "factory")
    app = make_training_app(tmp_path, factory)
    with TestClient(app) as client:
        if default_workflow:
            r = client.put(
                "/api/settings",
                json={"values": {"default_image_workflow": default_workflow}},
            )
            assert r.status_code == 200, r.text

        r = run_wizard(client)
        assert r.status_code == 201, r.text
        cid = r.json()["character"]["id"]
        # stamped at wizard time already
        assert r.json()["character"]["lora_family"] == family
        prep = wait_job(client, r.json()["job_id"])
        assert prep["status"] == "done", prep
        assert json.loads(prep["payload_json"])["family"] == family
        prep_args = (factory / "prep_args.txt").read_text().splitlines()
        assert prep_args[prep_args.index("--family") + 1] == family

        r = client.post(f"/api/training/{cid}/train")
        assert r.status_code == 200, r.text
        train = wait_job(client, r.json()["job_id"])
        assert train["status"] == "done", train
        assert json.loads(train["payload_json"])["family"] == family
        train_args = (factory / "train_args.txt").read_text().splitlines()
        assert train_args[0] == "hero-v1"
        assert train_args[train_args.index("--family") + 1] == family

        char = client.get(f"/api/training/{cid}").json()["character"]
        assert char["status"] == "trained"
        assert char["lora_family"] == family


def test_train_restamps_family_when_engine_changed(tmp_path):
    """Prep under one default engine, train under another: train wins."""
    factory = write_recording_factory(tmp_path / "factory")
    app = make_training_app(tmp_path, factory)
    with TestClient(app) as client:
        r = run_wizard(client)
        assert r.status_code == 201, r.text
        cid = r.json()["character"]["id"]
        assert r.json()["character"]["lora_family"] == "krea2"
        assert wait_job(client, r.json()["job_id"])["status"] == "done"

        r = client.put(
            "/api/settings",
            json={"values": {"default_image_workflow": "z-image-turbo"}},
        )
        assert r.status_code == 200, r.text
        r = client.post(f"/api/training/{cid}/train")
        assert r.status_code == 200, r.text
        assert json.loads(
            client.get(f"/api/jobs/{r.json()['job_id']}").json()["payload_json"]
        )["family"] == "z-image"
        wait_job(client, r.json()["job_id"])
        char = client.get(f"/api/training/{cid}").json()["character"]
        assert char["lora_family"] == "z-image"


# -- setup probe: per-tier training capability ---------------------------------


def test_probe_training_note_by_tier(tmp_path, fake_comfy):  # noqa: F811
    from test_health_probes import make_client

    cases = [
        (8, ("isn't established", "import ready-made")),
        (12, ("Z-Image", "1–2 h", "2000 steps")),
        (16, ("Z-Image", "1–2 h")),
        (24, ("24 GB-class", "2.5–4 h", "3000 steps", "1–2 h")),
        (32, ("24 GB-class",)),
    ]
    with make_client(tmp_path, comfyui_url=fake_comfy.url) as client:
        for vram, fragments in cases:
            fake_comfy.state.system_stats = gpu_stats(vram)
            body = client.get("/api/setup/probe").json()
            note = body["training"]["note"]
            assert body["training"]["vram_gb"] == vram
            for fragment in fragments:
                assert fragment in note, (vram, fragment, note)


def test_probe_training_note_without_gpu(tmp_path):
    from test_health_probes import make_client

    with make_client(tmp_path, comfyui_url="") as client:
        body = client.get("/api/setup/probe").json()
        assert body["training"]["vram_gb"] is None
        assert "import" in body["training"]["note"]
