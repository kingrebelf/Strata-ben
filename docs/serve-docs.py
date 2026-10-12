#!/usr/bin/env python3
"""Serve docs/decisions-api.html on its own port:  python3 docs/serve-docs.py  (DOCS_PORT / DOCS_BIND to change)."""
import http.server
import os

PORT = int(os.environ.get("DOCS_PORT", "4747"))
BIND = os.environ.get("DOCS_BIND", "127.0.0.1")
DOC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "decisions-api.html")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/favicon"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            with open(DOC, "rb") as f:
                body = f.read()
        except OSError:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    http.server.ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
