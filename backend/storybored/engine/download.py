# OWNED-BY: engine-agent
"""model_download job handler: fetch a verified catalog file into COMFY_MODELS_DIR.

Runs on the ``io`` lane so a multi-gigabyte download never blocks the GPU
queue. Payload (built by POST /api/workflows/{id}/download-models):

    {"filename", "url", "folder", "size_bytes"?, "workflow_id"?}

Streams to ``<COMFY_MODELS_DIR>/<folder>/<filename>.part``, reports progress
like every other job, verifies the size when the catalog knows it, renames
into place, then flushes the /object_info cache so the next availability check
sees the new file as soon as ComfyUI rescans.
"""

import asyncio
import json
from pathlib import Path

import httpx

from storybored.api.settings_api import effective_setting
from storybored.engine.comfy_client import clear_object_info_cache
from storybored.jobs.registry import register
from storybored.jobs.runner import JobCancelled

CHUNK_BYTES = 1 << 20  # 1 MiB
#: progress writes hit the DB + SSE — only update every this many bytes
PROGRESS_EVERY_BYTES = 64 << 20

_TIMEOUT = httpx.Timeout(30.0, read=300.0)


def _safe_name(value: str, what: str) -> str:
    """A single path component: no separators, no traversal, not hidden."""
    value = str(value)
    if (
        not value
        or value != value.strip()
        or "/" in value
        or "\\" in value
        or value in (".", "..")
        or value.startswith(".")
    ):
        raise RuntimeError(f"unsafe {what} {value!r}")
    return value


def _human(n: int) -> str:
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n / 2**20:.0f} MB"


@register("model_download")
async def model_download(job, ctx):
    payload = json.loads(job.payload_json or "{}")
    filename = _safe_name(payload.get("filename", ""), "filename")
    folder = _safe_name(payload.get("folder", ""), "folder")
    url = str(payload.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise RuntimeError("model_download needs an http(s) url")
    expected = payload.get("size_bytes")
    expected = int(expected) if isinstance(expected, int) and expected > 0 else None

    with ctx.session_factory() as session:
        models_dir = effective_setting(session, ctx.settings, "comfy_models_dir")
    if not models_dir:
        raise RuntimeError(
            "COMFY_MODELS_DIR is not configured — set it in Settings to download "
            "models in-app, or place the file manually"
        )
    dest = Path(models_dir).expanduser() / folder / filename
    if dest.is_file() and (expected is None or dest.stat().st_size == expected):
        clear_object_info_cache()
        ctx.update_progress(progress=1.0, detail="file already present")
        return {"file_path": str(dest), "size_bytes": dest.stat().st_size}
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")

    done = 0
    last_reported = 0
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT) as http:
            async with http.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"download failed: HTTP {resp.status_code} from {url}"
                    )
                total = expected
                if total is None:
                    try:
                        total = int(resp.headers.get("content-length", "")) or None
                    except ValueError:
                        total = None
                ctx.update_progress(
                    progress=0.0,
                    detail=f"downloading {filename}"
                    + (f" ({_human(total)})" if total else ""),
                )
                with tmp.open("wb") as f:
                    async for chunk in resp.aiter_bytes(CHUNK_BYTES):
                        ctx.raise_if_cancelled()
                        f.write(chunk)
                        done += len(chunk)
                        if total and done - last_reported >= PROGRESS_EVERY_BYTES:
                            last_reported = done
                            ctx.update_progress(
                                progress=min(done / total, 0.99),
                                detail=f"downloading {filename} — "
                                f"{_human(done)} of {_human(total)}",
                            )
        if expected is not None and tmp.stat().st_size != expected:
            raise RuntimeError(
                f"size mismatch: got {tmp.stat().st_size} bytes, catalog says "
                f"{expected} — deleted the partial file, try again"
            )
        tmp.replace(dest)
    except (JobCancelled, asyncio.CancelledError) as exc:
        tmp.unlink(missing_ok=True)
        raise JobCancelled(str(exc)) from exc
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    # availability may flip the pack to ready as soon as ComfyUI rescans —
    # don't make the user wait out the 60s cache on top
    clear_object_info_cache()
    ctx.update_progress(progress=1.0, detail=f"{filename} downloaded")
    return {"file_path": str(dest), "size_bytes": done}
