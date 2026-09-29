"""Shared helpers for the watchdog tests (not a test module itself).

Run: python -m unittest discover -s services/determined-watchdog/tests
The modules under ../build need `requests` (the watchdog image has it).
"""
import base64
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

BUILD_DIR = Path(__file__).resolve().parent.parent / "build"
if str(BUILD_DIR) not in sys.path:
    sys.path.insert(0, str(BUILD_DIR))

sys.dont_write_bytecode = True

# Obvious placeholders only.
FAKE_PASSWORD = "PLACEHOLDER-det-password"
FAKE_GRAFANA_TOKEN = "PLACEHOLDER-grafana-token"
FAKE_SLACK_PATH = "/services/TPLACEHOLDER/BPLACEHOLDER/placeholderwebhooksecret"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_token(claims=None, footer=None, expiry=None, sig=b"\x07" * 64) -> str:
    """Fake PASETO-like token: v2.public.<base64url(json + 64 sig bytes)>[.<footer>]."""
    if claims is None:
        claims = {"id": 42, "user_id": 1}
        if expiry is not None:
            claims["expiry"] = expiry
    body = json.dumps(claims).encode() + sig
    token = "v2.public." + b64url(body)
    if footer is not None:
        token += "." + b64url(footer.encode())
    return token


def go_time(dt: datetime) -> str:
    """Format like Go's time.Time JSON (RFC 3339 with nanoseconds)."""
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}123Z"


def token_expiring_in(delta: timedelta, now=None) -> str:
    now = now or datetime.now(timezone.utc)
    return make_token(expiry=go_time(now + delta))


def make_config(tmpdir, **overrides):
    from alert_config import Config

    tmpdir = Path(tmpdir)
    kwargs = dict(
        det_web="http://det.invalid:8080",
        det_username="admin",
        det_password=FAKE_PASSWORD,
        grafana_web="http://grafana.invalid:3000",
        grafana_api_token=FAKE_GRAFANA_TOKEN,
        alert_name="IdleKillAlert",
        slack_webhook_url="https://hooks.slack.invalid" + FAKE_SLACK_PATH,
        is_debug=False,
        base_path=tmpdir / "data",
        det_metrics_token_path=tmpdir / "secrets" / "token",
    )
    kwargs.update(overrides)
    Path(kwargs["base_path"]).mkdir(parents=True, exist_ok=True)
    Path(kwargs["det_metrics_token_path"]).parent.mkdir(parents=True, exist_ok=True)
    return Config(**kwargs)


class StubServer:
    """Tiny HTTP server in a thread. routes: {(METHOD, path): (status, body)}.

    body may be a dict/list (sent as JSON), bytes/str (sent as text) or a callable
    taking the request record and returning (status, body).
    """

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                record = {
                    "method": method,
                    "path": parts.path,
                    "query": parse_qs(parts.query),
                    "headers": dict(self.headers),
                    "body": body,
                }
                stub.requests.append(record)
                route = stub.routes.get((method, parts.path))
                if route is None:
                    status, payload = 404, {"message": "not found"}
                elif callable(route):
                    status, payload = route(record)
                else:
                    status, payload = route
                if isinstance(payload, (dict, list)):
                    data, ctype = json.dumps(payload).encode(), "application/json"
                else:
                    data = payload.encode() if isinstance(payload, str) else payload
                    ctype = "text/plain"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:%d/" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def requests_to(self, method, path):
        return [r for r in self.requests if r["method"] == method and r["path"] == path]


def closed_port_url(path="/"):
    """URL of a local port that nothing listens on (connection refused)."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d%s" % (port, path)
