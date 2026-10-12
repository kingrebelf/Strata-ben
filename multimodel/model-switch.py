#!/usr/bin/env python3
"""model-switch.py - the one front on port 8080.  Exactly one model backend runs at a time (the launchers stop one
before starting the next); this process only forwards to it and says which one it is.

  backend "strata" (Qwen3.8, Strata's serve/server.py on 127.0.0.1:8181): every request passes through unchanged,
      including the Decisions API (/v1/decide and /v1/systemone), the web UI and the API key check.  The UI page gets one extra
      line in the Monitor tab ("Running model: ...") injected on the way through; Strata's files are not changed.
  backend "llama" (DeepSeek / GLM, llama-server on 127.0.0.1:8182): /v1/* passes through after the same API key
      check; /v1/decide and /v1/systemone run here with Strata's own serve/systemone.py (imported, unchanged) and
      llama-server's json_schema output; /v1/status and / are answered here.

GET /v1/active-model (no key) -> {"name", "label", "backend", "since", "healthy"}.
The active backend is read from STATE/active.json, written by the launchers.
"""
import argparse
import hmac
import html
import http.client
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # this file lives in <repo>/multimodel
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
       "transfer-encoding", "upgrade"}
DIALECTS = ["/v1/chat/completions", "/v1/messages", "/v1/responses", "/v1/systemone"]

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="0.0.0.0")
ap.add_argument("--port", type=int, default=8080)
ap.add_argument("--state", default=os.path.expanduser("~/.local/state/llm-launchers/active.json"))
ap.add_argument("--strata-dir", default=os.environ.get("STRATA_DIR") or REPO_DIR,
                help="the Strata checkout (its serve/systemone.py runs the Decisions API on llama.cpp models)")
ap.add_argument("--strata-config", default=os.environ.get("STRATA_CONFIG"),
                help="the API key is read from this config's api_key (the same key Strata uses); "
                     "default <strata-dir>/strata-iq3_s.json")
A = ap.parse_args()
A.strata_config = A.strata_config or os.path.join(A.strata_dir, "strata-iq3_s.json")

sys.path.insert(0, A.strata_dir)
from serve import systemone                       # noqa: E402  the Decisions API module, used as-is
from serve.structured import StructuredOutputError  # noqa: E402


def load_keys():
    try:
        k = json.load(open(A.strata_config)).get("api_key", "")
    except Exception:
        return []
    if isinstance(k, str):
        k = [x.strip() for x in k.split(",")]
    return [x for x in k if isinstance(x, str) and x.strip()]


KEYS = load_keys()


def active():
    try:
        with open(A.state) as f:
            st = json.load(f)
        if st.get("backend") in ("strata", "llama") and st.get("url"):
            return st
    except Exception:
        pass
    return None


def backend_healthy(st):
    try:
        u = urlsplit(st["url"])
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=2)
        c.request("GET", "/health")
        ok = c.getresponse().status == 200
        c.close()
        return ok
    except Exception:
        return False


def llama_json(st, method, path, body=None, timeout=900):
    u = urlsplit(st["url"])
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
    c.request(method, path, body=json.dumps(body) if body is not None else None,
              headers={"Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    c.close()
    if r.status != 200:
        raise ConnectionError(f"{path}: HTTP {r.status} {data[:300]!r}")
    return json.loads(data)


def reasoning_defaults(st, body):
    """Thinking control for /v1/chat/completions on a llama-server model.

    - The client turned thinking off (chat_template_kwargs.enable_thinking=false or reasoning_effort="none"): also
      send reasoning_budget_tokens=0, because some templates (GLM-5.3) always open a think block and ignore
      enable_thinking, so the hidden thinking would otherwise eat the visible answer.
    - The client said nothing about thinking: apply the model's defaults from active.json ("chat_defaults", e.g.
      {"reasoning_effort": "low", "reasoning_budget_tokens": 2048}).
    - Anything the client set is kept.  The thinking itself comes back in message.reasoning_content.
    """
    try:
        req = json.loads(body or b"{}")
    except ValueError:
        return body
    if not isinstance(req, dict):
        return body
    kw = req.get("chat_template_kwargs") or {}
    budget_keys = ("reasoning_budget_tokens", "thinking_budget_tokens")
    off = kw.get("enable_thinking") is False or kw.get("thinking") is False or req.get("reasoning_effort") == "none"
    said = off or "reasoning_effort" in req or any(k in req for k in budget_keys) or \
        "enable_thinking" in kw or "thinking" in kw
    if off and not any(k in req for k in budget_keys):
        req["reasoning_budget_tokens"] = 0
    elif not said:
        for k, v in (st.get("chat_defaults") or {}).items():
            req.setdefault(k, v)
    else:
        return body
    return json.dumps(req).encode()


INDICATOR_JS = r"""
(function () {
  function put(m) {
    var card = document.querySelector('#view-monitor .state-card');
    if (!card) return;
    var row = document.getElementById('running-model-row');
    if (!row) {
      row = document.createElement('div');
      row.id = 'running-model-row';
      row.className = 'state-card__row';
      row.innerHTML = '<span>Running model: <b id="running-model-name"></b></span><span class="muted" id="running-model-since"></span>';
      card.insertBefore(row, card.children[1] || null);
    }
    document.getElementById('running-model-name').textContent = m.label || m.name || 'none';
    document.getElementById('running-model-since').textContent = m.since ? 'since ' + new Date(m.since * 1000).toLocaleString() : '';
  }
  function poll() {
    fetch('/v1/active-model', {cache: 'no-store'}).then(function (r) { return r.json(); }).then(put).catch(function () {});
  }
  poll(); setInterval(poll, 10000);
})();
"""


def status_page(st, healthy):
    name = html.escape((st or {}).get("label") or (st or {}).get("name") or "no model running")
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(st["since"])) if st and st.get("since") else "-"
    state = "ready" if healthy else ("starting or stopped" if st else "stopped")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Model server</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="10">
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#222;background:#fafafa}}
.card{{background:#fff;border:1px solid #ddd;border-radius:10px;padding:18px 22px}}td{{padding:4px 12px 4px 0}}
code{{background:#eee;padding:1px 5px;border-radius:4px}}</style></head><body><div class="card">
<h2>Running model: {name}</h2><table>
<tr><td>Status</td><td>{state}</td></tr><tr><td>Engine</td><td>{html.escape((st or {}).get("backend", "-"))}</td></tr>
<tr><td>Since</td><td>{since}</td></tr>
<tr><td>API</td><td><code>/v1/chat/completions</code>, <code>/v1/messages</code>, <code>/v1/decide</code>, <code>/v1/systemone</code></td></tr>
</table><p>The Strata chat and monitor pages are available while Qwen3.8 (Strata) is the running model.</p>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "model-switch"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ------------------------------------------------------------------ helpers
    def _body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            out = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return out
                out += self.rfile.read(size)
                self.rfile.readline()
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, code, data, ctype="application/json"):
        if not isinstance(data, bytes):
            data = (json.dumps(data) if ctype == "application/json" else data).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code, kind, msg):
        self._send(code, {"error": {"type": kind, "message": msg}})

    def _authorized(self):
        if not KEYS:
            return True
        got = self.headers.get("x-api-key", "")
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
        return any(hmac.compare_digest(got.encode(), k.encode()) for k in KEYS)

    def _proxy(self, st, body, inject=False):
        u = urlsplit(st["url"])
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        if inject:
            headers.pop("Accept-Encoding", None)
        headers["Content-Length"] = str(len(body))
        try:
            c = http.client.HTTPConnection(u.hostname, u.port, timeout=3600)
            c.request(self.command, self.path, body=body if body else None, headers=headers)
            r = c.getresponse()
        except Exception as e:
            return self._error(503, "server_error", f"the model ({st.get('label') or st.get('name')}) is not reachable "
                                                   f"yet - it may still be starting: {e}")
        ctype = r.getheader("Content-Type", "")
        if inject and r.status == 200 and ctype.startswith("text/html"):
            page = r.read().decode("utf-8", "replace")
            tag = "<script>" + INDICATOR_JS + "</script>"
            page = page.replace("</body>", tag + "</body>") if "</body>" in page else page + tag
            data = page.encode()
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in HOP and k.lower() != "content-length":
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            c.close()
            return
        self.send_response(r.status)
        length = r.getheader("Content-Length")
        for k, v in r.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        if length is None:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
                if not chunk:
                    break
                if length is None:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if length is None:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            c.close()

    # ------------------------------------------------------------------ the Decisions API on a llama-server model
    def _decide(self, st, body):
        try:
            req = json.loads(body or b"{}")
            state, questions = systemone.check_request(req)

            def count(s):
                return len(llama_json(st, "POST", "/tokenize", {"content": s}, timeout=30)["tokens"])
            systemone.check_budget(state, questions, count)
        except ValueError as e:
            return self._error(400, "invalid_request_error", str(e))
        messages = [{"role": "system", "content": systemone.SYSTEM},
                    {"role": "user", "content": systemone.build_prompt(state, questions)}]
        payload = {"messages": messages, "stream": False, "max_tokens": 4096,
                   "chat_template_kwargs": {"enable_thinking": False, "thinking": False},
                   "reasoning_budget_tokens": 0,   # no thinking before the JSON (templates that always think: GLM)
                   "response_format": {"type": "json_schema", "json_schema": {
                       "name": "systemone", "strict": True, "schema": systemone.build_schema(questions)}}}
        for k in ("temperature", "top_p", "seed"):
            if k in req:
                payload[k] = req[k]
        err = None
        for _attempt in range(2):   # one retry when the answer does not validate (the schema's ranges are advisory)
            try:
                out = llama_json(st, "POST", "/v1/chat/completions", payload)
            except Exception as e:
                return self._error(503, "server_error", f"the model did not answer: {e}")
            try:
                text = out["choices"][0]["message"].get("content") or ""
                usage = out.get("usage", {})
                resp = systemone.respond(json.loads(text), questions, st.get("name"),
                                         usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
                return self._send(200, resp)
            except (StructuredOutputError, ValueError, KeyError, IndexError) as e:
                err = e
        self._error(502, "server_error", f"the model's answer was not usable: {err}")

    # ------------------------------------------------------------------ routing
    def _handle(self):
        path = urlsplit(self.path).path
        body = self._body() if self.command in ("POST", "PUT", "PATCH", "DELETE") else b""
        st = active()
        if path == "/v1/active-model":
            if not st:
                return self._send(200, {"name": None, "label": "no model running", "backend": None,
                                        "since": None, "healthy": False})
            return self._send(200, {"name": st.get("name"), "label": st.get("label"), "backend": st["backend"],
                                    "since": st.get("since"), "healthy": backend_healthy(st)})
        if not st:
            if self.command == "GET" and path in ("/", "/index.html"):
                return self._send(200, status_page(None, False), "text/html; charset=utf-8")
            return self._error(503, "server_error", "no model is running; start one with a launcher")
        if st["backend"] == "strata":
            return self._proxy(st, body, inject=(self.command == "GET" and path in ("/", "/index.html")))
        # llama-server backend
        if self.command == "GET" and path in ("/", "/index.html", "/api-monitor"):
            return self._send(200, status_page(st, backend_healthy(st)), "text/html; charset=utf-8")
        if path == "/health":
            return self._proxy(st, body)
        if path.startswith("/v1/") and not self._authorized():
            return self._error(401, "authentication_error", "a valid API key is required")
        if path == "/v1/status":
            return self._send(200, {"model": st.get("name"), "label": st.get("label"), "service": "llama.cpp",
                                    "dialects": DIALECTS, "healthy": backend_healthy(st)})
        if path in ("/v1/decide", "/v1/systemone") and self.command == "POST":
            return self._decide(st, body)
        if path == "/v1/chat/completions" and self.command == "POST":
            body = reasoning_defaults(st, body)
        return self._proxy(st, body)

    def do_GET(self):
        self._handle()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_GET

    def do_OPTIONS(self):
        st = active()
        if st and st["backend"] == "strata":
            return self._proxy(st, b"")
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, x-api-key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    srv = Server((A.host, A.port), Handler)
    print(f"model-switch on {A.host}:{A.port}, state {A.state}, {len(KEYS)} API key(s)", flush=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()
