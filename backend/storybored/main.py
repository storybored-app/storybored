"""FastAPI app factory: all routers, SSE, job runner, static frontend."""

import asyncio
import importlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse

from storybored.api import (
    breakdown,
    characters,
    enhance,
    export,
    generate,
    health,
    jobs,
    lifecycle,
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
    "storybored.export.archive",
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
        enhance,
        export,
        lifecycle,
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


#: served instead of the app when frontend/dist doesn't exist yet — a silent
#: 404 on / makes a fresh install look broken when it's just one step short.
NOT_BUILT_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>StoryBored — one step left</title>
  <style>
    body { margin: 0; display: grid; place-items: center; min-height: 100vh;
           background: #121110; color: #e8e4dd;
           font: 15px/1.6 system-ui, sans-serif; }
    main { max-width: 34rem; padding: 2rem; }
    h1 { font-size: 1.15rem; }  h1 span { color: #e2a33d; }
    pre { background: #1c1a18; border: 1px solid #2c2926; border-radius: 8px;
          padding: 0.9rem 1.1rem; overflow-x: auto; }
    p { color: #a89f93; }  code { color: #e8e4dd; }
  </style>
</head>
<body>
<main>
  <h1>Story<span>Bored</span> is running — the interface just isn't built yet</h1>
  <p>The server is up (the API is live under <code>/api</code>), but the web
  interface hasn't been compiled. From the project folder, run:</p>
  <pre><code>npm --prefix frontend install
npm --prefix frontend run build</code></pre>
  <p>then reload this page — no server restart needed.</p>
</main>
</body>
</html>
"""


def _mount_frontend(app: FastAPI) -> None:
    """Serve frontend/dist at / with index.html fallback (SPA).

    When the frontend isn't built, serve a small self-contained page that says
    exactly how to finish the install instead of 404ing silently. The check is
    per-request, so building the frontend takes effect on reload — no restart."""
    dist = FRONTEND_DIST
    index = dist / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if not index.is_file():
            return HTMLResponse(NOT_BUILT_HTML)
        if full_path:
            candidate = (dist / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(dist):
                return FileResponse(candidate)
        return FileResponse(index)
