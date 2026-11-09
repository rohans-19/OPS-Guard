"""Minimal HTTP fixture; attack text is printed as data and never executed."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import environ


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/ready":
            self.send_error(404)
            return
        body = (environ["FIXTURE_SERVICE"] + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    print("Readiness probe ready. Synthetic workload started.", flush=True)
    if environ.get("FIXTURE_LOG_LINE"):
        print(environ["FIXTURE_LOG_LINE"], flush=True)
    with ThreadingHTTPServer(("0.0.0.0", 8080), Handler) as server:
        server.serve_forever()
