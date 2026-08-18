"""GET /api/setup/probe — GPU/VRAM tier derivation, model list, candidate URLs.

The endpoint must never invent hardware numbers: everything comes from the
engine's /system_stats payload (faked here), and a missing/odd payload lands
in the conservative "board" tier.
"""

import pytest
from fake_comfy import fake_comfy  # noqa: F401 - fixture
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from test_health_probes import make_client, serve, unused_port_url

from storybored.api.health import derive_tier, parse_gpus, recommended_llm_tag

GIB = 2**30


def gpu_stats(*vram_gib: float, name: str = "cuda:0 Test GPU : cudaMallocAsync") -> dict:
    return {
        "system": {"os": "fake"},
        "devices": [
            {"name": name, "type": "cuda", "index": i, "vram_total": int(v * GIB)}
            for i, v in enumerate(vram_gib)
        ],
    }


# ------------------------------------------------------------- tier derivation


@pytest.mark.parametrize(
    ("vram_gib", "tier"),
    [
        # cards typically report a hair under their marketing size — rounding
        # to whole GiB must keep them in their tier
        (23.99, "studio"),
        (32.0, "studio"),
        (23.4, "stills-hd"),
        (15.99, "stills-hd"),
        (15.4, "stills"),
        (11.99, "stills"),
        (11.4, "stills-lite"),
        (8.0, "stills-lite"),
        (5.99, "stills-lite"),
        (5.4, "board"),
        (4.0, "board"),
    ],
)
def test_tier_boundaries(vram_gib, tier):
    assert derive_tier(parse_gpus(gpu_stats(vram_gib))) == tier


def test_tier_board_when_no_devices_reported():
    assert derive_tier(parse_gpus({"system": {}})) == "board"


def test_tier_board_when_cpu_only():
    stats = {"system": {}, "devices": [{"name": "cpu", "type": "cpu"}]}
    assert parse_gpus(stats) == []
    assert derive_tier([]) == "board"


def test_tier_uses_best_gpu():
    assert derive_tier(parse_gpus(gpu_stats(8.0, 24.0))) == "studio"


@pytest.mark.parametrize(
    ("vram_gib", "tag"),
    [(None, "qwen3.5:4b"), (8.0, "qwen3.5:4b"), (11.99, "qwen3.5:9b"),
     (24.0, "qwen3.5:9b"), (31.99, "qwen3.5:35b-a3b"), (48.0, "qwen3.5:35b-a3b")],
)
def test_recommended_llm_tag_by_vram(vram_gib, tag):
    assert recommended_llm_tag(vram_gib) == tag


def test_gpu_without_vram_number_stays_honest():
    stats = {"system": {}, "devices": [{"name": "cuda:0 Mystery GPU", "type": "cuda"}]}
    gpus = parse_gpus(stats)
    assert gpus == [{"name": "cuda:0 Mystery GPU", "vram_gb": None}]
    assert derive_tier(gpus) == "board"


def test_gpu_name_drops_allocator_suffix():
    gpus = parse_gpus(gpu_stats(24.0, name="cuda:0 Big Card : cudaMallocAsync"))
    assert gpus[0]["name"] == "cuda:0 Big Card"


# ------------------------------------------------------------------- endpoint


def test_probe_reports_gpu_tier_and_packs(tmp_path, fake_comfy):  # noqa: F811
    fake_comfy.state.system_stats = gpu_stats(24.0, name="cuda:0 Test GPU 24G")
    with make_client(tmp_path, comfyui_url=fake_comfy.url) as client:
        body = client.get("/api/setup/probe").json()
    assert body["comfy"]["status"] == "ok"
    assert body["comfy"]["tier"] == "studio"
    assert body["comfy"]["gpus"] == [{"name": "cuda:0 Test GPU 24G", "vram_gb": 24.0}]
    # shipped packs validated against the fake's enums (allow_pack_models())
    by_id = {w["id"]: w for w in body["workflows"]}
    assert by_id["krea2-basic"]["available"] is True
    assert by_id["krea2-basic"]["kind"] == "image"
    assert by_id["minimax-h3-i2v"]["license_note"] != ""
    assert by_id["z-image-turbo"]["license_note"] == ""
    assert body["tiers"] == {
        "studio": 24, "stills-hd": 16, "stills": 12, "stills-lite": 6,
    }
    assert body["trainer"]["status"] == "not_configured"


# ---------------------------------------------------------- recommended block


@pytest.mark.parametrize(
    ("vram_gib", "tier", "image_id", "video_id", "llm"),
    [
        (8.0, "stills-lite", "z-image-turbo", None, "qwen3.5:4b"),
        (12.0, "stills", "z-image-turbo", "wan22-ti2v-5b", "qwen3.5:9b"),
        (16.0, "stills-hd", "krea2-basic", "wan22-ti2v-5b", "qwen3.5:9b"),
        (24.0, "studio", "qwen-image-2512", "wan22-i2v-14b", "qwen3.5:9b"),
        (32.0, "studio", "qwen-image-2512", "wan22-i2v-14b", "qwen3.5:35b-a3b"),
    ],
)
def test_probe_recommends_per_tier(
    tmp_path, fake_comfy, vram_gib, tier, image_id, video_id, llm  # noqa: F811
):
    fake_comfy.state.system_stats = gpu_stats(vram_gib)
    with make_client(tmp_path, comfyui_url=fake_comfy.url) as client:
        rec = client.get("/api/setup/probe").json()["recommended"]
    assert rec["tier"] == tier
    assert rec["llm"] == llm
    assert rec["image"]["id"] == image_id
    if video_id is None:
        assert rec["video"] is None
    else:
        assert rec["video"]["id"] == video_id
    # the fake allows every shipped pack's models → nothing to download
    assert rec["image"]["available"] is True
    assert rec["image"]["missing_models"] == []
    assert rec["image"]["download_bytes"] == 0


def test_probe_recommendation_carries_download_size(tmp_path, fake_comfy):  # noqa: F811
    """Missing files roll up to a catalog-verified download size so the wizard
    can say what one click will fetch."""
    fake_comfy.state.system_stats = gpu_stats(12.0)
    fake_comfy.state.models["UNETLoader.unet_name"] = []
    fake_comfy.state.models["CLIPLoader.clip_name"] = []
    with make_client(tmp_path, comfyui_url=fake_comfy.url) as client:
        rec = client.get("/api/setup/probe").json()["recommended"]
    image = rec["image"]
    assert image["id"] == "z-image-turbo"
    assert image["available"] is False
    assert set(image["missing_models"]) == {
        "z_image_turbo_int8_convrot.safetensors",
        "qwen_3_4b_fp8_mixed.safetensors",
    }
    # exact sum of the two files' catalog sizes (HF-API-verified bytes)
    assert image["download_bytes"] == 6201001296 + 5631994051
    assert image["downloadable"] is True


def test_probe_no_recommendation_when_engine_down(tmp_path):
    with make_client(tmp_path, comfyui_url=unused_port_url()) as client:
        body = client.get("/api/setup/probe").json()
    assert body["recommended"] is None


def test_probe_board_tier_recommends_no_packs(tmp_path, fake_comfy):  # noqa: F811
    fake_comfy.state.system_stats = gpu_stats(4.0)
    with make_client(tmp_path, comfyui_url=fake_comfy.url) as client:
        rec = client.get("/api/setup/probe").json()["recommended"]
    assert rec["tier"] == "board"
    assert rec["image"] is None and rec["video"] is None
    assert rec["llm"] == "qwen3.5:4b"


def test_probe_missing_models_flagged(tmp_path, fake_comfy):  # noqa: F811
    # wipe one enum → that pack's models go missing, pack flagged not hidden
    fake_comfy.state.models["UNETLoader.unet_name"] = []
    with make_client(tmp_path, comfyui_url=fake_comfy.url) as client:
        body = client.get("/api/setup/probe").json()
    unavailable = [w for w in body["workflows"] if not w["available"]]
    assert unavailable and all(w["missing_models"] for w in unavailable)


def test_probe_candidate_url_overrides_settings(tmp_path, fake_comfy):  # noqa: F811
    # settings point nowhere; the wizard probes a candidate URL via query param
    with make_client(tmp_path, comfyui_url=unused_port_url()) as client:
        assert client.get("/api/setup/probe").json()["comfy"]["status"] == "unreachable"
        body = client.get("/api/setup/probe", params={"comfy_url": fake_comfy.url}).json()
    assert body["comfy"]["status"] == "ok"
    assert body["comfy"]["url"] == fake_comfy.url


def test_probe_down_engine_is_board_tier_with_no_workflows(tmp_path):
    with make_client(tmp_path, comfyui_url=unused_port_url()) as client:
        body = client.get("/api/setup/probe").json()
    assert body["comfy"]["status"] == "unreachable"
    assert body["comfy"]["tier"] == "board"
    assert body["comfy"]["gpus"] == []
    assert body["workflows"] == []


def test_probe_llm_models_and_candidate_url(tmp_path):
    async def h(request):
        if request.path_params["path"] == "v1/models":
            return JSONResponse({"object": "list", "data": [{"id": "small"}, {"id": "big"}]})
        return JSONResponse({}, status_code=404)

    app = Starlette(routes=[Route("/{path:path}", h, methods=["GET"])])
    with serve(app) as url, make_client(tmp_path) as client:
        body = client.get("/api/setup/probe", params={"llm_url": f"{url}/v1"}).json()
    assert body["llm"]["status"] == "ok"
    assert body["llm"]["models"] == ["small", "big"]


def test_probe_trainer_candidate_dir(tmp_path):
    trainer = tmp_path / "trainer"
    trainer.mkdir()
    with make_client(tmp_path) as client:
        ok = client.get("/api/setup/probe", params={"trainer_dir": str(trainer)}).json()
        missing = client.get(
            "/api/setup/probe", params={"trainer_dir": str(tmp_path / "nope")}
        ).json()
    assert ok["trainer"]["status"] == "ok"
    assert missing["trainer"]["status"] == "missing"
