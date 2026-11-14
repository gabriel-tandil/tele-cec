#!/usr/bin/env python3
"""Sirve tests/mock_netflix.html en cualquier ruta (para probar sin Netflix).

    python3 tests/serve_mock.py [puerto]   ->  http://www.netflix.com.localhost:8765/browse
"""
import http.server
import sys
from pathlib import Path

MOCK = (Path(__file__).parent / "mock_netflix.html").read_bytes()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/favicon"):
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(MOCK)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"Netflix simulado en http://www.netflix.com.localhost:{port}/browse", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
