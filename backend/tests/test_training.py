"""Trainer adapter + wizard flow against a FAKE lora-factory checkout.

Covers: fetch.py guards (via a local stdlib file server), graceful degradation
when LORA_FACTORY_DIR is unset, the full wizard e2e (wizard → prep job done →
train → character trained), user-cancel of a running train, and re-attaching
to a train that survived a backend restart.
"""

import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from storybored.config import Settings
from storybored.db import init_db
from storybored.main import create_app
from storybored.models import Character, Job
from storybored.training import fetch

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 128


@pytest.fixture(autouse=True)
def _allow_loopback_fetch(monkeypatch):
    monkeypatch.setattr(fetch, "ALLOW_PRIVATE_HOSTS", True)

# -- fake lora-factory checkout ------------------------------------------------

PREP_SH = """#!/usr/bin/env bash
set -e
raw="$1"; shift
name=""; trigger=""; classword=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) name="$2"; shift 2 ;;
    --trigger) trigger="$2"; shift 2 ;;
    --class-word) classword="$2"; shift 2 ;;
    *) shift ;;
  esac
done
echo "[prep] scanning $raw"
count=$(find "$raw" -maxdepth 1 -type f | wc -l | tr -d ' ')
echo "[prep] found $count candidate images"
echo "[prep] face filter pass"
echo "[prep] caption pass (trigger=$trigger class=$classword)"
mkdir -p "jobs/$name"
{
  echo "# Dataset report: $name"
  echo ""
  echo "- trigger: $trigger"
  echo "- class word: $classword"
  echo "- kept: $count"
  echo "- rejected: 0"
} > "jobs/$name/report.md"
echo "[prep] kept $count / $count images"
echo "[prep] done"
"""

TRAIN_SH = """#!/usr/bin/env bash
set -e
job="$1"
mkdir -p "output/$job"
echo "starting training for $job"
for s in 500 1000 1500 2000 2500 3000; do
  echo "step $s/3000 loss 0.42"
  : > "output/$job/$job-$(printf '%06d' "$s").safetensors"
  sleep {delay}
done
: > "output/$job/$job.safetensors"
echo "training complete: output/$job/$job.safetensors"
"""


def write_factory(root: Path, delay: float = 0.02) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "prep.sh").write_text(PREP_SH)
    (root / "train.sh").write_text(TRAIN_SH.format(delay=delay))
    for name in ("prep.sh", "train.sh"):
        os.chmod(root / name, 0o755)
    return root


# -- fake image file server ------------------------------------------------------


class _FileHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        route = self.server.routes.get(self.path)  # type: ignore[attr-defined]
        if route is None:
            self.send_error(404)
            return
        try:
            if route[0] == "body":
                _, ctype, body = route
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:  # chunked
                _, ctype, chunk, count = route
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for _ in range(count):
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # client bailed early (size cap) — fine


class FileServer:
    def __init__(self) -> None:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FileHandler)
        self._httpd.routes = {  # type: ignore[attr-defined]
            "/pic.jpg": ("body", "image/jpeg", JPG),
            "/pic2.png": ("body", "image/png", PNG),
            "/noext": ("body", "image/png", PNG),
            "/page.html": ("body", "text/html", b"<html>nope</html>"),
            "/big.jpg": ("body", "image/jpeg", b"x" * (fetch.MAX_BYTES + 1024)),
            "/big-stream": ("chunked", "image/jpeg", b"y" * (1024 * 1024), 12),
        }
        Thread(target=self._httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}{path}"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture(scope="module")
def fileserver():
    server = FileServer()
    yield server
    server.stop()


# -- app plumbing ----------------------------------------------------------------


def make_app(tmp_path: Path, factory: Path | None):
    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        lora_factory_dir=str(factory) if factory else "",
        comfy_mode_image_cmd="",
        comfy_mode_video_cmd="",
        comfy_flush_cmd="",
        llm_base_url="",
    )
    return create_app(settings), settings


def wait_job(client, job_id, statuses, timeout=15.0):
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = client.get(f"/api/jobs/{job_id}").json()
        if row["status"] in statuses:
            return row
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {statuses}: {row}")


def run_wizard(client, handle="hero", image_urls="", n_uploads=2):
    files = [
        ("images", (f"photo_{i}.png", PNG, "image/png")) for i in range(n_uploads)
    ]
    data = {
        "name": "Hero",
        "handle": handle,
        "trigger": "herox7",
        "class_word": "person",
        "image_urls": image_urls,
    }
    if files:
        return client.post("/api/characters/wizard", data=data, files=files)
    return client.post("/api/characters/wizard", data=data)


# -- fetch.py ---------------------------------------------------------------------


def test_fetch_downloads_image(fileserver, tmp_path):
    results = fetch.fetch_images([fileserver.url("/pic.jpg")], tmp_path / "raw")
    assert results[0]["ok"], results
    path = Path(results[0]["path"])
    assert path.is_file() and path.suffix == ".jpg"
    assert path.read_bytes() == JPG


def test_fetch_rejects_non_image_content_type(fileserver, tmp_path):
    results = fetch.fetch_images([fileserver.url("/page.html")], tmp_path / "raw")
    assert not results[0]["ok"]
    assert "not an image" in results[0]["error"]
    assert not list((tmp_path / "raw").glob("*"))


def test_fetch_rejects_oversize_content_length(fileserver, tmp_path):
    results = fetch.fetch_images([fileserver.url("/big.jpg")], tmp_path / "raw")
    assert not results[0]["ok"]
    assert "too large" in results[0]["error"]


def test_fetch_rejects_oversize_stream(fileserver, tmp_path):
    # chunked response (no content-length) must be capped mid-stream
    results = fetch.fetch_images([fileserver.url("/big-stream")], tmp_path / "raw")
    assert not results[0]["ok"]
    assert "too large" in results[0]["error"]
    assert not list((tmp_path / "raw").glob("*"))  # partial file removed


def test_fetch_ext_from_content_type(fileserver, tmp_path):
    results = fetch.fetch_images([fileserver.url("/noext")], tmp_path / "raw")
    assert results[0]["ok"]
    assert results[0]["path"].endswith(".png")


def test_fetch_bad_scheme_rejected(tmp_path):
    results = fetch.fetch_images(["ftp://example.invalid/pic.jpg"], tmp_path / "raw")
    assert not results[0]["ok"]


def test_fetch_too_many_urls(tmp_path):
    with pytest.raises(ValueError):
        fetch.fetch_images(["http://x.invalid/a.jpg"] * (fetch.MAX_FILES + 1), tmp_path / "raw")


# -- graceful degradation (LORA_FACTORY_DIR unset) ---------------------------------


def test_wizard_503_when_trainer_not_configured(client):
    r = run_wizard(client)
    assert r.status_code == 503
    assert "LORA_FACTORY_DIR" in r.json()["detail"]


def test_train_503_when_trainer_not_configured(client):
    r = client.post("/api/training/1/train")
    assert r.status_code == 503


def test_health_reports_trainer_not_configured(client):
    assert client.get("/api/health").json()["trainer"] == "not_configured"


# -- wizard validation -------------------------------------------------------------


def test_wizard_validation(tmp_path):
    factory = write_factory(tmp_path / "factory")
    app, _ = make_app(tmp_path, factory)
    with TestClient(app) as client:
        # bad handle
        r = run_wizard(client, handle="Not A Handle!")
        assert r.status_code == 400
        # no images at all
        r = run_wizard(client, handle="empty", n_uploads=0)
        assert r.status_code == 400
        assert "at least one image" in r.json()["detail"]
        # too many images
        urls = " ".join(f"http://127.0.0.1:1/{i}.jpg" for i in range(fetch.MAX_FILES + 1))
        r = run_wizard(client, handle="crowd", image_urls=urls, n_uploads=0)
        assert r.status_code == 400
        assert "too many" in r.json()["detail"]
        # duplicate handle
        r = run_wizard(client, handle="hero")
        assert r.status_code == 201, r.text
        r = run_wizard(client, handle="@HERO")  # normalizes to the same handle
        assert r.status_code == 409


# -- full wizard flow e2e ------------------------------------------------------------


def test_wizard_prep_train_e2e(tmp_path, fileserver):
    factory = write_factory(tmp_path / "factory", delay=0.02)
    app, _ = make_app(tmp_path, factory)
    with TestClient(app) as client:
        r = run_wizard(client, image_urls=fileserver.url("/pic.jpg"))
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["staged"] == 3  # 2 uploads + 1 URL
        assert body["character"]["status"] == "dataset"
        assert body["character"]["handle"] == "hero"
        cid = body["character"]["id"]

        # dataset_prep runs prep.sh, streams a stdout tail, stores report.md
        prep = wait_job(client, body["job_id"], {"done", "failed"})
        assert prep["status"] == "done", prep
        assert "[prep]" in prep["detail"]
        result = json.loads(prep["result_json"])
        assert result["job_name"] == "hero-v1"
        assert "kept: 3" in result["report_md"]

        # status endpoint: report + samples + job states
        st = client.get(f"/api/training/{cid}").json()
        assert "kept: 3" in st["report_md"]
        assert len(st["samples"]) == 3
        assert st["prep_job"]["status"] == "done"
        assert st["train_job"] is None

        # explicit train step
        r = client.post(f"/api/training/{cid}/train")
        assert r.status_code == 200, r.text
        train_id = r.json()["job_id"]

        # double-start guarded while queued/running
        assert client.post(f"/api/training/{cid}/train").status_code == 409

        row = wait_job(client, train_id, {"done", "failed"}, timeout=30)
        assert row["status"] == "done", row
        result = json.loads(row["result_json"])
        assert result["lora_name"] == "lorafactory_hero-v1/hero-v1.safetensors"
        assert (factory / "output" / "hero-v1" / "hero-v1.safetensors").is_file()

        st = client.get(f"/api/training/{cid}").json()
        assert st["character"]["status"] == "trained"
        assert st["character"]["lora_name"] == "lorafactory_hero-v1/hero-v1.safetensors"
        assert st["character"]["lora_strength"] == 1.0
        # pidfile is cleaned up after a completed train
        assert not list((Path(tmp_path) / "data" / "training" / "pids").glob("*.json"))


def test_train_requires_finished_prep(tmp_path):
    factory = write_factory(tmp_path / "factory")
    app, _ = make_app(tmp_path, factory)
    with TestClient(app) as client:
        with Session(app.state.engine, expire_on_commit=False) as session:
            character = Character(name="Rey", handle="rey", status="ready")
            session.add(character)
            session.commit()
            session.refresh(character)
        r = client.post(f"/api/training/{character.id}/train")
        assert r.status_code == 409
        assert "prep" in r.json()["detail"]


# -- cancel + restart survival --------------------------------------------------------


def _wizard_prep_train(client, settings):
    """Inside a live client: wizard → prep done → slow train running.

    Returns (character_id, train_job_id, pidfile_info)."""
    r = run_wizard(client)
    assert r.status_code == 201, r.text
    cid = r.json()["character"]["id"]
    prep = wait_job(client, r.json()["job_id"], {"done", "failed"})
    assert prep["status"] == "done", prep
    r = client.post(f"/api/training/{cid}/train")
    assert r.status_code == 200, r.text
    train_id = r.json()["job_id"]
    wait_job(client, train_id, {"running", "done", "failed"})

    # wait for the pidfile — proof the trainer subprocess is up
    pids_dir = settings.data_path / "training" / "pids"
    deadline = time.monotonic() + 10
    pidfiles: list[Path] = []
    while time.monotonic() < deadline:
        pidfiles = list(pids_dir.glob("*.json"))
        if pidfiles:
            break
        time.sleep(0.05)
    assert pidfiles, "pidfile never appeared"
    info = json.loads(pidfiles[0].read_text())
    return cid, train_id, info


def _pid_alive(pid: int) -> bool:
    try:
        done_pid, _ = os.waitpid(pid, os.WNOHANG)
        return done_pid != pid  # our zombie child counts as dead
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_cancel_running_train_kills_process(tmp_path):
    factory = write_factory(tmp_path / "factory", delay=0.9)
    app, settings = make_app(tmp_path, factory)
    with TestClient(app) as client:
        cid, train_id, info = _wizard_prep_train(client, settings)
        assert client.post(f"/api/jobs/{train_id}/cancel").status_code == 200
        row = wait_job(client, train_id, {"cancelled", "done", "failed"})
        assert row["status"] == "cancelled", row
        st = client.get(f"/api/training/{cid}").json()
        assert st["character"]["status"] == "dataset"  # back to reviewable state
        assert not list((settings.data_path / "training" / "pids").glob("*.json"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _pid_alive(info["pid"]):
        time.sleep(0.1)
    assert not _pid_alive(info["pid"]), "train process survived cancel"


def test_train_survives_backend_restart(tmp_path):
    # phase 1: start a slow train, then shut the backend down mid-train:
    # exiting the TestClient context runs lifecycle shutdown and cancels the
    # runner task — but the trainer process must keep running
    # (start_new_session + pidfile + log-file stdout).
    factory = write_factory(tmp_path / "factory", delay=0.9)
    app1, settings = make_app(tmp_path, factory)
    with TestClient(app1) as client:
        cid, train_id, info = _wizard_prep_train(client, settings)
    assert _pid_alive(info["pid"]), "train process died with the backend"

    # phase 2: fresh backend on the same DATA_DIR re-attaches and finishes.
    app2 = create_app(settings)
    with TestClient(app2) as client:
        client.get(f"/api/training/{cid}")  # triggers recover_orphan_trains
        deadline = time.monotonic() + 40
        status = None
        while time.monotonic() < deadline:
            status = client.get(f"/api/training/{cid}").json()
            if status["character"]["status"] == "trained":
                break
            time.sleep(0.2)
        assert status is not None and status["character"]["status"] == "trained", status
        assert (
            status["character"]["lora_name"] == "lorafactory_hero-v1/hero-v1.safetensors"
        )
        train_job = status["train_job"]
        assert train_job is not None and train_job["status"] == "done"
        assert not list((settings.data_path / "training" / "pids").glob("*.json"))


# -- cancelling a re-attach train sticks -------------------------------------------


def test_cancelled_queued_reattach_train_stays_cancelled(tmp_path):
    # A train resurrected after a restart sits 'queued' behind the busy GPU lane
    # with a LIVE trainer process behind its pidfile. Cancelling it while queued
    # must actually kill that process and drop the pidfile, so recovery cannot
    # resurrect a train the user explicitly stopped.
    from storybored.training.lora_factory import recover_orphan_trains

    factory = write_factory(tmp_path / "factory")
    app, settings = make_app(tmp_path, factory)
    init_db(app.state.engine)
    runner = app.state.runner  # workers never started → the job stays queued

    # a real long-lived orphan "trainer"
    proc = subprocess.Popen(["bash", "-c", "sleep 60"], start_new_session=True)
    pid = proc.pid
    try:
        job_name = "ghost-v1"
        pids = settings.data_path / "training" / "pids"
        pids.mkdir(parents=True, exist_ok=True)
        with Session(app.state.engine, expire_on_commit=False) as s:
            job = Job(
                type="lora_train",
                status="queued",
                lane="gpu",
                payload_json=json.dumps(
                    {"job_name": job_name, "reattach": True, "pid": pid}
                ),
            )
            s.add(job)
            s.commit()
            s.refresh(job)
            jid = job.id
        (pids / f"{job_name}.json").write_text(
            json.dumps(
                {
                    "pid": pid,
                    "job_id": jid,
                    "job_name": job_name,
                    "character_id": None,
                    "output_dir": str(factory / "output" / job_name),
                    "log_file": str(settings.data_path / "training" / "logs" / f"{job_name}.log"),
                }
            )
        )

        # user cancels while queued
        cancelled = runner.cancel(jid)
        assert cancelled is not None and cancelled.status == "cancelled"

        # the orphan trainer is killed and its pidfile removed
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        assert not _pid_alive(pid), "orphan trainer survived cancel"
        assert not list(pids.glob("*.json")), "pidfile not removed on cancel"

        # recovery must NOT resurrect it
        actions = recover_orphan_trains(runner)
        assert all(a.get("action") != "reattached" for a in actions), actions
        with Session(app.state.engine, expire_on_commit=False) as s:
            assert s.get(Job, jid).status == "cancelled"  # sticks
            trains = list(s.exec(select(Job).where(Job.type == "lora_train")))
            assert all(r.status == "cancelled" for r in trains)  # nothing re-queued
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# -- live-step parsing --------------------------------------------------------------


def test_step_re_parses_tqdm_and_bare_tokens():
    from storybored.training.lora_factory import STEP_RE

    assert STEP_RE.findall("step 500/3000 loss 0.42")[-1] == ("500", "3000")
    # ai-toolkit tqdm line
    assert STEP_RE.findall("hero-v1: 19/3000 [3.5s/it, loss=0.1]")[-1] == ("19", "3000")
    # bare N/TOTAL token
    assert STEP_RE.findall("progress 2750/3000")[-1] == ("2750", "3000")


def test_train_progress_tracks_tqdm_line(tmp_path):
    from storybored.training.lora_factory import _train_progress

    log_file = tmp_path / "train.log"
    log_file.write_text("loading model\nmychar: 1500/3000 [1.2s/it]\n")
    progress, detail = _train_progress(log_file, tmp_path / "no_output", "mychar")
    assert abs(progress - 0.5) < 0.01  # live steps, not just checkpoint jumps
    assert "1500/3000" in detail
