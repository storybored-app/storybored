# OWNED-BY: training-agent
"""lora-factory trainer adapter: dataset_prep + lora_train job handlers.

Wraps an external lora-factory checkout (LORA_FACTORY_DIR, DB-overridable).
When unset the trainer degrades cleanly: /api/health reports "not_configured"
and the wizard/train endpoints return 503 with a friendly detail.

dataset_prep — `bash prep.sh <staging_dir> --name <job> --trigger <t>
--class-word <w>` with cwd=LORA_FACTORY_DIR; the stdout tail (last ~5 lines)
streams into job.detail; on success `jobs/<job>/report.md` is read into
result_json for the wizard's review screen.

lora_shootout — after a finished train: `compare.py <job>` renders the saved
checkpoints through the engine (grid.jpg contact sheet), then `score.py <job>`
ranks checkpoint+strength combos (facenet likeness + VLM judge → scores.md).
The ranked table is parsed into result_json so the wizard can offer one-click
"use this checkpoint". Both scripts run under the factory's own venv python.

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
import sys
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

#: expected training steps per LoRA family (used for the checkpoint-count
#: progress fallback and the startup ETA line; live "N/total" stdout lines
#: always win). krea2 = the tuned 3000-step recipe (~2.5–4 h on 24 GB);
#: z-image = the community-proven 12 GB recipe (2000 steps, ~1–2 h);
#: qwen-image = ai-toolkit's example config (2000 steps, EXPERIMENTAL — no
#: verified timing, so no ETA is claimed for it).
FAMILY_STEPS: dict[str, int] = {"krea2": 3000, "z-image": 2000, "qwen-image": 2000}
FAMILY_ETA: dict[str, str] = {"krea2": "~2.5–4h", "z-image": "~1–2h"}

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


def _train_progress(
    log_file: Path, output_dir: Path, job_name: str, total_steps: int = TOTAL_STEPS
) -> tuple[float, str]:
    """(progress 0..0.99, detail tail) from stdout steps else checkpoint count."""
    detail = _log_tail(log_file)
    progress = 0.01
    matches = STEP_RE.findall(detail)
    if matches:
        step, total = matches[-1]
        total_i = int(total) or total_steps
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
            progress = best / total_steps
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
    # target model family (per-family resize ceiling + config template in the
    # trainer); absent on pre-family payloads → the trainer's own default
    if payload.get("family"):
        cmd += ["--family", str(payload["family"])]
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


# -- lora_shootout ------------------------------------------------------------

# compare.py progress lines look like "[compare] [3/24] step 2500 @ 1.0 p0 seed0"
COMPARE_PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\]")

# scores.md table rows: rank, checkpoint stem, strength, TOTAL, likeness,
# prompt, clean, no-face/of (all whitespace-separated fixed-width columns)
SCORE_LINE_RE = re.compile(
    r"^\s*(\d+)\s+(\S+?)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+(\d+)/(\d+)\s*$"
)


def factory_python(factory: Path) -> str:
    """The factory's own venv python (compare/score deps live there)."""
    venv = factory / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def checkpoint_filenames(factory: Path, job_name: str) -> list[str]:
    """Saved checkpoint files for a job, step order, unsuffixed final last."""
    out_dir = factory / "output" / job_name
    if not out_dir.is_dir():
        return []
    stem_re = re.compile(re.escape(job_name) + r"_(\d+)\.safetensors$")
    stepped: list[tuple[int, str]] = []
    finals: list[str] = []
    for path in sorted(out_dir.glob(f"{job_name}*.safetensors")):
        m = stem_re.search(path.name)
        if m:
            stepped.append((int(m.group(1)), path.name))
        elif path.name == f"{job_name}.safetensors":
            finals.append(path.name)
    return [name for _, name in sorted(stepped)] + finals


def checkpoint_label(stem: str, job_name: str) -> str:
    m = re.match(re.escape(job_name) + r"_(\d+)$", stem)
    return f"step {int(m.group(1))}" if m else "final"


def parse_scores(scores_md: str, job_name: str) -> list[dict]:
    """Parse score.py's ranked table into rows the UI can act on."""
    rows: list[dict] = []
    for line in scores_md.splitlines():
        m = SCORE_LINE_RE.match(line)
        if not m:
            continue
        stem = m.group(2)
        rows.append(
            {
                "rank": int(m.group(1)),
                "checkpoint": f"{stem}.safetensors",
                "label": checkpoint_label(stem, job_name),
                "strength": float(m.group(3)),
                "total": float(m.group(4)),
                "likeness": float(m.group(5)),
                "prompt_match": float(m.group(6)),
                "clean": float(m.group(7)),
                "no_face": int(m.group(8)),
                "cells": int(m.group(9)),
            }
        )
    rows.sort(key=lambda r: r["rank"])
    return rows


async def _stream_subprocess(
    ctx,
    cmd: list[str],
    cwd: Path,
    tail: deque[str],
    progress_lo: float,
    progress_hi: float,
    env_extra: dict[str, str] | None = None,
) -> int:
    """Run cmd streaming stdout into job.detail; compare-style [n/total] lines
    map onto the [progress_lo, progress_hi] band. Kills the group on cancel."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env={**os.environ, "HF_HUB_OFFLINE": "1", **(env_extra or {})},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if not text:
                continue
            tail.append(text)
            progress = None
            m = COMPARE_PROGRESS_RE.search(text)
            if m and int(m.group(2)) > 0:
                frac = min(int(m.group(1)) / int(m.group(2)), 1.0)
                progress = progress_lo + (progress_hi - progress_lo) * frac
            ctx.update_progress(progress, "\n".join(tail))
            ctx.raise_if_cancelled()
        return await proc.wait()
    except (JobCancelled, asyncio.CancelledError):
        _terminate_group(proc.pid)
        raise


def _compare_env_for_pack(pack) -> dict[str, str]:
    """compare.py env for rendering through a workflow pack: the pack's graph,
    its node ids, and the manifest's character-time LoRA exclusions
    (``character_injection.disable_nodes`` — aesthetic LoRAs that fight the
    character identity being measured)."""
    manifest = pack.manifest
    injection = manifest.get("character_injection") or {}
    tail_node = str(injection.get("after_node") or "")
    if not tail_node:
        return {}
    try:
        graph = pack.load_graph()
    except (OSError, ValueError):
        return {}

    def _model_ref(node: dict) -> str | None:
        ref = node.get("inputs", {}).get("model")
        return str(ref[0]) if isinstance(ref, list) and len(ref) == 2 else None

    consumer = next(
        (nid for nid, node in graph.items() if _model_ref(node) == tail_node), None
    )
    params = {p.get("key"): str(p.get("node")) for p in manifest.get("parameters", [])}
    nodes = {
        "seed": params.get("seed"),
        "size": params.get("width"),
        "prompt": params.get("prompt"),
        "save": str(manifest.get("output_node") or ""),
        "lora_tail": tail_node,
        "model_consumer": consumer,
    }
    if not all(nodes.values()):
        return {}
    skip = [
        str(graph[str(nid)]["inputs"].get("lora_name"))
        for nid in injection.get("disable_nodes") or []
        if str(nid) in graph
        and graph[str(nid)].get("class_type") == "LoraLoader"
        and graph[str(nid)]["inputs"].get("lora_name")
    ]
    env = {
        "COMFY_WORKFLOW": str(pack.dir / (manifest.get("graph") or "graph.json")),
        "COMPARE_NODES": json.dumps(nodes),
    }
    if skip:
        env["COMPARE_SKIP_LORAS"] = ",".join(skip)
    return env


def _shootout_engine_env(session, settings) -> dict[str, str]:
    """Resolve the app's default image engine and build compare.py's env from
    it, so the shootout renders through the same chain the app renders with.
    Empty dict → compare.py falls back to its standalone-CLI defaults."""
    from storybored.api.settings_api import effective_setting
    from storybored.engine import registry

    packs = registry.load_packs(settings)
    workflow_id = effective_setting(session, settings, "default_image_workflow")
    if not workflow_id:
        workflow_id = registry.default_workflow_id(packs, kind="image")
    pack = packs.get(workflow_id) if workflow_id else None
    if pack is None:
        return {}
    return _compare_env_for_pack(pack)


@register("lora_shootout")
async def lora_shootout(job: Job, ctx) -> dict:
    payload = json.loads(job.payload_json or "{}")
    job_name = payload["job_name"]
    with ctx.session_factory() as session:
        factory = resolve_trainer_dir(session, ctx.settings)
        engine_env = _shootout_engine_env(session, ctx.settings)
    python = factory_python(factory)

    compare_cmd = [python, "compare.py", job_name]
    strengths = (payload.get("strengths") or "1.0").strip()
    compare_cmd += ["--strengths", strengths]
    ckpts = (payload.get("ckpts") or "").strip()
    if ckpts:
        compare_cmd += ["--ckpts", ckpts]
    seeds = int(payload.get("seeds") or 1)
    if seeds > 1:
        compare_cmd += ["--seeds", str(seeds)]

    tail: deque[str] = deque(maxlen=TAIL_LINES)
    ctx.update_progress(0.02, f"rendering checkpoint test shots for '{job_name}'")
    code = await _stream_subprocess(
        ctx, compare_cmd, factory, tail, 0.02, 0.7, env_extra=engine_env
    )
    if code != 0:
        raise RuntimeError(f"compare.py exited with code {code}\n" + "\n".join(tail))

    ctx.update_progress(0.72, "scoring renders (likeness + prompt judges)…")
    code = await _stream_subprocess(
        ctx, [python, "score.py", job_name], factory, tail, 0.72, 0.97
    )
    if code != 0:
        raise RuntimeError(f"score.py exited with code {code}\n" + "\n".join(tail))

    comp = factory / "output" / job_name / "comparison"
    scores_path = comp / "scores.md"
    scores_md = scores_path.read_text(errors="replace") if scores_path.is_file() else ""
    results = parse_scores(scores_md, job_name)
    ctx.update_progress(0.99, "\n".join([*tail, "shootout finished — pick a winner"]))
    return {
        "job_name": job_name,
        "results": results,
        "scores_md": scores_md,
        "grid": (comp / "grid.jpg").is_file(),
    }


# -- lora_train ---------------------------------------------------------------


@register("lora_train")
async def lora_train(job: Job, ctx) -> dict:
    payload = json.loads(job.payload_json or "{}")
    job_name = payload["job_name"]
    character_id = payload.get("character_id")
    family = str(payload.get("family") or "")
    total_steps = FAMILY_STEPS.get(family, TOTAL_STEPS)
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
        cmd = ["bash", "train.sh", job_name]
        if family:
            cmd += ["--family", family]
        with log_file.open("ab") as log_fh:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=factory,
                stdout=log_fh,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        pid = proc.pid
        eta = FAMILY_ETA.get(family or "krea2")
        ctx.update_progress(
            0.01,
            f"training started (pid {pid}, "
            + (f"{eta} for {total_steps} steps)" if eta else f"{total_steps} steps)"),
        )

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
            progress, detail = _train_progress(log_file, output_dir, job_name, total_steps)
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
