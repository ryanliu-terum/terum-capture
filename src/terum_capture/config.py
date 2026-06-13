import json
import os
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

CONFIG_DIR = Path.home() / ".terum"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_config() -> dict | None:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_config(api_key: str, api_url: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"api_key": api_key, "api_url": api_url}, indent=2))
    if sys.platform != "win32":
        os.chmod(CONFIG_FILE, 0o600)


def delete_config() -> None:
    CONFIG_FILE.unlink(missing_ok=True)


CORS_ORIGIN = "https://app.terum.ai"


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict | None = None

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_POST(self):
        if self.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        if length > 8192:
            self.send_response(413)
            self.end_headers()
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            return
        _CallbackHandler.result = body
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, format, *args):
        pass


class CallbackServer:
    def __init__(self):
        self.port: int | None = None
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int | None:
        for port in (19284, 19285, 19286):
            try:
                self._httpd = HTTPServer(("127.0.0.1", port), _CallbackHandler)
                self.port = port
                break
            except OSError:
                continue
        else:
            print("Error: Could not bind to ports 19284-19286.")
            return None
        _CallbackHandler.result = None
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def wait_for_callback(self, expected_state: str, timeout: int = 120) -> dict | None:
        assert self._thread is not None and self._httpd is not None
        self._thread.join(timeout=timeout)
        self._httpd.shutdown()
        result = _CallbackHandler.result
        if result is None:
            print("Setup timed out. Run 'terum-capture setup' to try again.")
            return None
        if result.get("state") != expected_state:
            print("Error: State mismatch — possible CSRF. Run 'terum-capture setup' to try again.")
            return None
        token = result.get("token", "")
        if not token:
            print("Error: No token received. Run 'terum-capture setup' to try again.")
            return None
        return result
