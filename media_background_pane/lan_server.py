from __future__ import annotations

import json
import queue
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .config import thumb_cache_dir

WEB_ROOT = Path(__file__).with_name("web")
DEFAULT_PORT = 8745


def lan_ips() -> list[str]:
    found: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        ip = probe.getsockname()[0]
        probe.close()
        if ip and not ip.startswith("127.") and ip not in found:
            found.insert(0, ip)
    except OSError:
        pass
    return found or ["127.0.0.1"]


def lan_urls(port: int) -> list[str]:
    return [f"http://{ip}:{port}" for ip in lan_ips()]


class LanHub:
    """Thread-safe snapshot of pane state. HTTP thread only reads; Qt writes."""

    def __init__(self) -> None:
        self.commands: queue.Queue[dict[str, Any]] = queue.Queue()
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {}
        self._preview_jpg = b""
        self._program_jpg = b""

    def submit(self, command: dict[str, Any]) -> None:
        self.commands.put(command)

    def publish_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            self._state = state

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def set_jpeg(self, which: str, data: bytes) -> None:
        with self._lock:
            if which == "preview":
                self._preview_jpg = data
            else:
                self._program_jpg = data

    def jpeg(self, which: str) -> bytes:
        with self._lock:
            if which == "preview":
                return self._preview_jpg
            return self._program_jpg


class _Handler(BaseHTTPRequestHandler):
    server_version = "MediaBackgroundPaneRemote/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _hub(self) -> LanHub:
        return self.server.hub  # type: ignore[attr-defined]

    def _send(self, code: int, body: bytes, content_type: str, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", "public, max-age=120")
        else:
            self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path in ("/", "/index.html"):
            self._static("index.html", "text/html; charset=utf-8")
            return
        if path == "/app.css":
            self._static("app.css", "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self._static("app.js", "text/javascript; charset=utf-8")
            return
        if path == "/api/state":
            body = json.dumps(self._hub().state()).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if path == "/api/preview.jpg":
            self._jpeg(self._hub().jpeg("preview"))
            return
        if path == "/api/program.jpg":
            self._jpeg(self._hub().jpeg("program"))
            return
        if path.startswith("/thumbs/"):
            self._thumb(path[len("/thumbs/") :])
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/command":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 1_000_000:
            self._send(400, b"bad request", "text/plain")
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, b"bad json", "text/plain")
            return
        if not isinstance(payload, dict) or not payload.get("op"):
            self._send(400, b"bad command", "text/plain")
            return
        self._hub().submit(payload)
        self._send(204, b"", "text/plain")

    def _static(self, name: str, content_type: str) -> None:
        path = (WEB_ROOT / name).resolve()
        if not str(path).startswith(str(WEB_ROOT.resolve())) or not path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, path.read_bytes(), content_type)

    def _jpeg(self, data: bytes) -> None:
        if not data:
            self._send(204, b"", "image/jpeg")
            return
        self._send(200, data, "image/jpeg")

    def _thumb(self, name: str) -> None:
        if "/" in name or "\\" in name or not name.endswith(".jpg"):
            self._send(404, b"not found", "text/plain")
            return
        stem = name[:-4]
        if len(stem) != 40 or any(ch not in "0123456789abcdef" for ch in stem):
            self._send(404, b"not found", "text/plain")
            return
        path = (thumb_cache_dir() / name).resolve()
        cache = thumb_cache_dir().resolve()
        if not str(path).startswith(str(cache)) or not path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, path.read_bytes(), "image/jpeg", cache=True)


class LanServer:
    def __init__(self, hub: LanHub, port: int = DEFAULT_PORT) -> None:
        self.hub = hub
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str | None:
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", self.port), _Handler)
        except OSError:
            return None
        httpd.hub = self.hub
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="lan-remote", daemon=True)
        self._thread.start()
        return lan_urls(self.port)[0]

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None

    def urls(self) -> list[str]:
        return lan_urls(self.port)
