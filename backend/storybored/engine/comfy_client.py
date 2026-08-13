# OWNED-BY: engine-agent
"""Async HTTP client for ComfyUI.

Endpoints used (no websocket dependency — simplest robust path):

- ``POST /prompt``          submit an API-format graph with a client_id
- ``GET  /history/{id}``    poll completion (1s interval)
- ``GET  /queue``           queue position for progress display
- ``GET  /view``            fetch output files
- ``POST /upload/image``    upload input images (e.g. video first frames)
- ``GET  /object_info``     model dropdown enums, cached for 60s
- ``POST /interrupt`` + ``POST /queue`` {"delete": [...]}: cancellation

Errors coming back from ComfyUI (``node_errors``, validation failures,
execution errors in history) are flattened into a readable ``ComfyError``.
"""

import asyncio
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx

#: /object_info responses are cached per (base_url, class_type) for this long.
OBJECT_INFO_TTL_S = 60.0

_object_info_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def clear_object_info_cache() -> None:
    """Drop all cached /object_info responses (used by tests and settings changes)."""
    _object_info_cache.clear()


class ComfyError(RuntimeError):
    """Any error talking to ComfyUI, with a human-readable message."""


class ComfyCancelled(ComfyError):
    """Raised by wait_for() when the caller's should_cancel() turns true."""


def extract_error(payload: dict) -> str:
    """Flatten a ComfyUI error response (error + node_errors) into one string."""
    parts: list[str] = []
    err = payload.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("type") or ""
        if msg:
            parts.append(str(msg))
    elif err:
        parts.append(str(err))
    node_errors = payload.get("node_errors") or {}
    if isinstance(node_errors, dict):
        for node_id, info in node_errors.items():
            if not isinstance(info, dict):
                continue
            for e in info.get("errors") or []:
                msg = e.get("message") or "error"
                details = e.get("details") or ""
                text = f"node {node_id}: {msg}"
                if details:
                    text += f" ({details})"
                parts.append(text)
    return "; ".join(parts)


def _history_error(entry: dict) -> str:
    """Extract an execution error message from a /history entry."""
    status = entry.get("status") or {}
    parts: list[str] = []
    for message in status.get("messages") or []:
        try:
            kind, data = message[0], message[1]
        except (IndexError, TypeError):
            continue
        if kind == "execution_error" and isinstance(data, dict):
            node = data.get("node_id") or data.get("node_type") or "?"
            msg = data.get("exception_message") or data.get("exception_type") or "error"
            parts.append(f"node {node}: {msg}")
    return "; ".join(parts) or "ComfyUI reported an execution error"


class ComfyClient:
    def __init__(self, base_url: str, client_id: str | None = None, timeout: float = 60.0):
        self.base_url = (base_url or "").rstrip("/")
        self.client_id = client_id or uuid.uuid4().hex
        self.timeout = timeout

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            async with self._http() as http:
                return await http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ComfyError(f"engine unreachable ({self.base_url}): {exc}") from exc

    # -- submit / poll ---------------------------------------------------------

    async def submit(self, graph: dict) -> str:
        """POST the graph; return the prompt_id or raise ComfyError with details."""
        resp = await self._request(
            "POST", "/prompt", json={"prompt": graph, "client_id": self.client_id}
        )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200:
            raise ComfyError(
                extract_error(data) or f"ComfyUI /prompt returned HTTP {resp.status_code}"
            )
        if data.get("node_errors"):
            raise ComfyError(extract_error(data))
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise ComfyError("ComfyUI /prompt response had no prompt_id")
        return str(prompt_id)

    async def history(self, prompt_id: str) -> dict | None:
        """The /history entry for this prompt, or None while still executing."""
        resp = await self._request("GET", f"/history/{prompt_id}")
        if resp.status_code != 200:
            raise ComfyError(f"ComfyUI /history returned HTTP {resp.status_code}")
        data = resp.json() or {}
        entry = data.get(prompt_id)
        return entry if isinstance(entry, dict) else None

    async def queue_position(self, prompt_id: str) -> int | None:
        """0 = running, N>=1 = waiting behind N-1 others, None = not in queue."""
        resp = await self._request("GET", "/queue")
        if resp.status_code != 200:
            return None
        data = resp.json() or {}
        for item in data.get("queue_running") or []:
            if len(item) > 1 and item[1] == prompt_id:
                return 0
        for i, item in enumerate(data.get("queue_pending") or []):
            if len(item) > 1 and item[1] == prompt_id:
                return i + 1
        return None

    async def wait_for(
        self,
        prompt_id: str,
        poll_interval: float = 1.0,
        on_status: Callable[[int | None], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict:
        """Poll /history until the prompt completes; return the history entry.

        `on_status` receives the queue position (0=running) on each empty poll.
        `should_cancel` turning true interrupts the prompt and raises ComfyCancelled.
        """
        while True:
            if should_cancel is not None and should_cancel():
                await self.cancel(prompt_id)
                raise ComfyCancelled(f"prompt {prompt_id} cancelled")
            entry = await self.history(prompt_id)
            if entry is not None:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    raise ComfyError(_history_error(entry))
                if status.get("completed") or entry.get("outputs"):
                    return entry
            elif on_status is not None:
                try:
                    on_status(await self.queue_position(prompt_id))
                except ComfyError:
                    pass
            await asyncio.sleep(poll_interval)

    async def cancel(self, prompt_id: str) -> None:
        """Best-effort: drop the prompt from the queue and interrupt execution."""
        for method, path, kwargs in (
            ("POST", "/queue", {"json": {"delete": [prompt_id]}}),
            ("POST", "/interrupt", {}),
        ):
            try:
                await self._request(method, path, **kwargs)
            except ComfyError:
                pass

    # -- files -----------------------------------------------------------------

    async def download(
        self, filename: str, subfolder: str = "", folder_type: str = "output", dest: Path = None
    ) -> bytes:
        """GET /view; write to `dest` when given. Returns the raw bytes."""
        resp = await self._request(
            "GET",
            "/view",
            params={"filename": filename, "subfolder": subfolder, "type": folder_type},
        )
        if resp.status_code != 200:
            raise ComfyError(
                f"ComfyUI /view returned HTTP {resp.status_code} for {subfolder}/{filename}"
            )
        if dest is not None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.content)
        return resp.content

    async def upload_image(
        self, data: bytes, name: str, subfolder: str = "", overwrite: bool = True
    ) -> dict:
        """POST /upload/image (multipart). Returns ComfyUI's {name, subfolder, type}."""
        resp = await self._request(
            "POST",
            "/upload/image",
            files={"image": (name, data, "application/octet-stream")},
            data={"overwrite": "true" if overwrite else "false", "subfolder": subfolder},
        )
        if resp.status_code != 200:
            raise ComfyError(f"ComfyUI /upload/image returned HTTP {resp.status_code}")
        return resp.json()

    # -- model discovery ---------------------------------------------------------

    async def object_info(self, class_type: str | None = None) -> dict:
        """GET /object_info (optionally scoped to one class), cached for 60s."""
        key = (self.base_url, class_type or "")
        now = time.monotonic()
        hit = _object_info_cache.get(key)
        if hit is not None and now - hit[0] < OBJECT_INFO_TTL_S:
            return hit[1]
        path = f"/object_info/{class_type}" if class_type else "/object_info"
        resp = await self._request("GET", path)
        if resp.status_code != 200:
            raise ComfyError(f"ComfyUI {path} returned HTTP {resp.status_code}")
        data = resp.json() or {}
        _object_info_cache[key] = (now, data)
        return data

    async def model_enum(self, class_type: str, input_name: str) -> list[str]:
        """The dropdown choices for e.g. ("LoraLoader", "lora_name").

        [] when the node class or input is unknown to this ComfyUI install.
        """
        info = await self.object_info(class_type)
        node = info.get(class_type) or {}
        inputs = node.get("input") or {}
        for section in ("required", "optional"):
            spec = (inputs.get(section) or {}).get(input_name)
            if (
                isinstance(spec, list)
                and spec
                and isinstance(spec[0], list)
            ):
                return [str(x) for x in spec[0]]
        return []
