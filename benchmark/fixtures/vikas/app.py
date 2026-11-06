"""Synthetic HTTP consumers for Vikas's ConfigMap impact cases."""

import os
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


CHECKS = {
    "checkout": {
        "/feature": ("feature_flag", ("enabled", "legacy_enabled")),
        "/metrics": ("metrics_enabled", ("enabled",)),
        "/health": ("shared_mode", ("enabled",)),
    },
    "storefront": {
        "/cache": ("cache_mode", ("normal",)),
        "/health": ("shared_mode", ("enabled",)),
    },
    "decoy": {"/feature": ("feature_flag", ("enabled",))},
}


def response_for_path(service: str, config_dir: Path, path: str) -> tuple[int, bytes]:
    if path == "/ready":
        return 200, b"ready\n"

    check = CHECKS[service].get(path)
    if check is None:
        return 404, b"not found\n"

    key, healthy_values = check
    try:
        value = (config_dir / key).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return 503, b"configuration unavailable\n"
    if value not in healthy_values:
        return 503, b"degraded\n"
    if path == "/metrics":
        return 200, b"opsguard_fixture_up 1\n"
    return 200, b"ok\n"


class FixtureHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, service: str, config_dir: Path, **kwargs):
        self.service = service
        self.config_dir = config_dir
        super().__init__(*args, **kwargs)

    def do_GET(self):
        path = self.path.partition("?")[0]
        status, body = response_for_path(self.service, self.config_dir, path)
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    handler = partial(
        FixtureHandler,
        service=os.environ["FIXTURE_SERVICE"],
        config_dir=Path("/etc/opsguard"),
    )
    with ThreadingHTTPServer(("0.0.0.0", 8080), handler) as server:
        server.serve_forever()
