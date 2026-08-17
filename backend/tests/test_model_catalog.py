# OWNED-BY: engine-agent
"""Model catalog surfacing + the io-lane model_download job.

Downloads run against a local one-shot HTTP file server — no real network,
no real model hosts (Invariant 5)."""

import json
import os
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fake_comfy import fake_comfy  # noqa: F401 - pytest fixture

from storybored.engine.catalog import LARGE_FILE_BYTES

UNET_FILE = "krea2_raw_fp8_scaled.safetensors"


@pytest.fixture
def file_server(tmp_path):
    """Serve tmp_path/files over local HTTP; yields (url, files_dir)."""
    files_dir = tmp_path / "files"
    files_dir.mkdir()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(files_dir), **kwargs)

        def log_message(self, *args):  # keep test output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", files_dir
    finally:
        server.shutdown()
        thread.join(timeout=5.0)


def use_engine(client, fake):
    r = client.put("/api/settings", json={"values": {"comfyui_url": fake.url}})
    assert r.status_code == 200, r.text


def workflows(client):
    return {w["id"]: w for w in client.get("/api/workflows?refresh=true").json()}


def wait_job(client, job_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    job = None
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {job}")


def local_catalog(settings, url, filename, size_bytes):
    """DATA_DIR catalog override pointing a shipped filename at the local server."""
    wf_dir = settings.data_path / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "catalog.json").write_text(
        json.dumps(
            {
                filename: {
                    "source": f"{url}/{filename}",
                    "size_bytes": size_bytes,
                    "license": "test",
                    "folder": "diffusion_models",
                }
            }
        )
    )


# -- surfacing -----------------------------------------------------------------


def test_missing_models_info_from_shipped_catalog(client, fake_comfy):  # noqa: F811
    use_engine(client, fake_comfy)
    fake_comfy.state.models["UNETLoader.unet_name"] = []
    enum = fake_comfy.state.models["LoraLoader.lora_name"]
    enum.remove("lenovo_krea2.safetensors")

    rows = workflows(client)
    infos = {i["filename"]: i for i in rows["krea2-basic"]["missing_models_info"]}
    unet = infos[UNET_FILE]
    # verified entry: link + size + license + destination folder, downloadable
    assert unet["source"].startswith("https://huggingface.co/Comfy-Org/Krea-2/")
    assert unet["folder"] == "diffusion_models"
    assert unet["size_bytes"] == 13141730784
    assert "license" in unet
    assert unet["downloadable"] is True

    infos = {i["filename"]: i for i in rows["krea2-realism"]["missing_models_info"]}
    lenovo = infos["lenovo_krea2.safetensors"]
    # community entry: no URL, honest guidance instead
    assert "source" not in lenovo
    assert lenovo["downloadable"] is False
    assert lenovo["folder"] == "loras"
    assert "notes" in lenovo


def test_uncataloged_file_still_gets_folder(client, fake_comfy, settings):  # noqa: F811
    """A user pack's missing file has no catalog entry — the loader class still
    tells us the destination folder."""
    use_engine(client, fake_comfy)
    pack_dir = settings.data_path / "workflows" / "my-pack"
    pack_dir.mkdir(parents=True)
    (pack_dir / "graph.json").write_text(
        json.dumps(
            {
                "1": {"class_type": "VAELoader", "inputs": {"vae_name": "custom_vae.safetensors"}},
                "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}},
            }
        )
    )
    (pack_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "my-pack",
                "name": "Mine",
                "kind": "image",
                "graph": "graph.json",
                "parameters": [],
                "output_node": "2",
                "required_models": {"VAELoader.vae_name": ["custom_vae.safetensors"]},
            }
        )
    )
    fake_comfy.state.allow_graph_nodes(json.loads((pack_dir / "graph.json").read_text()))

    rows = workflows(client)
    (info,) = rows["my-pack"]["missing_models_info"]
    assert info["filename"] == "custom_vae.safetensors"
    assert info["folder"] == "vae"
    assert info["downloadable"] is False


# -- the downloader ------------------------------------------------------------


def test_download_models_requires_models_dir(client, fake_comfy):  # noqa: F811
    use_engine(client, fake_comfy)
    r = client.post("/api/workflows/krea2-basic/download-models", json={})
    assert r.status_code == 409
    assert "COMFY_MODELS_DIR" in r.json()["detail"]


def test_download_models_end_to_end(client, fake_comfy, settings, file_server, tmp_path):  # noqa: F811
    url, files_dir = file_server
    payload = os.urandom(256) * 8  # 2 KiB of "weights"
    (files_dir / UNET_FILE).write_bytes(payload)
    local_catalog(settings, url, UNET_FILE, len(payload))
    models_dir = tmp_path / "comfy-models"

    use_engine(client, fake_comfy)
    r = client.put("/api/settings", json={"values": {"comfy_models_dir": str(models_dir)}})
    assert r.status_code == 200, r.text
    fake_comfy.state.models["UNETLoader.unet_name"] = []

    r = client.post(
        "/api/workflows/krea2-basic/download-models", json={"filenames": [UNET_FILE]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queued"] == 1 and body["skipped"] == []
    (job_id,) = body["job_ids"]

    job_row = client.get(f"/api/jobs/{job_id}").json()
    assert job_row["lane"] == "io"  # never blocks the GPU queue
    job = wait_job(client, job_id)
    assert job["status"] == "done", job["error"]

    dest = models_dir / "diffusion_models" / UNET_FILE
    assert dest.read_bytes() == payload
    assert json.loads(job["result_json"])["file_path"] == str(dest)
    assert not dest.with_name(dest.name + ".part").exists()

    # re-posting doesn't queue a second copy of a file that's already fine:
    # the enum is still missing it (fake engine never rescans), but the job
    # short-circuits on the existing file
    r = client.post(
        "/api/workflows/krea2-basic/download-models", json={"filenames": [UNET_FILE]}
    )
    job2 = wait_job(client, r.json()["job_ids"][0])
    assert job2["status"] == "done"
    assert "already present" in job2["detail"]


def test_download_size_mismatch_fails_and_cleans_up(
    client, fake_comfy, settings, file_server, tmp_path  # noqa: F811
):
    url, files_dir = file_server
    (files_dir / UNET_FILE).write_bytes(b"short")
    local_catalog(settings, url, UNET_FILE, 999_999)  # catalog says otherwise
    models_dir = tmp_path / "comfy-models"

    use_engine(client, fake_comfy)
    client.put("/api/settings", json={"values": {"comfy_models_dir": str(models_dir)}})
    fake_comfy.state.models["UNETLoader.unet_name"] = []

    r = client.post("/api/workflows/krea2-basic/download-models", json={})
    (job_id,) = r.json()["job_ids"]
    job = wait_job(client, job_id)
    assert job["status"] == "failed"
    assert "size mismatch" in job["error"]
    dest = models_dir / "diffusion_models" / UNET_FILE
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_download_skips_community_files(client, fake_comfy, tmp_path):  # noqa: F811
    use_engine(client, fake_comfy)
    client.put(
        "/api/settings", json={"values": {"comfy_models_dir": str(tmp_path / "m")}}
    )
    enum = fake_comfy.state.models["LoraLoader.lora_name"]
    enum.remove("lenovo_krea2.safetensors")

    r = client.post("/api/workflows/krea2-realism/download-models", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queued"] == 0
    assert body["skipped"] == ["lenovo_krea2.safetensors"]


# -- big-model guardrail -------------------------------------------------------


def test_large_model_warning_on_slots(client, fake_comfy, tmp_path):  # noqa: F811
    models_dir = tmp_path / "comfy-models"
    unet_dir = models_dir / "diffusion_models"
    unet_dir.mkdir(parents=True)
    big = unet_dir / "big_unquantized_bf16.safetensors"
    with big.open("wb") as f:  # sparse file — no real 25 GB written
        f.truncate(LARGE_FILE_BYTES + 1)
    small = unet_dir / UNET_FILE
    small.write_bytes(b"x" * 1024)

    use_engine(client, fake_comfy)
    client.put("/api/settings", json={"values": {"comfy_models_dir": str(models_dir)}})
    fake_comfy.state.models["UNETLoader.unet_name"].append(
        "big_unquantized_bf16.safetensors"
    )

    rows = workflows(client)
    slot = {m["key"]: m for m in rows["krea2-basic"]["models"]}["unet"]
    assert slot["large_files"] == ["big_unquantized_bf16.safetensors"]

    # without the shared models dir there's nothing to stat — no warnings
    client.put("/api/settings", json={"values": {"comfy_models_dir": ""}})
    rows = workflows(client)
    slot = {m["key"]: m for m in rows["krea2-basic"]["models"]}["unet"]
    assert slot["large_files"] == []
