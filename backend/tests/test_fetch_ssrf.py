"""SSRF guard for the dataset image importer (fetch.py).

The production guard stays ON here (ALLOW_PRIVATE_HOSTS defaults False), so
these assert the real blocking behaviour — including a redirect that bounces
to an internal address.
"""

from urllib.parse import urlsplit

import httpx

from storybored.training import fetch


def test_fetch_rejects_loopback_url(tmp_path):
    results = fetch.fetch_images(["http://127.0.0.1:9/secret.jpg"], tmp_path / "raw")
    assert not results[0]["ok"]
    assert "private/loopback" in results[0]["error"]
    assert not list((tmp_path / "raw").glob("*"))


def test_fetch_rejects_cloud_metadata_ip(tmp_path):
    # 169.254.169.254 is link-local (cloud metadata endpoint) — must be blocked.
    results = fetch.fetch_images(
        ["http://169.254.169.254/latest/meta-data/"], tmp_path / "raw"
    )
    assert not results[0]["ok"]
    assert "private/loopback" in results[0]["error"]


def test_fetch_rejects_non_http_scheme(tmp_path):
    results = fetch.fetch_images(["file:///etc/passwd"], tmp_path / "raw")
    assert not results[0]["ok"]
    assert "http(s)" in results[0]["error"]


def test_fetch_rejects_redirect_to_private(tmp_path, monkeypatch):
    """A safe-looking first URL that 302-redirects to an internal IP is blocked
    at the redirect hop (manual redirect re-validation)."""
    real_check = fetch._check_url

    def fake_check(url: str):
        # Treat only the (fake) public first host as safe; every other host —
        # including the redirect target — goes through the real IP guard.
        if urlsplit(url).hostname == "images.example":
            return None
        return real_check(url)

    monkeypatch.setattr(fetch, "_check_url", fake_check)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "images.example"  # never reaches the private host
        return httpx.Response(
            302, headers={"Location": "http://169.254.169.254/secret.jpg"}
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    try:
        result = fetch._fetch_one(
            client, "http://images.example/pic.jpg", tmp_path, 0
        )
    finally:
        client.close()

    assert not result["ok"]
    assert "private/loopback" in result["error"]
    assert not list(tmp_path.glob("*"))
