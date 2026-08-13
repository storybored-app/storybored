"""GET /api/media/{path} — serve files under DATA_DIR, path-traversal-safe."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api", tags=["media"])


@router.get("/media/{path:path}")
def serve_media(path: str, request: Request):
    settings = request.app.state.settings
    base: Path = settings.data_path

    if not path or "\x00" in path:
        raise HTTPException(status_code=404, detail="not found")

    try:
        target = (base / path).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="not found") from None

    # resolve() collapses .. and follows symlinks; anything escaping DATA_DIR is rejected
    if not target.is_relative_to(base):
        raise HTTPException(status_code=404, detail="not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target)
