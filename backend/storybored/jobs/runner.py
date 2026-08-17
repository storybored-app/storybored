"""DB-backed job queue with a single asyncio worker per lane.

All v1 job types run in lane "gpu" → strict serialization (a long lora_train
naturally blocks generations). Jobs survive restarts: on startup, `running`
jobs are marked failed ("interrupted by restart") and `queued` jobs resume.

Mode switching: the runner tracks the last ComfyUI model family used
(image_gen → "image", video_gen → "video"; animatic and training jobs touch no
ComfyUI model family). When the family changes and the COMFY_* commands are
configured, it runs COMFY_FLUSH_CMD then COMFY_MODE_{IMAGE|VIDEO}_CMD via the
shell, then polls {COMFYUI_URL}/system_stats (max 120s) before running the job.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime

import httpx
from fastapi.encoders import jsonable_encoder
from sqlalchemy import update as sa_update
from sqlmodel import Session, select

from storybored.config import Settings
from storybored.events import EventBus
from storybored.jobs.registry import get_handler
from storybored.models import Job, Shot, Take

log = logging.getLogger("storybored.jobs")

#: job type → ComfyUI model family (jobs absent here need no ComfyUI mode switch)
JOB_FAMILY: dict[str, str] = {
    "image_gen": "image",
    "character_thumb": "image",
    "video_gen": "video",
    # compare.py renders its test shots through the image engine
    "lora_shootout": "image",
}

#: "gpu" serializes every GPU job (the concurrency model — see ARCHITECTURE.md);
#: "io" runs network/disk jobs (model downloads) without blocking the GPU queue.
LANES = ("gpu", "io")

COMFY_WAIT_S = 120.0


class JobCancelled(Exception):
    """Raised inside a handler via ctx.raise_if_cancelled()."""


def _now() -> datetime:
    return datetime.now(UTC)


class JobContext:
    """Handed to every handler. Gives DB access, settings, events, progress
    updates and cooperative cancellation."""

    def __init__(self, runner: "JobRunner", job_id: int) -> None:
        self._runner = runner
        self.job_id = job_id
        self.settings: Settings = runner.settings
        self.bus: EventBus = runner.bus
        self.session_factory = runner.session_factory

    def publish(self, event_type: str, data) -> None:
        self.bus.publish(event_type, data)

    def update_progress(self, progress: float | None = None, detail: str | None = None) -> None:
        self._runner.update_job(self.job_id, progress=progress, detail=detail)

    def cancelled(self) -> bool:
        return self.job_id in self._runner.cancel_requested

    def raise_if_cancelled(self) -> None:
        if self.cancelled():
            raise JobCancelled(f"job {self.job_id} cancelled")


class JobRunner:
    def __init__(self, engine, settings: Settings, bus: EventBus) -> None:
        self.engine = engine
        self.settings = settings
        self.bus = bus
        self.cancel_requested: set[int] = set()
        self._workers: dict[str, asyncio.Task] = {}
        self._current: dict[str, tuple[int, asyncio.Task]] = {}
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._last_family: str | None = None

    # -- session / row helpers ------------------------------------------------

    def session_factory(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    @staticmethod
    def job_dict(job: Job) -> dict:
        return jsonable_encoder(job)

    def update_job(self, job_id: int, **fields) -> Job | None:
        """Set the given non-None fields on a job, commit, publish a `job` event."""
        with self.session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                return None
            for key, value in fields.items():
                if value is not None:
                    setattr(job, key, value)
            session.add(job)
            session.commit()
        self.bus.publish("job", self.job_dict(job))
        return job

    # -- public API -----------------------------------------------------------

    def enqueue(self, job_type: str, payload: dict | None = None, lane: str = "gpu") -> Job:
        """Insert a queued job and wake the worker. Thread-safe."""
        job = Job(
            type=job_type,
            status="queued",
            lane=lane,
            payload_json=json.dumps(payload or {}),
        )
        with self.session_factory() as session:
            session.add(job)
            session.commit()
        self.bus.publish("job", self.job_dict(job))
        self._wake_worker()
        return job

    def cancel(self, job_id: int) -> Job | None:
        """Cancel a job. Queued → cancelled immediately; running → cooperative
        flag + hard task cancel. Thread-safe. Returns the (possibly updated) row.

        The queued→cancelled flip is a conditional UPDATE guarded on
        ``status == 'queued'``: if the worker won the race and already claimed
        the row as ``running`` (see ``_claim_next``), the update matches zero
        rows and we fall through to the cooperative-cancel path instead of
        silently losing the cancel."""
        cancelled_job: Job | None = None
        with self.session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                return None
            if job.status == "queued":
                result = session.execute(
                    sa_update(Job)
                    .where(Job.id == job_id, Job.status == "queued")
                    .values(
                        status="cancelled",
                        finished_at=_now(),
                        error="cancelled before start",
                    )
                )
                session.commit()
                session.refresh(job)
                if result.rowcount == 1:
                    cancelled_job = job
        if cancelled_job is not None:
            self.bus.publish("job", self.job_dict(cancelled_job))
            self._settle_shot_for_job(cancelled_job)
            if cancelled_job.type == "lora_train":
                # a queued train's handler never ran its cleanup — reap any live
                # trainer + pidfile so it stops and stays cancelled (lazy import
                # avoids a runner↔training import cycle).
                from storybored.training.lora_factory import on_train_cancelled

                on_train_cancelled(self, cancelled_job)
            return cancelled_job
        if job.status == "running":
            self.cancel_requested.add(job_id)
            for lane_job_id, task in self._current.values():
                if lane_job_id == job_id and self._loop is not None:
                    self._loop.call_soon_threadsafe(task.cancel)
        return job

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._stopping = False
        self._recover()
        for lane in LANES:
            self._workers[lane] = asyncio.create_task(self._worker(lane), name=f"job-worker-{lane}")
        log.info("job runner started (lanes: %s)", ", ".join(LANES))

    async def stop(self) -> None:
        self._stopping = True
        for task in self._workers.values():
            task.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()

    # -- internals ------------------------------------------------------------

    def _recover(self) -> None:
        """Startup recovery: running → failed (interrupted); queued jobs resume.

        A crash/restart can leave side effects half-written: a Take stuck at
        'pending' (its handler never got to mark it done/failed) and its Shot
        stuck at 'queued'. We settle those too, mirroring the terminal-failure
        path, so nothing is left spinning forever in the UI."""
        interrupted: list[Job] = []
        with self.session_factory() as session:
            for job in session.exec(select(Job).where(Job.status == "running")):
                job.status = "failed"
                job.error = "interrupted by restart"
                job.finished_at = _now()
                session.add(job)
                interrupted.append(job)
            session.commit()
        for job in interrupted:
            self.bus.publish("job", self.job_dict(job))
        self._settle_interrupted_side_effects()
        if interrupted:
            log.warning("marked %d interrupted job(s) failed", len(interrupted))

    def _settle_interrupted_side_effects(self) -> None:
        """Fail orphaned 'pending' takes and un-stick 'queued' shots left behind
        by an interrupted job. Safe to run even when nothing is stuck."""
        # 1) pending takes have no live handler to finish them → mark failed.
        take_dicts: list[dict] = []
        with self.session_factory() as session:
            pending = list(session.exec(select(Take).where(Take.status == "pending")))
            for take in pending:
                take.status = "failed"
                take.error = "interrupted by restart"
                session.add(take)
            session.commit()
            take_dicts = [jsonable_encoder(t) for t in pending]
        for take_dict in take_dicts:
            self.bus.publish("take", take_dict)
        # 2) any shot still 'queued' with no live generation → settle it.
        with self.session_factory() as session:
            queued_shot_ids = [
                s.id for s in session.exec(select(Shot).where(Shot.status == "queued"))
            ]
        for shot_id in queued_shot_ids:
            if shot_id is not None:
                self._settle_shot(shot_id)

    def _wake_worker(self) -> None:
        if self._loop is not None and self._wake is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._wake.set)

    def _claim_next(self, lane: str) -> Job | None:
        with self.session_factory() as session:
            job = session.exec(
                select(Job)
                .where(Job.lane == lane, Job.status == "queued")
                .order_by(Job.id)  # type: ignore[arg-type]
                .limit(1)
            ).first()
            if job is None:
                return None
            # Atomically flip queued→running: if the API thread cancelled this
            # row between our SELECT and here, the guard matches zero rows and we
            # skip it (a job cancelled while queued must stay cancelled).
            result = session.execute(
                sa_update(Job)
                .where(Job.id == job.id, Job.status == "queued")
                .values(status="running", started_at=_now())
            )
            session.commit()
            if result.rowcount != 1:
                return None  # lost the race (cancelled/claimed) — retry next tick
            session.refresh(job)
        self.bus.publish("job", self.job_dict(job))
        return job

    async def _worker(self, lane: str) -> None:
        assert self._wake is not None
        while not self._stopping:
            job = self._claim_next(lane)
            if job is None:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=0.25)
                except TimeoutError:
                    pass
                self._wake.clear()
                continue
            await self._run_job(job, lane)

    async def _run_job(self, job: Job, lane: str) -> None:
        job_id = job.id
        assert job_id is not None
        try:
            handler = get_handler(job.type)
            if handler is None:
                raise RuntimeError(f"no handler registered for job type '{job.type}'")

            family = JOB_FAMILY.get(job.type)
            if family is not None and family != self._last_family:
                # _mode_switch owns _last_family: it only records the new family
                # after the switch commands AND the readiness poll both succeed,
                # so a failed switch fails the job without desyncing our state.
                await self._mode_switch(job_id, family)

            ctx = JobContext(self, job_id)
            task = asyncio.create_task(handler(job, ctx), name=f"job-{job_id}-{job.type}")
            self._current[lane] = (job_id, task)
            result = await task
            self._finish(job_id, "done", result=result if isinstance(result, dict) else None)
        except JobCancelled:
            self._finish(job_id, "cancelled", error="cancelled")
        except asyncio.CancelledError:
            self._finish(job_id, "cancelled", error="cancelled")
            if self._stopping:
                raise
        except Exception as exc:  # noqa: BLE001 - job failures must never kill the worker
            log.exception("job %s failed", job_id)
            self._finish(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            self._current.pop(lane, None)
            self.cancel_requested.discard(job_id)

    def _finish(
        self, job_id: int, status: str, result: dict | None = None, error: str | None = None
    ) -> None:
        with self.session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            job.status = status
            job.finished_at = _now()
            if status == "done":
                job.progress = 1.0
                job.result_json = json.dumps(result or {})
            if error is not None:
                job.error = error
            session.add(job)
            session.commit()
        self.bus.publish("job", self.job_dict(job))
        if status in ("failed", "cancelled"):
            self._settle_shot_for_job(job)

    def _settle_shot_for_job(self, job: Job) -> None:
        """After a terminal image_gen failure/cancel, un-stick the shot status.

        POST /generate flips the shot to "queued"; on success the handler moves
        it to "generated" at the first finished take. If the whole job dies
        (all takes failed, mode-switch error, cancel, restart), roll the shot
        back to "generated" (it has finished takes) or "draft" so the UI never
        shows a forever-pulsing queued shot.
        """
        if job.type != "image_gen":
            return
        try:
            shot_id = int(json.loads(job.payload_json or "{}").get("shot_id"))
        except (TypeError, ValueError):
            return
        self._settle_shot(shot_id)

    def _settle_shot(self, shot_id: int) -> None:
        """Roll a stuck-'queued' shot back to a consistent status: 'generated'
        if it has any finished take, else 'draft'. A still-queued generation for
        the same shot keeps it 'queued'. No-op unless the shot is 'queued'."""
        shot_dict = None
        with self.session_factory() as session:
            shot = session.get(Shot, shot_id)
            if shot is None or shot.status != "queued":
                return
            # another queued generation for this shot keeps it "queued"
            for pending in session.exec(
                select(Job).where(Job.type == "image_gen", Job.status == "queued")
            ):
                try:
                    if int(json.loads(pending.payload_json or "{}").get("shot_id")) == shot_id:
                        return
                except (TypeError, ValueError):
                    continue
            has_done = (
                session.exec(
                    select(Take)
                    .where(Take.shot_id == shot_id, Take.kind == "image", Take.status == "done")
                    .limit(1)
                ).first()
                is not None
            )
            shot.status = "generated" if has_done else "draft"
            session.add(shot)
            session.commit()
            session.refresh(shot)
            shot_dict = jsonable_encoder(shot)
        self.bus.publish("shot", shot_dict)

    async def _mode_switch(self, job_id: int, family: str) -> None:
        """Switch ComfyUI to the given model family, then record it as current.

        Both the switch commands and the post-switch readiness poll gate job
        submission. If a command exits non-zero or the poll times out, we raise
        (failing the job) and leave ``_last_family`` = None, so the next job of
        this family retries the switch instead of assuming ComfyUI's real mode.
        """
        s = self.settings
        cmds: list[str] = []
        if s.comfy_flush_cmd:
            cmds.append(s.comfy_flush_cmd)
        mode_cmd = s.comfy_mode_image_cmd if family == "image" else s.comfy_mode_video_cmd
        if mode_cmd:
            cmds.append(mode_cmd)
        if not cmds:
            # nothing to run — the switch is a no-op, family is trivially current
            self._last_family = family
            return

        # the commands have side effects (flush / GPU profile / restart); our
        # tracked family is unknown until the switch is fully confirmed below.
        self._last_family = None

        lines: list[str] = [f"switching engine to {family} mode"]
        for cmd in cmds:
            self.update_job(job_id, detail=f"mode switch: {cmd}")
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            text = (out or b"").decode(errors="replace").strip()
            lines.append(f"$ {cmd} (exit {proc.returncode})")
            if text:
                lines.append(text)
            if proc.returncode != 0:
                # keep the captured output visible, then fail the job
                self.update_job(job_id, detail="\n".join(lines)[-2000:])
                raise RuntimeError(
                    f"engine {family}-mode switch command failed "
                    f"(exit {proc.returncode}): {cmd}"
                )
        self.update_job(job_id, detail="\n".join(lines)[-2000:])

        await self._wait_for_comfy(job_id)
        # switch commands + readiness poll both succeeded → family is now current
        self._last_family = family

    async def _wait_for_comfy(self, job_id: int) -> None:
        url = f"{self.settings.comfyui_url.rstrip('/')}/system_stats"
        assert self._loop is not None
        deadline = self._loop.time() + COMFY_WAIT_S
        async with httpx.AsyncClient(timeout=5.0) as client:
            while True:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        self.update_job(job_id, detail="engine ready")
                        return
                except httpx.HTTPError:
                    pass
                if self._loop.time() >= deadline:
                    raise RuntimeError(
                        f"ComfyUI not reachable within {int(COMFY_WAIT_S)}s after mode switch"
                    )
                await asyncio.sleep(2.0)
