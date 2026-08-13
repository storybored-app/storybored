# OWNED-BY: training-agent
"""lora-factory trainer adapter: dataset_prep + lora_train job handlers.

Wraps an external lora-factory checkout (LORA_FACTORY_DIR, DB-overridable).
When unset the trainer degrades cleanly: /api/health reports "not_configured"
and the wizard/train endpoints return 503 with a friendly detail.

dataset_prep — `bash prep.sh <staging_dir> --name <job> --trigger <t>
--class-word <w>` with cwd=LORA_FACTORY_DIR; the stdout tail (last ~5 lines)
streams into job.detail; on success `jobs/<job>/report.md` is read into
result_json for the wizard's review screen.

lora_train — `bash train.sh <job>` started with start_new_session=True and
stdout redirected to a log file, plus a pidfile under DATA_DIR, so a backend
restart does NOT kill a 3-hour train: on recovery (see
`recover_orphan_trains`, called by the training API), a live pidfile
re-attaches by polling the pid + output dir instead of failing the run.
Progress comes from `step N/3000` stdout lines when present, else from
counting step-numbered checkpoints in the output dir.
"""

import asyncio
import json
import logging
import os
import re
import signal
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session

from storybored.api.settings_api import effective_setting
from storybored.config import Settings
from storybored.jobs.registry import register
from storybored.jobs.runner import JobCancelled
from storybored.models import Character, Job

log = logging.getLogger("storybored.training")

TOTAL_STEPS = 3000
TAIL_LINES = 5
POLL_S = 1.0

# Matches live-step lines across trainer flavors:
#   "step 500/3000"            (lora-factory)
#   "hero-v1: 19/3000 [3.5s/it]" (ai-toolkit tqdm)
#   a bare "19/3000" token
# The "step"/label prefix is optional so a plain N/TOTAL still counts.
STEP_RE = re.compile(r"(?:step[ :=]+)?(\d+)\s*/\s*(\d+)", re.IGNORECASE)


class TrainerNotConfigured(RuntimeError):
    pass


# -- config / paths -----------------------------------------------------------


def resolve_trainer_dir(session: Session, settings: Settings) -> Path:
    """Effective LORA_FACTORY_DIR (DB override > env) or a friendly error."""
    raw = effective_setting(session, settings, "lora_factory_dir")
    if not raw:
        raise TrainerNotConfigured(
            "Character training is not configured — point LORA_FACTORY_DIR at a "
            "lora-factory checkout in Settings or .env."
        )
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise TrainerNotConfigured(f"trainer directory does not exist: {path}")
    return path


def pid_dir(settings: Settings) -> Path:
    return settings.data_path / "training" / "pids"


def train_log_path(settings: Settings, job_name: str) -> Path:
    return settings.data_path / "training" / "logs" / f"{job_name}.log"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    # If it's our own child, reap-check first: a finished-but-unreaped child
    # (zombie) would still answer kill(pid, 0). Not-our-child → fall through.
    try:
        done_pid, _ = os.waitpid(pid, os.WNOHANG)
        return done_pid != pid
    except ChildProcessError:
        pass
    except OSError:  # pragma: no cover
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists but not ours
        return True
    return True


def _terminate_group(pid: int) -> None:
    """SIGTERM the whole process group (start_new_session ⇒ pgid == pid)."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


# -- shared helpers -----------------------------------------------------------


def _set_character(session_factory, bus, character_id: int, **fields) -> None:
    with session_factory() as session:
        character = session.get(Character, character_id)
        if character is None:
            return
        for key, value in fields.items():
            setattr(character, key, value)
        session.add(character)
        session.commit()
        from fastapi.encoders import jsonable_encoder

        payload = jsonable_encoder(character)
    if bus is not None:
        bus.publish("character", payload)


def _log_tail(log_file: Path, lines: int = TAIL_LINES) -> str:
    if not log_file.is_file():
        return ""
    try:
        with log_file.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            text = fh.read().decode(errors="replace")
    except OSError:
        return ""
    return "\n".join([ln for ln in text.splitlines() if ln.strip()][-lines:])


def _train_progress(log_file: Path, output_dir: Path, job_name: str) -> tuple[float, str]:
    """(progress 0..0.99, detail tail) from stdout steps else checkpoint count."""
    detail = _log_tail(log_file)
    progress = 0.01
    matches = STEP_RE.findall(detail)
    if matches:
        step, total = matches[-1]
        total_i = int(total) or TOTAL_STEPS
        progress = int(step) / total_i
    elif output_dir.is_dir():
        best = 0
        # ai-toolkit names step checkpoints "<job>_000002500.safetensors"
        # (underscore + zero-padded), NOT "<job>-2500"
        ckpt_re = re.compile(re.escape(job_name) + r"_(\d+)\.safetensors$")
        for path in output_dir.glob(f"{job_name}_*.safetensors"):
            m = ckpt_re.search(path.name)
            if m:
                best = max(best, int(m.group(1)))
        if best:
            progress = best / TOTAL_STEPS
    return min(max(progress, 0.01), 0.99), detail


# -- dataset_prep -------------------------------------------------------------


@register("dataset_prep")
async def dataset_prep(job: Job, ctx) -> dict:
    payload = json.loads(job.payload_json or "{}")
    job_name = payload["job_name"]
    staging = payload["staging_dir"]
    with ctx.session_factory() as session:
        factory = resolve_trainer_dir(session, ctx.settings)

    cmd = [
        "bash",
        "prep.sh",
        staging,
        "--name",
        job_name,
        "--trigger",
        payload.get("trigger", ""),
        "--class-word",
        payload.get("class_word", "person"),
    ]
    ctx.update_progress(0.05, f"preparing dataset '{job_name}'")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=factory,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    try:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if text:
                tail.append(text)
                ctx.update_progress(detail="\n".join(tail))
            ctx.raise_if_cancelled()
        returncode = await proc.wait()
    except (JobCancelled, asyncio.CancelledError):
        _terminate_group(proc.pid)
        raise

    if returncode != 0:
        raise RuntimeError(
            f"prep.sh exited with code {returncode}\n" + "\n".join(tail)
        )

    report_path = factory / "jobs" / job_name / "report.md"
    report_md = report_path.read_text(errors="replace") if report_path.is_file() else ""
    ctx.update_progress(
        0.99,
        "\n".join([*tail, "dataset prepared — review the report, then start training"]),
    )
    return {
        "job_name": job_name,
        "staging_dir": staging,
        "report_path": str(report_path),
        "report_md": report_md,
    }


# -- lora_train ---------------------------------------------------------------


@register("lora_train")
async def lora_train(job: Job, ctx) -> dict:
    payload = json.loads(job.payload_json or "{}")
    job_name = payload["job_name"]
    character_id = payload.get("character_id")
    with ctx.session_factory() as session:
        factory = resolve_trainer_dir(session, ctx.settings)

    output_dir = factory / "output" / job_name
    log_file = train_log_path(ctx.settings, job_name)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    pids = pid_dir(ctx.settings)
    pids.mkdir(parents=True, exist_ok=True)
    pidfile = pids / f"{job_name}.json"

    if character_id is not None:
        _set_character(ctx.session_factory, ctx.bus, character_id, status="training")

    proc: asyncio.subprocess.Process | None = None
    if payload.get("reattach"):
        # never respawn on reattach: poll the pid if alive, else fall through
        # to the final-checkpoint check below (it may have finished already).
        pid = int(payload.get("pid") or 0)
        if pid and pid_alive(pid):
            ctx.update_progress(detail=f"re-attached to running training (pid {pid})")
            log.info("lora_train %s: re-attached to pid %s", job_name, pid)
    else:
        with log_file.open("ab") as log_fh:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "train.sh",
                job_name,
                cwd=factory,
                stdout=log_fh,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        pid = proc.pid
        ctx.update_progress(0.01, f"training started (pid {pid}, ~3h for {TOTAL_STEPS} steps)")

    pidfile.write_text(
        json.dumps(
            {
                "pid": pid,
                "job_id": ctx.job_id,
                "job_name": job_name,
                "character_id": character_id,
                "output_dir": str(output_dir),
                "log_file": str(log_file),
                "started_at": datetime.now(UTC).isoformat(),
            }
        )
    )

    try:
        while True:
            if proc is not None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=POLL_S)
                    break
                except TimeoutError:
                    pass
            else:
                if not pid or not pid_alive(pid):
                    break
                await asyncio.sleep(POLL_S)
            ctx.raise_if_cancelled()
            progress, detail = _train_progress(log_file, output_dir, job_name)
            ctx.update_progress(progress, detail or None)
    except (JobCancelled, asyncio.CancelledError) as exc:
        if isinstance(exc, JobCancelled) or ctx.cancelled():
            # user cancel: stop the trainer and clean up
            _terminate_group(pid)
            pidfile.unlink(missing_ok=True)
            if character_id is not None:
                _set_character(ctx.session_factory, ctx.bus, character_id, status="dataset")
        # backend shutdown (plain CancelledError): leave the process + pidfile
        # so a restarted backend can re-attach via recover_orphan_trains().
        raise

    pidfile.unlink(missing_ok=True)
    returncode = proc.returncode if proc is not None else None
    final = output_dir / f"{job_name}.safetensors"
    if (returncode not in (None, 0)) or not final.is_file():
        if character_id is not None:
            _set_character(ctx.session_factory, ctx.bus, character_id, status="dataset")
        raise RuntimeError(
            f"training failed (exit {returncode}, final checkpoint "
            f"{'missing' if not final.is_file() else 'present'})\n{_log_tail(log_file)}"
        )

    lora_name = f"lorafactory_{job_name}/{job_name}.safetensors"
    if character_id is not None:
        _set_character(
            ctx.session_factory,
            ctx.bus,
            character_id,
            status="trained",
            lora_name=lora_name,
            lora_strength=1.0,
        )
    return {"job_name": job_name, "lora_name": lora_name, "checkpoint": str(final)}


# -- cancellation of a train that has no live handler -------------------------


def on_train_cancelled(runner, job) -> None:
    """Reap a lora_train cancelled while it was still *queued*.

    A queued train's handler never ran, so its JobCancelled cleanup never fires.
    But a re-attach train (resurrected after a restart) already has a live
    trainer process behind a pidfile; if we just flip the row to 'cancelled' the
    process keeps burning the GPU and `recover_orphan_trains` resurrects it on
    the next poll. So here we kill the tracked process group, drop the pidfile
    (recovery keys off pidfile presence) and roll the character back to
    'dataset'. Safe/no-op for a train that never had a running process.

    Called from JobRunner.cancel() on the queued→cancelled path.
    """
    try:
        payload = json.loads(job.payload_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return
    job_name = payload.get("job_name")
    if not job_name:
        return
    pidfile = pid_dir(runner.settings) / f"{job_name}.json"
    info: dict = {}
    try:
        info = json.loads(pidfile.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    pid = int(info.get("pid") or payload.get("pid") or 0)
    if pid and pid_alive(pid):
        _terminate_group(pid)
    pidfile.unlink(missing_ok=True)
    character_id = payload.get("character_id") or info.get("character_id")
    if character_id is not None:
        _set_character(runner.session_factory, runner.bus, character_id, status="dataset")
    log.info("cancelled queued train '%s' (pid %s reaped)", job_name, pid or "none")


# -- restart recovery ---------------------------------------------------------


def recover_orphan_trains(runner) -> list[dict]:
    """Re-attach to trains that survived a backend restart.

    Called opportunistically by the training API. For each pidfile under
    DATA_DIR/training/pids:
    - pid alive + its DB job no longer queued/running → resurrect the job as
      queued with a reattach payload (the handler then polls the pid).

    A user-cancelled train never reaches here as an orphan: cancelling it (queued
    or running) kills the trainer and removes the pidfile up front (see
    `on_train_cancelled` / the handler's JobCancelled path), so recovery finds no
    pidfile and cannot resurrect a train the user explicitly stopped. A backend
    *shutdown* deliberately leaves the pidfile so the running train can be picked
    back up — that is the case this loop resurrects.
    - pid dead but the final checkpoint exists (train finished while we were
      down) → mark the character trained and the job done.
    - pid dead, no checkpoint → drop the stale pidfile.
    """
    settings: Settings = runner.settings
    pids = pid_dir(settings)
    if not pids.is_dir():
        return []
    actions: list[dict] = []
    for pidfile in sorted(pids.glob("*.json")):
        try:
            info = json.loads(pidfile.read_text())
        except (OSError, json.JSONDecodeError):
            pidfile.unlink(missing_ok=True)
            continue
        job_name = info.get("job_name", "")
        pid = int(info.get("pid") or 0)
        character_id = info.get("character_id")
        output_dir = Path(info.get("output_dir", ""))

        with runner.session_factory() as session:
            job = session.get(Job, info.get("job_id")) if info.get("job_id") else None
            if job is not None and job.status in ("queued", "running"):
                continue  # already handled by a live worker

            if pid and pid_alive(pid):
                payload = {
                    "job_name": job_name,
                    "character_id": character_id,
                    "reattach": True,
                    "pid": pid,
                }
                if job is not None and job.type == "lora_train":
                    job.status = "queued"
                    job.error = None
                    job.finished_at = None
                    job.detail = "re-attaching to running training after restart"
                    job.payload_json = json.dumps(payload)
                    session.add(job)
                    session.commit()
                    runner.bus.publish("job", runner.job_dict(job))
                else:
                    runner.enqueue("lora_train", payload)
                log.info("recovered running train '%s' (pid %s)", job_name, pid)
                actions.append({"job_name": job_name, "action": "reattached", "pid": pid})
                continue

            # pid gone — did it finish while the backend was down?
            final = output_dir / f"{job_name}.safetensors"
            if final.is_file():
                lora_name = f"lorafactory_{job_name}/{job_name}.safetensors"
                if character_id is not None:
                    _set_character(
                        runner.session_factory,
                        runner.bus,
                        character_id,
                        status="trained",
                        lora_name=lora_name,
                        lora_strength=1.0,
                    )
                if job is not None and job.status != "done":
                    job.status = "done"
                    job.error = None
                    job.progress = 1.0
                    job.finished_at = datetime.now(UTC)
                    job.detail = "training finished while the backend was offline"
                    job.result_json = json.dumps(
                        {"job_name": job_name, "lora_name": lora_name, "checkpoint": str(final)}
                    )
                    session.add(job)
                    session.commit()
                    runner.bus.publish("job", runner.job_dict(job))
                actions.append({"job_name": job_name, "action": "finalized"})
            else:
                # pid died without a final checkpoint (crash or host shutdown):
                # the handler set the character to "training" on spawn and the
                # shutdown-cancel path never rolls it back, which 409s any
                # retrain — reset it so the wizard can train again (ai-toolkit
                # auto-resumes from the newest checkpoint in the output dir).
                if character_id is not None:
                    _set_character(
                        runner.session_factory, runner.bus, character_id, status="dataset"
                    )
                actions.append({"job_name": job_name, "action": "stale"})
        pidfile.unlink(missing_ok=True)
    return actions
