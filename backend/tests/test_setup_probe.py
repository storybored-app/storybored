"""GET /api/setup/probe — GPU/VRAM tier derivation, model list, candidate URLs.

The endpoint must never invent hardware numbers: everything comes from the
engine's /system_stats payload (faked here), and a missing/odd payload lands
in the conservative "board" tier.
"""

from fake_comfy import fake_comfy  # noqa: F401 - fixture
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from test_health_probes import make_client, serve, unused_port_url

from storybored.api.health import derive_tier, parse_gpus

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


def test_tier_video_at_24gb():
    assert derive_tier(parse_gpus(gpu_stats(23.99))) == "video"


def test_tier_stills_at_16gb():
    # a "16 GB" card typically reports a hair under 16 GiB — must still qualify
    assert derive_tier(parse_gpus(gpu_stats(15.99))) == "stills"


def test_tier_board_below_16gb():
    assert derive_tier(parse_gpus(gpu_stats(11.99))) == "board"


def test_tier_board_when_no_devices_reported():
    assert derive_tier(parse_gpus({"system": {}})) == "board"


def test_tier_board_when_cpu_only():
    stats = {"system": {}, "devices": [{"name": "cpu", "type": "cpu"}]}
    assert parse_gpus(stats) == []
    assert derive_tier([]) == "board"


def test_tier_uses_best_gpu():
    assert derive_tier(parse_gpus(gpu_stats(8.0, 24.0))) == "video"


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
    assert body["comfy"]["tier"] == "video"
    assert body["comfy"]["gpus"] == [{"name": "cuda:0 Test GPU 24G", "vram_gb": 24.0}]
    # shipped packs validated against the fake's enums (allow_pack_models())
    by_id = {w["id"]: w for w in body["workflows"]}
    assert by_id["krea2-basic"]["available"] is True
    assert by_id["krea2-basic"]["kind"] == "image"
    assert body["tiers"] == {"stills_min_vram_gb": 16, "video_min_vram_gb": 24}
    assert body["trainer"]["status"] == "not_configured"


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
