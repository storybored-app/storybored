# OWNED-BY: llm-agent
"""LLM probe authentication.

When an API key is configured, /api/health and /api/setup/probe send it as
``Authorization: Bearer …`` so key-protected hosted providers report "ok"
instead of "unrecognized". The key is only ever released to the configured
base URL's host (candidate URLs typed into the wizard can't siphon it), it
is never accepted as a query param, and 401/403 map to "unauthorized".
"""

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from test_health_probes import make_client, serve

KEY = "sk-test-sekret"


def key_gated_app(seen_auth_headers: list):
    """An OpenAI-shaped /v1/models that 401s unless it gets the right key.

    Every request's Authorization header (or None) lands in the given list."""

    async def h(request):
        seen_auth_headers.append(request.headers.get("authorization"))
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if request.path_params["path"] == "v1/models":
            return JSONResponse({"object": "list", "data": [{"id": "hosted-model"}]})
        return PlainTextResponse("nope", status_code=404)

    return Starlette(routes=[Route("/{path:path}", h, methods=["GET"])])


def test_health_sends_configured_key(tmp_path):
    seen: list = []
    with serve(key_gated_app(seen)) as url:
        with make_client(tmp_path, llm_base_url=f"{url}/v1", llm_api_key=KEY) as client:
            assert client.get("/api/health").json()["llm"] == "ok"
    assert seen[-1] == f"Bearer {KEY}"


def test_health_no_key_is_unauthorized_not_unrecognized(tmp_path):
    seen: list = []
    with serve(key_gated_app(seen)) as url:
        with make_client(tmp_path, llm_base_url=f"{url}/v1") as client:
            assert client.get("/api/health").json()["llm"] == "unauthorized"
    assert seen[-1] is None


def test_setup_probe_effective_settings_send_key(tmp_path):
    seen: list = []
    with serve(key_gated_app(seen)) as url:
        with make_client(tmp_path, llm_base_url=f"{url}/v1", llm_api_key=KEY) as client:
            llm = client.get("/api/setup/probe").json()["llm"]
    assert llm["status"] == "ok"
    assert llm["models"] == ["hosted-model"]
    assert seen[-1] == f"Bearer {KEY}"


def test_setup_probe_candidate_on_same_host_uses_saved_key(tmp_path):
    seen: list = []
    with serve(key_gated_app(seen)) as url:
        with make_client(tmp_path, llm_base_url=f"{url}/v1", llm_api_key=KEY) as client:
            r = client.get("/api/setup/probe", params={"llm_url": f"{url}/v1"})
    assert r.json()["llm"]["status"] == "ok"
    assert seen[-1] == f"Bearer {KEY}"


def test_setup_probe_candidate_on_other_host_never_gets_key(tmp_path):
    """A candidate URL pointing anywhere but the configured host is probed
    WITHOUT the stored secret — and a key query param is ignored, never used."""
    seen_a: list = []
    seen_b: list = []
    with serve(key_gated_app(seen_a)) as url_a, serve(key_gated_app(seen_b)) as url_b:
        with make_client(tmp_path, llm_base_url=f"{url_a}/v1", llm_api_key=KEY) as client:
            r = client.get(
                "/api/setup/probe",
                # the extra params must be inert: keys are never accepted this way
                params={"llm_url": f"{url_b}/v1", "llm_key": KEY, "api_key": KEY},
            )
    assert r.json()["llm"]["status"] == "unauthorized"
    assert seen_b == [None]
    assert seen_a == []  # only the candidate was probed


def test_override_base_url_does_not_leak_env_key(tmp_path):
    """The llm/client.py key-exfil guard carries into probes: a runtime
    override base URL never receives the env-provided key."""
    seen_a: list = []
    seen_b: list = []
    with serve(key_gated_app(seen_a)) as url_a, serve(key_gated_app(seen_b)) as url_b:
        with make_client(tmp_path, llm_base_url=f"{url_a}/v1", llm_api_key=KEY) as client:
            r = client.put(
                "/api/settings", json={"values": {"llm_base_url": f"{url_b}/v1"}}
            )
            assert r.status_code == 200
            assert client.get("/api/health").json()["llm"] == "unauthorized"
    assert seen_b == [None]
