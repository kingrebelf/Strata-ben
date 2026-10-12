"""llama_ui.py - Strata's web page (Chat / Monitor / About) for the llama.cpp models behind model-switch.py.

Strata's page (serve/web) is plain HTML + JavaScript that reads a few endpoints.  While a llama.cpp model runs, the
switch serves the same files and answers those endpoints in Strata's shapes:

  /health      model, context size, loaded               /metrics   live state, hardware, history, recent requests
  /settings    the "use for other apps too" defaults     /mcp       no MCP servers (an empty list)
  /config      404 (the run-config card is Strata's own, so the page hides it)

Hardware numbers come from nvidia-smi and /proc (sampled once a second while a llama.cpp model runs); the requests
table comes from the chat requests that pass through the switch.  Strata-only panels (expert cache, conversation
cache) have no counterpart and stay empty.
"""
import collections
import json
import os
import re
import subprocess
import threading
import time

HISTORY = 120   # seconds of sparkline history, like Strata's
WHOLE_DISK = re.compile(r"sd[a-z]+|vd[a-z]+|nvme\d+n\d+")   # whole disks, not their partitions


class Telemetry:
    """GPU / CPU / RAM / disk sampled once a second (only while asked for: start() / stop())."""

    def __init__(self):
        self.now, self.static = {}, {}
        self.history = collections.defaultdict(lambda: collections.deque(maxlen=HISTORY))
        self._lock = threading.Lock()
        self._run = False
        self._cpu = self._disk = None
        self.speeds = lambda: (0.0, 0.0)   # set by LlamaUI: the running request's decode / prefill tok/s

    def start(self):
        if not self._run:
            self._run = True
            threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._run = False

    def _loop(self):
        if not self.static:
            self.static = self._static()
        while self._run:
            try:
                now = self._sample()
                with self._lock:
                    self.now = now
                    for k in ("gpu_util", "gpu_mem_used", "gpu_temp", "gpu_power", "gpu_pcie_rx_mb", "cpu", "ram_used",
                              "disk_read_mb", "tok_s", "prefill_tok_s_mean"):
                        self.history[k].append(now.get(k))
            except Exception:
                pass
            time.sleep(1)

    def snapshot(self):
        with self._lock:
            return dict(self.now), {k: list(v) for k, v in self.history.items()}, dict(self.static)

    @staticmethod
    def _smi(fields):
        out = subprocess.run(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout
        return [[x.strip() for x in line.split(",")] for line in out.strip().splitlines()]

    def _static(self):
        st = {"psutil": True}
        try:
            names = [r[0] for r in self._smi("name")]
            st["gpu_name"] = " + ".join(names) if len(set(names)) > 1 else (f"{len(names)}x {names[0]}" if names else None)
            st["gpu_count"] = len(names)
        except Exception:
            pass
        try:
            info = open("/proc/cpuinfo").read()
            st["cpu_name"] = next((ln.split(":", 1)[1].strip() for ln in info.splitlines() if ln.startswith("model name")), None)
            st["threads"] = os.cpu_count()
            cores = {(p, c) for p, c in zip(
                [ln.split(":")[1].strip() for ln in info.splitlines() if ln.startswith("physical id")],
                [ln.split(":")[1].strip() for ln in info.splitlines() if ln.startswith("core id")])}
            st["cores"] = len(cores) or None
        except Exception:
            pass
        return st

    def _sample(self):
        num = lambda s: None if s in ("", "[N/A]", "N/A") else float(s)   # noqa: E731
        rows = self._smi("index,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,"
                         "pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current")
        gpus = []
        for r in rows:
            gpus.append({"index": int(r[0]), "util": num(r[1]), "mem_used": (num(r[2]) or 0) * 2 ** 20,
                         "mem_total": (num(r[3]) or 0) * 2 ** 20, "temp": num(r[4]), "power": num(r[5]),
                         "power_limit": num(r[6]), "gen": num(r[7]), "gen_max": num(r[8]), "width": num(r[9])})
        now = {"gpus": [{k: g[k] for k in ("index", "util", "mem_used", "mem_total", "temp", "power")} for g in gpus]}
        if gpus:
            now.update(gpu_util=sum(g["util"] or 0 for g in gpus) / len(gpus),
                       gpu_mem_used=sum(g["mem_used"] for g in gpus), gpu_mem_total=sum(g["mem_total"] for g in gpus),
                       gpu_temp=max(g["temp"] or 0 for g in gpus), gpu_power=sum(g["power"] or 0 for g in gpus),
                       gpu_power_limit=sum(g["power_limit"] or 0 for g in gpus),
                       gpu_pcie_gen=gpus[0]["gen"] and int(gpus[0]["gen"]),
                       gpu_pcie_gen_max=gpus[0]["gen_max"] and int(gpus[0]["gen_max"]),
                       gpu_pcie_width=gpus[0]["width"] and int(gpus[0]["width"]), gpu_pcie_rx_mb=None)
        # CPU busy share since the last sample
        f = [int(x) for x in open("/proc/stat").readline().split()[1:]]
        idle, total = f[3] + f[4], sum(f)
        if self._cpu:
            dt = total - self._cpu[1]
            now["cpu"] = round(100.0 * (1 - (idle - self._cpu[0]) / dt), 1) if dt > 0 else 0.0
        self._cpu = (idle, total)
        mem = {ln.split(":")[0]: int(ln.split()[1]) * 1024 for ln in open("/proc/meminfo")}
        now["ram_total"] = mem.get("MemTotal")
        now["ram_used"] = mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)
        # disk read/write MB/s over the whole-disk devices
        rd = wr = 0
        for ln in open("/proc/diskstats"):
            p = ln.split()
            if len(p) > 9 and WHOLE_DISK.fullmatch(p[2]):
                rd += int(p[5]) * 512
                wr += int(p[9]) * 512
        t = time.time()
        if self._disk:
            dt = t - self._disk[2]
            now["disk_read_mb"] = round((rd - self._disk[0]) / dt / 2 ** 20, 1)
            now["disk_write_mb"] = round((wr - self._disk[1]) / dt / 2 ** 20, 1)
        self._disk = (rd, wr, t)
        now["tok_s"], now["prefill_tok_s_mean"] = self.speeds()
        return now


class Requests:
    """The chat requests that went through the switch, newest first, in Strata's /metrics shapes."""

    def __init__(self):
        self.rows = collections.deque(maxlen=100)
        self.since = time.time()
        self.totals = {"since": self.since, "requests": 0, "prompt_tokens": 0, "reused": 0, "output_tokens": 0,
                       "prompt_ms": 0.0, "decode_ms": 0.0, "drafts_offered": 0, "drafts_accepted": 0}
        self.live = {"state": "idle", "queued": 0}
        self._lock = threading.Lock()
        self._busy = 0

    def begin(self, max_tokens=None):
        with self._lock:
            self._busy += 1
            self.live = {"state": "reading", "queued": max(0, self._busy - 1), "max_tokens": max_tokens,
                         "started": time.time()}

    def reading(self, processed, total, prefill_tok_s):
        with self._lock:
            if self.live.get("state") == "reading":
                self.live.update(prompt_read=processed, prompt_total=total, prompt_tokens=total,
                                 prefill_tok_s_mean=prefill_tok_s)

    def generating(self, generated, tok_s, prefill_tok_s):
        with self._lock:
            if self.live.get("state") in ("reading", "generating"):
                self.live["state"] = "generating"
                self.live["generated"] = generated
                if tok_s:
                    self.live["tok_s"] = tok_s
                if prefill_tok_s:
                    self.live["prefill_tok_s_mean"] = prefill_tok_s

    def speeds(self):
        """(decode tok/s, prefill tok/s) right now, for the sparklines: 0 when nothing runs."""
        with self._lock:
            s = self.live.get("state")
            return (self.live.get("tok_s") or 0.0 if s == "generating" else 0.0,
                    self.live.get("prefill_tok_s_mean") or 0.0 if s in ("reading", "generating") else 0.0)

    def end(self, timings, finish, started):
        with self._lock:
            self._busy = max(0, self._busy - 1)
            self.live = {"state": "idle" if not self._busy else "generating", "queued": max(0, self._busy - 1)}
            if not timings:
                return
            row = {"time": time.time(), "finish": finish or "stop",
                   "prompt_tokens": (timings.get("prompt_n") or 0) + (timings.get("cache_n") or 0),
                   "reused": timings.get("cache_n") or 0, "output_tokens": timings.get("predicted_n") or 0,
                   "decode_tok_s": timings.get("predicted_per_second"), "prompt_ms": timings.get("prompt_ms") or 0,
                   "hit_rate": None, "duration_s": time.time() - started}
            self.rows.appendleft(row)
            t = self.totals
            t["requests"] += 1
            t["prompt_tokens"] += row["prompt_tokens"]
            t["reused"] += row["reused"]
            t["output_tokens"] += row["output_tokens"]
            t["prompt_ms"] += timings.get("prompt_ms") or 0
            t["decode_ms"] += timings.get("predicted_ms") or 0

    def snapshot(self, all_rows=False):
        with self._lock:
            rows = list(self.rows)
            return dict(self.live), (rows if all_rows else rows[:12]), len(rows), dict(self.totals)


class LlamaUI:
    def __init__(self, strata_dir, state_dir):
        self.web = os.path.join(strata_dir, "serve", "web")
        self.settings_file = os.path.join(state_dir, "ui-settings.json")
        self.tel = Telemetry()
        self.req = Requests()
        self.tel.speeds = self.req.speeds
        self._props = {}
        self._props_for = None

    # ---- static files of Strata's page
    def static(self, path):
        """Path under /web/ (or the index page) -> (bytes, content type), or None."""
        rel = "index.html" if path in ("/", "/index.html") else path[len("/web/"):] if path.startswith("/web/") else None
        if rel is None:
            return None
        full = os.path.realpath(os.path.join(self.web, rel))
        if not full.startswith(os.path.realpath(self.web) + os.sep) or not os.path.isfile(full):
            return None
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css",
                 ".svg": "image/svg+xml", ".woff2": "font/woff2", ".woff": "font/woff", ".png": "image/png",
                 ".ico": "image/x-icon", ".json": "application/json"}.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            return f.read(), ctype

    # ---- the model's facts from llama-server /props
    def props(self, st, fetch):
        if self._props_for != (st.get("name"), st.get("since")):
            try:
                self._props = fetch(st, "GET", "/props", timeout=5)
                self._props_for = (st.get("name"), st.get("since"))
            except Exception:
                return {}
        return self._props

    def health(self, st, healthy, fetch, keyed):
        p = self.props(st, fetch) if healthy else {}
        n_ctx = (p.get("default_generation_settings") or {}).get("n_ctx") or p.get("n_ctx")
        return {"status": "ok" if healthy else "loading", "max_context": n_ctx, "model": st.get("name"),
                "images": bool((p.get("modalities") or {}).get("vision")), "api_key": keyed, "loaded": healthy,
                "service": "llama.cpp"}

    def metrics(self, st, healthy, fetch, all_rows):
        self.tel.start()
        p = self.props(st, fetch) if healthy else {}
        n_ctx = (p.get("default_generation_settings") or {}).get("n_ctx") or p.get("n_ctx")
        live, rows, kept, totals = self.req.snapshot(all_rows)
        now, hist, static = self.tel.snapshot()
        engine = {"model": st.get("name"), "max_context": n_ctx, "context": n_ctx,
                  "images": bool((p.get("modalities") or {}).get("vision")),
                  "version": "llama.cpp " + str(p.get("build_info") or "").strip(), "kv": None, "cvec": 0}
        started = live.get("started")
        live = {"state": live.get("state", "idle"), "queued": live.get("queued", 0), "phase": None,
                "prompt_tokens": live.get("prompt_tokens"), "prompt_read": live.get("prompt_read"),
                "prompt_total": live.get("prompt_total"), "generated": live.get("generated"),
                "max_tokens": live.get("max_tokens"), "elapsed_s": time.time() - started if started else None,
                "tok_s": live.get("tok_s"), "tok_s_mean": live.get("tok_s"),
                "prefill_tok_s_mean": live.get("prefill_tok_s_mean")}
        return {"engine": engine, "live": live, "requests": rows, "requests_kept": kept, "totals": totals,
                "conversation_cache": None, "hardware": now, "hardware_static": static, "history": hist,
                "time": time.time()}

    # ---- "Use for other apps too" (Strata's GET / POST /settings), kept per model
    def get_settings(self, model):
        d = self._load().get(model)
        return {"shared": bool(d), "defaults": d or {}}

    def set_settings(self, model, defaults):
        saved = self._load()
        if defaults:
            saved[model] = {k: v for k, v in defaults.items()
                            if k in ("reasoning_effort", "temperature", "top_p", "top_k", "seed", "max_tokens")}
        else:
            saved.pop(model, None)
        tmp = self.settings_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(saved, f)
        os.replace(tmp, self.settings_file)
        return self.get_settings(model)

    def shared_defaults(self, model):
        return self._load().get(model) or {}

    def _load(self):
        try:
            with open(self.settings_file) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
