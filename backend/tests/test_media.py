"""/api/media: serves files under DATA_DIR, rejects path traversal."""

from pathlib import Path


def test_media_serves_file(client, settings):
    media_dir = Path(settings.data_dir) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "hello.txt").write_text("hi there")

    r = client.get("/api/media/media/hello.txt")
    assert r.status_code == 200
    assert r.text == "hi there"


def test_media_missing_file_404(client):
    assert client.get("/api/media/media/nope.png").status_code == 404


def test_media_rejects_traversal(client, settings, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("do not serve")

    # encoded ../ traversal — would resolve to tmp_path/secret.txt (outside DATA_DIR)
    r = client.get("/api/media/..%2Fsecret.txt")
    assert r.status_code == 404

    r = client.get("/api/media/%2e%2e/%2e%2e/etc/passwd")
    assert r.status_code == 404

    # absolute path smuggled in
    r = client.get("/api/media//etc/passwd")
    assert r.status_code == 404

    # symlink escaping DATA_DIR is followed by resolve() and rejected
    link = Path(settings.data_dir) / "leak.txt"
    link.symlink_to(secret)
    r = client.get("/api/media/leak.txt")
    assert r.status_code == 404
