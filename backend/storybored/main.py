"""FastAPI app factory: all routers, SSE, job runner, static frontend."""

import asyncio
import importlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from storybored.api import (
    breakdown,
    characters,
    export,
    generate,
    health,
    jobs,
    media,
    projects,
    scenes,
    settings_api,
    shots,
    training,
    workflows_api,
)
from storybored.config import Settings
from storybored.db import create_db_engine, init_db
from storybored.events import EventBus
from storybored.events import router as events_router
from storybored.jobs.runner import JobRunner
from storybored.seed import demo

log = logging.getLogger("storybored")

#: imported at startup so their job handlers self-register
HANDLER_MODULES = [
    "storybored.engine.image",
    "storybored.engine.video",
    "storybored.export.animatic",
    "storybored.training.lora_factory",
]

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_db_engine(settings)
    bus = EventBus()
    runner = JobRunner(engine, settings, bus)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(engine)
        bus.set_loop(asyncio.get_running_loop())
        for module in HANDLER_MODULES:
            importlib.import_module(module)
        await runner.start()
        # Re-attach to LoRA trains that survived a restart (start_new_session
        # keeps them alive; the pidfile lets us pick them back up eagerly).
        try:
            from storybored.training.lora_factory import recover_orphan_trains

            recover_orphan_trains(runner)
        except Exception:  # noqa: BLE001 - recovery must never block startup
            log.exception("orphan train recovery failed")
        yield
        await runner.stop()

    app = FastAPI(title="StoryBored", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.bus = bus
    app.state.runner = runner

    for module in (
        health,
        projects,
        scenes,
        shots,
        characters,
        jobs,
        generate,
        breakdown,
        export,
        settings_api,
        workflows_api,
        training,
        media,
        demo,
    ):
        app.include_router(module.router)
    app.include_router(events_router)

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve frontend/dist at / with index.html fallback (SPA), if built."""
    dist = FRONTEND_DIST
    index = dist / "index.html"
    if not index.is_file():
        return

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path:
            candidate = (dist / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(dist):
                return FileResponse(candidate)
        return FileResponse(index)
