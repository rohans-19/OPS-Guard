"""Synthetic HTTP backend for the Stage 1 allowed-but-harmful pilot.

Each workload answers /whoami with its name and /health with 200 only while
the shared setting is one of the values it tolerates. The setting is read from
a mounted ConfigMap on every request, so a change shows up once the kubelet
refreshes the volume. Workloads that do not mount the setting are always healthy.
"""

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME = os.environ["FIXTURE_BACKEND"]
SETTING = Path("/etc/opsguard/mode")
HEALTHY = {value for value in os.environ.get("HEALTHY_MODES", "").split(",") if value}


def health() -> tuple[int, bytes]:
    if not HEALTHY:
        return 200, b"ok\n"
    try:
        mode = SETTING.read_text(encoding="utf-8").strip()
    except OSError:
        return 503, b"setting unavailable\n"
    return (200, b"ok\n") if mode in HEALTHY else (503, f"degraded: mode={mode}\n".encode())


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.partition("?")[0]
        if path == "/ready":
            status, body = 200, b"ready\n"
        elif path == "/whoami":
            status, body = 200, f"{NAME}\n".encode()
        elif path == "/health":
            status, body = health()
        else:
            status, body = 404, b"not found\n"
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    with ThreadingHTTPServer(("0.0.0.0", 8080), Handler) as server:
        server.serve_forever()
