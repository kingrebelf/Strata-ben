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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llama_ui                                     # noqa: E402  Strata's web page for the llama.cpp models

UI = llama_ui.LlamaUI(A.strata_dir, os.path.dirname(os.path.abspath(A.state)))


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


def reasoning_defaults(st, body, shared=None):
    """Thinking control for /v1/chat/completions on a llama-server model.

    - Settings saved with "Use for other apps too" in the web page (`shared`) fill in what the request leaves out.
    - The model's "_effort_map" (chat_defaults in active.json) renames effort levels its template does not know
      (GLM-5.3 knows low / high / max: "medium" -> "high").
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
    defaults = st.get("chat_defaults") or {}
    for k, v in (shared or {}).items():
        req.setdefault(k, v)
    emap = defaults.get("_effort_map") or {}
    if req.get("reasoning_effort") in emap:
        req["reasoning_effort"] = emap[req["reasoning_effort"]]
    kw = req.get("chat_template_kwargs") or {}
    budget_keys = ("reasoning_budget_tokens", "thinking_budget_tokens")
    off = kw.get("enable_thinking") is False or kw.get("thinking") is False or req.get("reasoning_effort") == "none"
    said = off or "reasoning_effort" in req or any(k in req for k in budget_keys) or \
        "enable_thinking" in kw or "thinking" in kw
    if off and not any(k in req for k in budget_keys):
        req["reasoning_budget_tokens"] = 0
    elif not said:
        for k, v in defaults.items():
            if not k.startswith("_"):
                req.setdefault(k, v)
    elif defaults.get("_max_reasoning_budget") and not any(k in req for k in budget_keys):
        # thinking asked for without a budget (the web page's effort buttons): cap it, so a model that loops in its
        # thinking (seen with GLM-5.3 at 2 bits) cannot run until the context is full
        req["reasoning_budget_tokens"] = defaults["_max_reasoning_budget"]
    return json.dumps(req).encode()


class ChatWatcher:
    """Follows a /v1/chat/completions answer on its way through for the web page's Monitor.

    For streamed requests the switch asks llama-server for per-token timings and prompt progress
    (`injected`: timings_per_token, return_progress), feeds them to the Monitor live (reading %, decode and prefill
    tok/s), and takes those fields out again before the stream reaches the client, so clients get what they asked."""

    def __init__(self, injected=(), max_tokens=None):
        self.started = time.time()
        self.injected = set(injected)
        self.buf = b""
        self.chunks = 0
        self.timings = None
        self.finish = None
        UI.req.begin(max_tokens)

    def transform(self, data):
        """Bytes from llama-server -> bytes for the client (complete lines only; the rest waits for its newline)."""
        self.buf += data
        out = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            out.append(self._line(line) + b"\n")
        return b"".join(out)

    def flush(self):
        rest, self.buf = self.buf, b""
        return self._line(rest) if rest else b""

    def _line(self, raw):
        line = raw.strip()
        sse = line.startswith(b"data:")
        payload = line[5:].strip() if sse else line
        if not payload.startswith(b"{"):
            return raw
        try:
            j = json.loads(payload)
        except ValueError:
            return raw
        ch = (j.get("choices") or [{}])[0]
        if ch.get("finish_reason"):
            self.finish = ch["finish_reason"]
        pp = j.get("prompt_progress")
        if pp:
            done = max(0, (pp.get("processed") or 0) - (pp.get("cache") or 0))
            ms = pp.get("time_ms") or 0
            UI.req.reading(pp.get("processed"), pp.get("total"), done / (ms / 1000) if ms > 0 else None)
        t = j.get("timings")
        if t:
            self.timings = t
        delta = ch.get("delta") or {}
        if delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"):
            self.chunks += 1
            UI.req.generating((t or {}).get("predicted_n") or self.chunks,
                              (t or {}).get("predicted_per_second"), (t or {}).get("prompt_per_second"))
        if not sse or not self.injected:
            return raw
        changed = False
        if "return_progress" in self.injected and "prompt_progress" in j:
            del j["prompt_progress"]
            changed = True
        if "timings_per_token" in self.injected and "timings" in j and not ch.get("finish_reason"):
            del j["timings"]
            changed = True
        return b"data: " + json.dumps(j, ensure_ascii=False).encode() if changed else raw

    def close(self):
        UI.req.end(self.timings, self.finish, self.started)


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
</table><p>The chat and monitor page opens here once a model is running (start one with a launcher).</p>
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

    def _proxy(self, st, body, inject=False, watch=None):
        u = urlsplit(st["url"])
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        if inject or watch:
            headers.pop("Accept-Encoding", None)
        headers["Content-Length"] = str(len(body))
        try:
            c = http.client.HTTPConnection(u.hostname, u.port, timeout=3600)
            c.request(self.command, self.path, body=body if body else None, headers=headers)
            r = c.getresponse()
        except Exception as e:
            if watch:
                watch.close()
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
                if watch:
                    chunk = watch.transform(chunk)
                    if not chunk:
                        continue
                if length is None:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            tail = watch.flush() if watch else b""
            if tail:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(tail), tail) if length is None else tail)
            if length is None:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            c.close()
            if watch:
                watch.close()

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
            UI.tel.stop()
            return self._proxy(st, body, inject=(self.command == "GET" and path in ("/", "/index.html")))
        # llama-server backend: Strata's web page, served here, with its endpoints answered in Strata's shapes
        if self.command == "GET" and (path in ("/", "/index.html") or path.startswith("/web/")):
            page = UI.static(path)
            if page is None:
                if path.startswith("/web/"):
                    return self._error(404, "not_found_error", "no such file")
                return self._send(200, status_page(st, backend_healthy(st)), "text/html; charset=utf-8")
            data, ctype = page
            if ctype.startswith("text/html"):
                data = data.replace(b"</body>", b"<script>" + INDICATOR_JS.encode() + b"</script></body>")
            return self._send(200, data, ctype)
        if self.command == "GET" and path == "/api-monitor":
            return self._send(200, status_page(st, backend_healthy(st)), "text/html; charset=utf-8")
        if path == "/health":
            return self._send(200, UI.health(st, backend_healthy(st), llama_json, bool(KEYS)))
        ui_paths = ("/metrics", "/settings", "/config", "/mcp")
        if (path.startswith("/v1/") or path in ui_paths) and not self._authorized():
            return self._error(401, "authentication_error", "a valid API key is required")
        if path == "/metrics":
            return self._send(200, UI.metrics(st, backend_healthy(st), llama_json, "requests=all" in self.path))
        if path == "/settings":
            if self.command == "POST":
                try:
                    defaults = (json.loads(body or b"{}") or {}).get("defaults")
                except ValueError:
                    return self._error(400, "invalid_request_error", "send a JSON object")
                return self._send(200, UI.set_settings(st.get("name"), defaults))
            return self._send(200, UI.get_settings(st.get("name")))
        if path == "/mcp":
            return self._send(200, {"servers": [], "tools": 0})
        if path == "/config":   # Strata's run-config editor: not for llama.cpp models (the page hides the card)
            return self._error(404, "not_found_error", "the run config belongs to the Strata model")
        if path == "/v1/status":
            return self._send(200, {"model": st.get("name"), "label": st.get("label"), "service": "llama.cpp",
                                    "dialects": DIALECTS, "healthy": backend_healthy(st)})
        if path in ("/v1/decide", "/v1/systemone") and self.command == "POST":
            return self._decide(st, body)
        if path == "/v1/chat/completions" and self.command == "POST":
            body = reasoning_defaults(st, body, UI.shared_defaults(st.get("name")))
            injected, max_tokens = [], None
            try:
                req = json.loads(body)
                max_tokens = req.get("max_tokens") or req.get("max_completion_tokens")
                if req.get("stream"):   # live numbers for the Monitor; taken out of the stream again (ChatWatcher)
                    for k in ("timings_per_token", "return_progress"):
                        if k not in req:
                            req[k] = True
                            injected.append(k)
                    body = json.dumps(req).encode()
            except (ValueError, AttributeError):
                pass
            return self._proxy(st, body, watch=ChatWatcher(injected, max_tokens))
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
