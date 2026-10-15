#!/usr/bin/env python3
import http.server

PORT = 4747
BIND = "0.0.0.0"
DOC = "/home/max/Desktop/Strata/docs/decisions-api.html"


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