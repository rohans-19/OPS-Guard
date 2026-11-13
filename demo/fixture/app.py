"""Tiny HTTP workload: no Kubernetes credentials and no agent tools."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path

service = os.environ["FIXTURE_SERVICE"]
print(os.environ.get("FIXTURE_LOG_LINE") or "Application started. No incident override.", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        config = Path("/settings/mode")
        mode = config.read_text().strip() if config.exists() else "enabled"
        version = Path("/app/version").read_text().strip()
        if self.path == "/info":
            body = json.dumps({"service":service, "version":version, "mode":mode})
            status = 200
        else:
            body = service
            status = 503 if self.path == "/ready" and mode != "enabled" else 200
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_):
        pass


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
