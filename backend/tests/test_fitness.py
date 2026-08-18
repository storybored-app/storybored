# OWNED-BY: engine-agent
"""Hardware fit + measured speed: unit rules and the /api/workflows surface."""

import json
import time

import pytest
from fake_comfy import fake_comfy  # noqa: F401 - fixture

from storybored.config import Settings
from storybored.engine import fitness

GB = 2**30


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


CATALOG = {
    "big-unet.safetensors": {"folder": "diffusion_models", "size_bytes": 20 * GB},
    "expert-a.safetensors": {"folder": "diffusion_models", "size_bytes": 14 * GB},
    "expert-b.safetensors": {"folder": "diffusion_models", "size_bytes": 14 * GB},
    "encoder.safetensors": {"folder": "text_encoders", "size_bytes": 9 * GB},
    "vae.safetensors": {"folder": "vae", "size_bytes": 1 * GB},
    "style.safetensors": {"folder": "loras", "size_bytes": 1 * GB},
}


def test_profile_sums_residents_but_maxes_sequential_experts():
    required = {
        "UNETLoader.unet_name": ["expert-a.safetensors", "expert-b.safetensors"],
        "CLIPLoader.clip_name": ["encoder.safetensors"],
        "VAELoader.vae_name": ["vae.safetensors"],
        "LoraLoader.lora_name": ["style.safetensors"],
    }
    # experts swap (max 14), encoder+vae+lora co-resident (+11) → 25 GB peak;
    # largest single file = one 14 GB expert (the streaming floor)
    assert fitness.pack_size_profile(required, CATALOG) == (25 * GB, 14 * GB)


def test_profile_admits_ignorance_on_unknown_heavy_files():
    required = {"UNETLoader.unet_name": ["mystery.safetensors"]}
    assert fitness.pack_size_profile(required, CATALOG) is None
    # unknown VAE degrades to 0 instead of poisoning the estimate
    required = {
        "UNETLoader.unet_name": ["big-unet.safetensors"],
        "VAELoader.vae_name": ["mystery-vae.safetensors"],
    }
    assert fitness.pack_size_profile(required, CATALOG) == (20 * GB, 20 * GB)


def test_vram_budget_reads_biggest_device_and_clamps_overhead():
    stats = {
        "devices": [
            {"vram_total": 8 * GB, "vram_free": 8 * GB},
            {"vram_total": 32 * GB, "vram_free": 29 * GB},
        ]
    }
    total, usable = fitness.vram_budget(stats, base_url=f"t{time.monotonic()}")
    assert total == 32 * GB
    assert usable == 29 * GB  # 3 GB observed desktop overhead
    # a mid-render snapshot (24 GB "overhead") must not poison the budget
    busy = {"devices": [{"vram_total": 32 * GB, "vram_free": 8 * GB}]}
    total, usable = fitness.vram_budget(busy, base_url=f"u{time.monotonic()}")
    assert usable == 32 * GB - 2 * GB  # falls back to the default overhead


def test_fit_verdicts():
    budget = (32 * GB, 29 * GB)
    ok, detail = fitness.fit_verdict((12 * GB, 8 * GB), budget)
    assert ok == "ok" and detail == ""
    tight, detail = fitness.fit_verdict((30 * GB, 20 * GB), budget)  # the qwen-2512 case
    assert tight == "tight" and "paging" in detail
    exceeds, detail = fitness.fit_verdict((40 * GB, 20 * GB), budget)
    assert exceeds == "exceeds" and "exceed" in detail
    unknown, _ = fitness.fit_verdict(None, budget)
    assert unknown == "unknown"
    unknown, _ = fitness.fit_verdict((12 * GB, 8 * GB), None)
    assert unknown == "unknown"


def test_offload_friendly_streams_instead_of_warning():
    # the 8 GB card that started this: z-image-shaped pack (12 GB peak,
    # 6 GB largest file) must read "ok" — it is the tier's recommendation
    budget_8gb = (8 * GB, 7 * GB)
    ok, detail = fitness.fit_verdict((12 * GB, 6 * GB), budget_8gb, offload_friendly=True)
    assert ok == "ok" and detail == ""
    # wan-5b-shaped (10 GB largest) streams — informational, never "exceeds"
    streams, detail = fitness.fit_verdict(
        (18 * GB, 10 * GB), budget_8gb, offload_friendly=True
    )
    assert streams == "streams" and "streams layers" in detail
    # without the flag the same profile is an honest hard warning
    exceeds, _ = fitness.fit_verdict((18 * GB, 10 * GB), budget_8gb)
    assert exceeds == "exceeds"


def test_workflows_surface_fit_and_timings(client, fake_comfy, settings):  # noqa: F811
    # fabricate a healthy card so fit computes from the shipped catalog sizes
    fake_comfy.state.system_stats["devices"] = [
        {"vram_total": 48 * GB, "vram_free": 46 * GB}
    ]
    rows = {w["id"]: w for w in client.get("/api/workflows").json()}
    for row in rows.values():
        assert row["fit"] in ("ok", "streams", "tight", "exceeds", "unknown")
        assert row["median_render_s"] is None and row["timing_samples"] == 0

    # render two takes → krea2-basic gains a measured per-frame median
    project = client.post("/api/projects", json={"title": "Speed"}).json()
    scene = client.post(f"/api/projects/{project['id']}/scenes", json={"title": "S"}).json()
    shot = client.post(
        f"/api/scenes/{scene['id']}/shots", json={"description": "a quiet hallway"}
    ).json()
    r = client.post(
        f"/api/shots/{shot['id']}/generate", json={"workflow_id": "krea2-basic", "n_takes": 2}
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "done", job

    rows = {w["id"]: w for w in client.get("/api/workflows").json()}
    assert rows["krea2-basic"]["timing_samples"] == 1
    assert rows["krea2-basic"]["median_render_s"] is not None
    assert rows["krea2-basic"]["median_render_s"] >= 0
    assert rows["krea2-realism"]["median_render_s"] is None  # never rendered here
