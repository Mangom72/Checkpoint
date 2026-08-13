"""A tiny in-process stand-in for the GitHub REST + GraphQL API, used by tests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class Reply:
    """Route value that pins an explicit status code / headers."""

    def __init__(self, status: int, payload=None, headers: dict | None = None):
        self.status = status
        self.payload = payload if payload is not None else {"message": "boom"}
        self.headers = headers or {}


class Sequence:
    """Route value that returns each item in turn (last item repeats)."""

    def __init__(self, *items):
        self.items = list(items)
        self.index = 0

    def next(self):
        item = self.items[min(self.index, len(self.items) - 1)]
        self.index += 1
        return item


def make_repo(full_name: str, clone_url: str, **overrides) -> dict:
    owner, name = full_name.split("/")
    repo = {
        "id": 1,
        "name": name,
        "full_name": full_name,
        "owner": {"login": owner},
        "private": False,
        "fork": False,
        "archived": False,
        "has_wiki": False,
        "has_discussions": True,
        "default_branch": "main",
        "size": 12,
        "clone_url": clone_url,
        "pushed_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
        "description": "test repo",
    }
    repo.update(overrides)
    return repo


class FakeGitHub:
    """Serves canned JSON. ``routes`` maps a path to a payload or callable."""

    def __init__(self, routes: dict[str, object], *, page_size: int = 2):
        self.routes = routes
        self.page_size = page_size
        self.requests: list[str] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # -- lifecycle ------------------------------------------------------
    def __enter__(self) -> "FakeGitHub":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # -- request handling -----------------------------------------------
    def _handler_class(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence stderr noise
                pass

            def _send(self, status: int, payload, extra_headers: dict | None = None):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-RateLimit-Remaining", "4999")
                self.send_header("X-RateLimit-Reset", "9999999999")
                for key, value in (extra_headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                api.requests.append(parsed.path)
                payload = api.routes.get(parsed.path)
                if isinstance(payload, Sequence):
                    payload = payload.next()
                if callable(payload):
                    payload = payload(parsed)
                if isinstance(payload, Reply):
                    self._send(payload.status, payload.payload, payload.headers)
                    return
                if payload is None:
                    self._send(200, [])
                    return
                if isinstance(payload, list):
                    self._send_page(parsed, payload)
                    return
                self._send(200, payload)

            def _send_page(self, parsed, items):
                query = parse_qs(parsed.query)
                page = int(query.get("page", ["1"])[0])
                size = api.page_size
                start = (page - 1) * size
                chunk = items[start : start + size]
                headers = {}
                if start + size < len(items):
                    nxt = f"{api.url}{parsed.path}?page={page + 1}&per_page={size}"
                    headers["Link"] = f'<{nxt}>; rel="next"'
                self._send(200, chunk, headers)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                api.requests.append("POST /graphql")
                self._send(200, {"data": api.routes.get("__graphql__", {})})

        return Handler
