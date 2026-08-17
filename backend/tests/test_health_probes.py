"""Strict /api/health probes, the not-built frontend page, and the
comfy_loras_dir / setup_complete settings keys.

A random web server answering on the configured port must never make a
component report "ok" — the probes check the response actually looks like
the expected service.
"""

import socket
import threading
import time
from contextlib import contextmanager

import uvicorn
from fake_comfy import fake_comfy  # noqa: F401 - fixture
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from storybored.config import Settings
from storybored.main import create_app


@contextmanager
def serve(app: Starlette):
    """Run a starlette app on an ephemeral localhost port; yield its URL."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", access_log=False, lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server failed to start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


def catch_all_app(handler) -> Starlette:
    return Starlette(routes=[Route("/{path:path}", handler, methods=["GET"])])


def unused_port_url() -> str:
    """A URL nothing listens on (bound then closed → connection refused)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def make_client(tmp_path, **overrides) -> TestClient:
    kwargs = {"llm_base_url": "", "lora_factory_dir": "", **overrides}
    settings = Settings(_env_file=None, data_dir=str(tmp_path / "data"), **kwargs)
    return TestClient(create_app(settings))


# ---------------------------------------------------------------- comfy probe


def test_comfy_ok_against_real_shape(tmp_path, fake_comfy):  # noqa: F811
    with make_client(tmp_path, comfyui_url=fake_comfy.url) as client:
        assert client.get("/api/health").json()["comfy"] == "ok"


def test_comfy_404_server_is_unrecognized(tmp_path):
    async def h(request):
        return PlainTextResponse("nope", status_code=404)

    with serve(catch_all_app(h)) as url, make_client(tmp_path, comfyui_url=url) as client:
        assert client.get("/api/health").json()["comfy"] == "unrecognized"


def test_comfy_non_json_200_is_unrecognized(tmp_path):
    async def h(request):
        return PlainTextResponse("<html>hello</html>")

    with serve(catch_all_app(h)) as url, make_client(tmp_path, comfyui_url=url) as client:
        assert client.get("/api/health").json()["comfy"] == "unrecognized"


def test_comfy_wrong_json_shape_is_unrecognized(tmp_path):
    async def h(request):
        return JSONResponse({"hello": "world"})

    with serve(catch_all_app(h)) as url, make_client(tmp_path, comfyui_url=url) as client:
        assert client.get("/api/health").json()["comfy"] == "unrecognized"


def test_comfy_5xx_is_error(tmp_path):
    async def h(request):
        return PlainTextResponse("boom", status_code=500)

    with serve(catch_all_app(h)) as url, make_client(tmp_path, comfyui_url=url) as client:
        assert client.get("/api/health").json()["comfy"] == "error"


def test_comfy_unreachable(tmp_path):
    with make_client(tmp_path, comfyui_url=unused_port_url()) as client:
        assert client.get("/api/health").json()["comfy"] == "unreachable"


# ------------------------------------------------------------------ llm probe


def test_llm_models_200_is_ok(tmp_path):
    async def h(request):
        if request.path_params["path"] == "v1/models":
            return JSONResponse({"object": "list", "data": [{"id": "some-model"}]})
        return PlainTextResponse("nope", status_code=404)

    with serve(catch_all_app(h)) as url:
        with make_client(tmp_path, llm_base_url=f"{url}/v1") as client:
            assert client.get("/api/health").json()["llm"] == "ok"


def test_llm_404_on_models_is_unrecognized(tmp_path):
    """A bare web server (or the wrong base path) must not report ok."""

    async def h(request):
        return PlainTextResponse("nope", status_code=404)

    with serve(catch_all_app(h)) as url:
        with make_client(tmp_path, llm_base_url=f"{url}/v1") as client:
            assert client.get("/api/health").json()["llm"] == "unrecognized"


def test_llm_unreachable(tmp_path):
    with make_client(tmp_path, llm_base_url=unused_port_url()) as client:
        assert client.get("/api/health").json()["llm"] == "unreachable"


# -------------------------------------------------------------- trainer probe


def test_trainer_dir_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "trainer").mkdir()
    with make_client(tmp_path, lora_factory_dir="~/trainer") as client:
        assert client.get("/api/health").json()["trainer"] == "ok"


# ------------------------------------------------------- not-built front page


def test_missing_dist_serves_help_page(tmp_path, monkeypatch, settings):
    import storybored.main as main_mod

    monkeypatch.setattr(main_mod, "FRONTEND_DIST", tmp_path / "dist")
    with TestClient(create_app(settings)) as client:
        for path in ("/", "/settings"):
            resp = client.get(path)
            assert resp.status_code == 200
            assert "npm --prefix frontend run build" in resp.text
        # the API stays fully alive behind the help page
        assert client.get("/api/projects").status_code == 200


def test_built_dist_is_served_without_restart(tmp_path, monkeypatch, settings):
    import storybored.main as main_mod

    dist = tmp_path / "dist"
    monkeypatch.setattr(main_mod, "FRONTEND_DIST", dist)
    with TestClient(create_app(settings)) as client:
        assert "npm --prefix" in client.get("/").text
        # build lands while the server runs → next request serves the real app
        dist.mkdir()
        (dist / "index.html").write_text("<html>the real app</html>", encoding="utf-8")
        assert client.get("/").text == "<html>the real app</html>"


# --------------------------------------------- new runtime-editable settings


def test_comfy_loras_dir_and_setup_complete_are_overridable(client):
    resp = client.put(
        "/api/settings",
        json={"values": {"comfy_loras_dir": "~/loras", "setup_complete": "1"}},
    )
    assert resp.status_code == 200
    effective = client.get("/api/settings").json()["effective"]
    assert effective["comfy_loras_dir"] == "~/loras"
    assert effective["setup_complete"] == "1"


def test_unknown_setting_key_still_rejected(client):
    resp = client.put("/api/settings", json={"values": {"comfy_loras_dirs": "x"}})
    assert resp.status_code == 400


def test_import_lora_uses_db_override_and_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with make_client(tmp_path) as client:  # env comfy_loras_dir unset
        client.put("/api/settings", json={"values": {"comfy_loras_dir": "~/loras"}})
        resp = client.post(
            "/api/characters/import-lora",
            files={"file": ("hero.safetensors", b"fake-weights", "application/octet-stream")},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"lora_name": "hero.safetensors", "imported": True}
        assert (tmp_path / "loras" / "hero.safetensors").read_bytes() == b"fake-weights"


def test_import_lora_unconfigured_dir_points_at_settings(tmp_path):
    with make_client(tmp_path) as client:
        resp = client.post(
            "/api/characters/import-lora",
            files={"file": ("hero.safetensors", b"x", "application/octet-stream")},
        )
        assert resp.status_code == 400
        assert "Settings" in resp.json()["detail"]
