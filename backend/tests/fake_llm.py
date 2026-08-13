"""A tiny fake OpenAI-compatible chat server for tests (stdlib only).

Usage:
    server = FakeLLM()
    server.start()
    server.queue('{"scenes": []}', "```json\\n{...}\\n```")
    ... point llm_base_url at server.base_url ...
    server.stop()

Each POST /v1/chat/completions pops the next queued content string (falling
back to `default_content`) and records the parsed request body in
`server.requests` for assertions.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_DRAFT = {
    "scenes": [
        {
            "title": "Default Scene",
            "slugline": "INT. NOWHERE - DAY",
            "shots": [
                {
                    "description": "A default wide shot.",
                    "shot_type": "WIDE",
                    "camera": "static",
                    "dialogue": "",
                    "duration_s": 4.0,
                    "characters": [],
                }
            ],
        }
    ]
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102 - silence test noise
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": "fake-model"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        server: FakeLLM = self.server.fake  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}
        if not self.path.endswith("/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        with server.lock:
            server.requests.append(body)
            if server.responses:
                content = server.responses.pop(0)
            else:
                content = server.default_content
        self._json(
            200,
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "model": body.get("model", "fake-model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            },
        )


class FakeLLM:
    def __init__(self) -> None:
        self.responses: list[str] = []
        self.requests: list[dict] = []
        self.default_content = json.dumps(DEFAULT_DRAFT)
        self.lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.fake = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def queue(self, *contents: str) -> None:
        with self.lock:
            self.responses.extend(contents)

    def start(self) -> "FakeLLM":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
