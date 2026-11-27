"""Minimal Grocy stand-in for the container smoke test (stdlib only).

Answers the start-up checks (/api/system/info, /api/objects/locations/1) and
returns 400 for anything else.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import sys


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.headers.get("GROCY-API-KEY") != "smoke":
            self._send(401, {"error_message": "unauthorized"})
        elif self.path == "/api/system/info":
            self._send(200, {"grocy_version": {"Version": "smoke"}})
        elif self.path == "/api/objects/locations/1":
            self._send(200, {"id": 1, "name": "Pantry"})
        else:
            self._send(400, {"error_message": "not implemented in smoke server"})

    do_POST = do_GET


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 18080), Handler).serve_forever()
