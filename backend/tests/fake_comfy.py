# OWNED-BY: engine-agent
"""In-process fake ComfyUI for tests: a starlette app served by a uvicorn thread.

Implements /prompt, /history/{id}, /queue, /view, /object_info[/{class}],
/upload/image and /interrupt, returning a tiny real PNG for image outputs.

Usage: `from fake_comfy import fake_comfy` in a test module — the fixture
starts a server on an ephemeral localhost port and exposes `.url` + `.state`.
"""

import json
import socket
import threading
import time
import uuid
from io import BytesIO
from pathlib import Path

import pytest
import uvicorn
from PIL import Image
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

REPO_WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"

#: character LoRAs available in the fake dropdown out of the box
DEFAULT_CHARACTER_LORAS = [
    "characters/hero_v1.safetensors",
    "characters/rival_v1.safetensors",
]


def tiny_png() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (64, 48), (200, 120, 40)).save(buf, format="PNG")
    return buf.getvalue()


class FakeComfyState:
    def __init__(self) -> None:
        #: "ClassType.input_name" -> dropdown enum
        self.models: dict[str, list[str]] = {
            "LoraLoader.lora_name": list(DEFAULT_CHARACTER_LORAS),
        }
        #: node classes this fake install "has" beyond those implied by
        #: self.models (availability checks every graph class via /object_info)
        self.node_classes: set[str] = set()
        self.prompts: dict[str, dict] = {}  # prompt_id -> submitted graph
        self.order: list[str] = []
        self.polls: dict[str, int] = {}  # prompt_id -> history polls so far
        self.polls_before_done = 0  # history polls returning {} before completion
        self.fail_submit_error: str | None = None  # next /prompt 400s with this message
        self.uploads: list[str] = []
        self.interrupts = 0
        self.deleted: list[str] = []
        self.request_counts: dict[str, int] = {}
        self.png = tiny_png()

    def count(self, path: str) -> None:
        self.request_counts[path] = self.request_counts.get(path, 0) + 1

    def allow_pack_models(self, workflows_dir: Path = REPO_WORKFLOWS) -> None:
        """Union the repo packs' required_models into the enums and their
        graphs' node classes into the installed-class set, so the shipped
        packs validate as fully available against this fake."""
        for manifest_path in sorted(workflows_dir.glob("*/manifest.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for spec, files in (manifest.get("required_models") or {}).items():
                enum = self.models.setdefault(spec, [])
                for f in files or []:
                    if f not in enum:
                        enum.append(f)
            graph_path = manifest_path.parent / (manifest.get("graph") or "graph.json")
            if graph_path.is_file():
                graph = json.loads(graph_path.read_text(encoding="utf-8"))
                self.allow_graph_nodes(graph)

    def allow_graph_nodes(self, graph: dict) -> None:
        """Mark every class a graph uses as installed on this fake."""
        self.node_classes.update(
            str(node.get("class_type", ""))
            for node in graph.values()
            if isinstance(node, dict) and node.get("class_type")
        )

    def is_finished(self, prompt_id: str) -> bool:
        return self.polls.get(prompt_id, 0) > self.polls_before_done

    def completed_entry(self, prompt_id: str) -> dict:
        graph = self.prompts.get(prompt_id) or {}
        for node_id, node in graph.items():
            if node.get("class_type") in ("SaveImage", "SaveVideo"):
                prefix = (node.get("inputs") or {}).get("filename_prefix", "comfy")
                subfolder, _, base = str(prefix).rpartition("/")
                return {
                    "status": {"status_str": "success", "completed": True, "messages": []},
                    "outputs": {
                        node_id: {
                            "images": [
                                {
                                    "filename": f"{base}_00001_.png",
                                    "subfolder": subfolder,
                                    "type": "output",
                                }
                            ]
                        }
                    },
                }
        return {
            "status": {"status_str": "success", "completed": True, "messages": []},
            "outputs": {},
        }

    def object_info_payload(self, class_type: str | None = None) -> dict:
        by_class: dict[str, dict[str, list[str]]] = {}
        for spec, files in self.models.items():
            cls, _, inp = spec.partition(".")
            by_class.setdefault(cls, {})[inp] = list(files)
        payload = {
            cls: {"input": {"required": {inp: [enum] for inp, enum in inputs.items()}}}
            for cls, inputs in by_class.items()
        }
        for cls in self.node_classes:
            payload.setdefault(cls, {"input": {"required": {}}})
        if class_type is not None:
            return {class_type: payload[class_type]} if class_type in payload else {}
        return payload


def build_app(state: FakeComfyState) -> Starlette:
    async def prompt(request):
        state.count("/prompt")
        body = await request.json()
        graph = body.get("prompt") or {}
        if state.fail_submit_error:
            message = state.fail_submit_error
            state.fail_submit_error = None
            return JSONResponse(
                {
                    "error": {
                        "type": "prompt_outputs_failed_validation",
                        "message": "Prompt outputs failed validation",
                    },
                    "node_errors": {
                        "3": {
                            "class_type": "KSampler",
                            "errors": [{"message": message, "details": ""}],
                        }
                    },
                },
                status_code=400,
            )
        prompt_id = uuid.uuid4().hex
        state.prompts[prompt_id] = graph
        state.order.append(prompt_id)
        state.polls[prompt_id] = 0
        return JSONResponse(
            {"prompt_id": prompt_id, "number": len(state.order), "node_errors": {}}
        )

    async def history(request):
        state.count("/history")
        prompt_id = request.path_params["prompt_id"]
        if prompt_id not in state.prompts:
            return JSONResponse({})
        state.polls[prompt_id] = state.polls.get(prompt_id, 0) + 1
        if not state.is_finished(prompt_id):
            return JSONResponse({})
        return JSONResponse({prompt_id: state.completed_entry(prompt_id)})

    async def queue(request):
        state.count("/queue")
        if request.method == "POST":
            body = await request.json()
            state.deleted.extend(body.get("delete") or [])
            return JSONResponse({})
        unfinished = [pid for pid in state.order if not state.is_finished(pid)]
        running = [[i, pid] for i, pid in enumerate(unfinished[:1])]
        pending = [[i + 1, pid] for i, pid in enumerate(unfinished[1:])]
        return JSONResponse({"queue_running": running, "queue_pending": pending})

    async def view(request):
        state.count("/view")
        return Response(state.png, media_type="image/png")

    async def object_info(request):
        state.count("/object_info")
        class_type = request.path_params.get("class_type")
        return JSONResponse(state.object_info_payload(class_type))

    async def upload_image(request):
        state.count("/upload/image")
        form = await request.form()
        upload = form["image"]
        state.uploads.append(upload.filename)
        return JSONResponse({"name": upload.filename, "subfolder": "", "type": "input"})

    async def interrupt(request):
        state.count("/interrupt")
        state.interrupts += 1
        return JSONResponse({})

    async def system_stats(request):
        state.count("/system_stats")
        return JSONResponse({"system": {"os": "fake"}})

    return Starlette(
        routes=[
            Route("/prompt", prompt, methods=["POST"]),
            Route("/history/{prompt_id}", history),
            Route("/queue", queue, methods=["GET", "POST"]),
            Route("/view", view),
            Route("/object_info", object_info),
            Route("/object_info/{class_type}", object_info),
            Route("/upload/image", upload_image, methods=["POST"]),
            Route("/interrupt", interrupt, methods=["POST"]),
            Route("/system_stats", system_stats),
        ]
    )


class FakeComfy:
    def __init__(self) -> None:
        self.state = FakeComfyState()
        self.app = build_app(self.state)
        self.url = ""
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        port = self._sock.getsockname()[1]
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._sock]}, daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("fake ComfyUI server failed to start")
            time.sleep(0.01)
        self.url = f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)


@pytest.fixture
def fake_comfy():
    from storybored.engine.comfy_client import clear_object_info_cache

    fake = FakeComfy()
    fake.state.allow_pack_models()
    clear_object_info_cache()
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()
        clear_object_info_cache()
