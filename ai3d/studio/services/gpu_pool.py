from __future__ import annotations
import json, logging, os, re, signal, subprocess, threading, time
from pathlib import Path
from typing import Any
import urllib.error, urllib.request
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("gpu-pool")
ROOT = Path("/home/dashboardadmin/ai3d")
HOME = {0: ["llm"], 1: ["trellis"], 2: ["flux", "cutout"], 3: ["sound", "openjev"]}
# Keep each model's home GPU first. GPU 0 lends its LLM card only after other
# helpers, and the primary LLM weights are explicitly unloaded before reuse.
ELIGIBLE = {"trellis": [1, 2, 3, 0], "flux": [1, 2, 3, 0], "sound": [1, 2, 3, 0],
            "cutout": [1, 2, 3, 0], "llm": [1, 2]}
NEEDS = {"trellis": 30000, "flux": 22500, "sound": 18500, "cutout": 2000, "llm": 30000}
# GPU 3 keeps the small OpenJEV encoders resident while lending to TRELLIS.
# The 24 GiB gate matches observed TRELLIS worker use and preserves 8 GiB headroom.
GPU_NEEDS = {"trellis": {3: 24000}}
# After honoring explicit request priority and home-GPU work, serve TRELLIS
# backlog before borrowing another GPU for non-home Flux work.
MODEL_PRIORITY = {"trellis": 0, "flux": 1, "sound": 2, "cutout": 3, "llm": 4}
# The 500k-face / 4096px bake OOMed on GPU 3 with OpenJEV resident. Keep GPU 3
# for PBR work whose estimated texture/face cost is at most one quarter of it.
GPU3_PBR_BUDGET = 125000


def gpu_allowed_for_job(job: dict[str, Any], gpu: int) -> bool:
    if gpu != 3 or job["model"] != "trellis":
        return True
    marker = "-pbr-"
    if marker not in job["request_id"]:
        return True
    try:
        spec = job["request_id"].rsplit(marker, 1)[1].split(":", 1)[0].split("-")
        faces, texture_size = map(int, spec)
    except (IndexError, ValueError):
        return False
    estimated_cost = faces * (texture_size / 4096) ** 2
    return estimated_cost <= GPU3_PBR_BUDGET


def required_memory(model: str, gpu: int) -> int:
    return GPU_NEEDS.get(model, {}).get(gpu, NEEDS.get(model, 0))
PRIMARY = {"trellis": "http://127.0.0.1:9186", "flux": "http://127.0.0.1:9181",
           "cutout": "http://127.0.0.1:9188", "moss": "http://127.0.0.1:9183",
           "sa3": "http://127.0.0.1:9184", "llm": "http://127.0.0.1:8181"}
PORT = {"trellis": lambda g: 9270 + g, "flux": lambda g: 9280 + g,
        "sound": lambda g: 9290 + g, "cutout": lambda g: 9310 + g}
STATUS_PATH = Path("/home/dashboardadmin/ai3d/state/gpu-queue.json")
# Only requests whose client is actively polling can receive a lease. An
# abandoned entry at the head of the queue must not block live request workers.
CLAIM_POLL_TTL = 15

class Enqueue(BaseModel):
    model: str
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    home_gpu: int
    priority: int = Field(default=0, ge=0, le=100)

def service_names(mode: Any) -> list[str]:
    vals = mode if isinstance(mode, list) else [mode]
    out = []
    for v in vals:
        out.extend(["moss", "sa3"] if v == "sound" else ([v] if v in PRIMARY else []))
    return list(dict.fromkeys(out))

class Pool:
    def __init__(self):
        self.cv = threading.Condition(threading.RLock())
        self.jobs = {}
        self.gpus = {i: {"home_models": list(v), "models": list(v), "mode": list(v),
                         "active_job": None, "transitioning_to": None} for i, v in HOME.items()}
        self.children: dict[tuple[str, int], list[subprocess.Popen]] = {}
        self.child_logs: dict[tuple[str, int], list[Any]] = {}
        self.write()

    def write(self):
        with self.cv:
            queues = {}
            for model in ELIGIBLE:
                rows = [j for j in self.jobs.values() if j["model"] == model and
                        j["state"] in {"queued", "waiting_for_gpu", "switching", "running"}]
                rows.sort(key=lambda j: (j["state"] not in {"switching", "running"}, j["created_at"]))
                queues[model] = {"queued": sum(j["state"] in {"queued", "waiting_for_gpu"} for j in rows),
                    "running_requests": sum(j["state"] in {"switching", "running"} for j in rows),
                    "eligible_gpus": ELIGIBLE[model], "requests": [self.view(j) for j in rows]}
            workers = {}
            for gpu, w in self.gpus.items():
                j = self.jobs.get(w["active_job"]) or next((x for x in self.jobs.values()
                    if x.get("gpu") == gpu and x["state"] == "switching"), None)
                workers[str(gpu)] = {"home_models": w["home_models"], "current_models": w["models"],
                    "mode": "home" if w["mode"] == w["home_models"] else "borrowed",
                    "transitioning_to": w["transitioning_to"], "current_request_id": j["request_id"] if j else None,
                    "current_request_model": j["model"] if j else None}
            data = {"updated_at": time.time(), "workers": workers, "queues": queues,
                    "cpu": {"workers": 0, "running_jobs": []}}
            STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATUS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.chmod(tmp, 0o644)
            tmp.replace(STATUS_PATH)

    def view(self, j):
        waiting = j["state"] in {"queued", "waiting_for_gpu"}
        q = sorted((x for x in self.jobs.values() if x["model"] == j["model"] and
                    x["state"] in {"queued", "waiting_for_gpu"}),
                   key=lambda x: (x.get("priority", 0), x["created_at"]))
        pos = next((n for n, x in enumerate(q, 1) if x["request_id"] == j["request_id"]), None)
        attempts = ([{"target": f"GPU {j['gpu']}", "state": "loading" if j["state"] == "switching" else "running"}]
                    if j.get("gpu") is not None and j["state"] in {"switching", "running"} else [])
        return {"request_id": j["request_id"], "model": j["model"], "state": j["state"],
                "queue_position": pos if waiting else None, "queued_for_s": round(time.time()-j["created_at"], 1),
                "gpu": j.get("gpu"), "error": j.get("error"),
                "gpu_pending": waiting, "waiting_for_gpu": j["state"] == "waiting_for_gpu",
                "attempts": attempts}

    def is_home_wait(self, j):
        w = self.gpus[j["home_gpu"]]
        return j["model"] in w["home_models"] and w["active_job"] is None

    def submit(self, req: Enqueue):
        if (req.model not in ELIGIBLE or req.home_gpu not in HOME or
                (req.home_gpu not in ELIGIBLE[req.model] and req.model not in HOME[req.home_gpu])):
            raise HTTPException(422, "Invalid GPU queue model or home placement")
        with self.cv:
            cutoff = time.time() - 3600
            self.jobs = {rid: job for rid, job in self.jobs.items()
                         if job["state"] in {"queued", "waiting_for_gpu", "switching", "running"} or
                         job.get("finished_at", job.get("created_at", time.time())) > cutoff}
            if req.request_id not in self.jobs:
                self.jobs[req.request_id] = {"request_id": req.request_id, "model": req.model,
                    "home_gpu": req.home_gpu, "priority": req.priority, "created_at": time.time(),
                    "state": "queued", "gpu": None, "last_claim_at": None}
            self.cv.notify_all()
            self.write()
            return self.view(self.jobs[req.request_id])

    def choices_for(self, j, q):
        choices = []
        home = j["home_gpu"]
        candidates = (([home] if self.gpus[home]["active_job"] is None else []) +
                      [g for g in ELIGIBLE[j["model"]] if g != home])
        for g in candidates:
            w = self.gpus[g]
            if (g not in choices and w["active_job"] is None and w["transitioning_to"] is None
                    and gpu_allowed_for_job(j, g)):
                choices.append(g)
        # Keep a borrowed worker on its current model until a request for that GPU's
        # home model is waiting, avoiding repeated weight transfers.
        current = next((g for g in choices if j["model"] in self.gpus[g]["mode"]), None)
        if current is not None and not any(x["home_gpu"] == current and
                x["model"] in self.gpus[current]["home_models"] for x in q):
            choices.remove(current)
            choices.insert(0, current)
        return choices

    def claim(self, rid):
        with self.cv:
            j = self.jobs.get(rid)
            if not j:
                raise HTTPException(404, "Queue request not found")
            if j["state"] == "running":
                return {"state": "running", "gpu": j["gpu"]}
            if j["state"] == "switching":
                return {"state": "switching", "gpu": j["gpu"]}
            if j["state"] in {"succeeded", "failed", "cancelled"}:
                return {"state": j["state"], "gpu": j.get("gpu"), "error": j.get("error")}
            now = time.time()
            j["last_claim_at"] = now
            # A request enters arbitration once its API worker actually polls.
            # This prevents clients that are waiting behind a bounded executor
            # (or whose process disappeared) from holding the FIFO head forever.
            q = sorted((x for x in self.jobs.values()
                        if x["state"] in {"queued", "waiting_for_gpu"} and
                        now - (x.get("last_claim_at") or 0) <= CLAIM_POLL_TTL),
                       key=lambda x: (x.get("priority", 0), 0 if self.is_home_wait(x) else 1,
                                     MODEL_PRIORITY.get(x["model"], 99), x["created_at"]))
            selected = None
            choices = []
            for candidate in q:
                if candidate.get("retry_after", 0) > time.time():
                    continue
                available = self.choices_for(candidate, q)
                if available:
                    selected, choices = candidate, available
                    break
            if selected is None:
                for candidate in q:
                    candidate["state"] = "waiting_for_gpu"
                self.write()
                return {"state": "waiting_for_gpu", "queue_position": self.view(j)["queue_position"]}
            if selected is not j:
                can_run = bool(self.choices_for(j, q))
                if not can_run:
                    j["state"] = "waiting_for_gpu"
                    self.write()
                return {"state": "queued" if can_run else "waiting_for_gpu",
                        "queue_position": self.view(j)["queue_position"]}
            gpu = choices[0]
            old = self.gpus[gpu]["mode"]
            new = list(HOME[gpu]) if j["model"] in HOME[gpu] else j["model"]
            self.gpus[gpu]["transitioning_to"] = new
            j["state"] = "switching"
            j["gpu"] = gpu
            self.write()
        try:
            self.activate(gpu, old, new)
        except Exception as e:
            log.exception("GPU %d activation failed", gpu)
            with self.cv:
                self.gpus[gpu]["transitioning_to"] = None
                j["state"] = "waiting_for_gpu"
                j["gpu"] = None
                j["retry_after"] = time.time() + 30
                j["error"] = str(e)[:240]
                self.write()
            return {"state": "waiting_for_gpu", "queue_position": self.view(j)["queue_position"],
                    "error": str(e)[:240]}
        with self.cv:
            self.gpus[gpu].update(mode=new if isinstance(new, list) else [new],
                                  models=new if isinstance(new, list) else [new],
                                  active_job=rid, transitioning_to=None)
            j.update(state="running", gpu=gpu, started_at=time.time())
            self.write()
            return {"state": "running", "gpu": gpu}

    def activate(self, gpu, old, new):
        old_models = old if isinstance(old, list) else [str(old)]
        new_models = new if isinstance(new, list) else [str(new)]
        # Borrowed modes are normalized to one-item lists when committed; a
        # later request for that same model arrives as a string. Do not reload
        # it or wait for VRAM that the already-ready worker itself is using.
        if old_models == new_models: return
        # A worker that borrowed a GPU is also represented by a one-item list
        # after claim() commits it. Stop every departing worker, not just home
        # services; otherwise returning to home waits forever on the old clone's
        # VRAM and remains stuck in "Switching".
        for model in old_models:
            if model in new_models or model == "openjev":
                continue
            if gpu == 0 and model == "llm":
                self.call("llm", "offload", gpu)
            elif gpu == 1 and model == "trellis":
                self.systemctl("stop", "ai3d-trellis2-worker.service")
            elif gpu == 2 and model in {"flux", "cutout"}:
                self.call(model, "offload", gpu)
            elif gpu == 3 and model == "sound":
                self.call("moss", "offload", gpu)
                self.call("sa3", "offload", gpu)
            else:
                self.stop_worker(model, gpu)
        if isinstance(new, list):
            required = sum(required_memory(m, gpu) for m in new if m != "openjev")
            if required: self.wait_free(gpu, required)
            if gpu == 0 and "llm" in new:
                self.call("llm", "place", gpu)
            elif gpu == 1 and "trellis" in new:
                self.systemctl("start", "ai3d-trellis2-worker.service")
                self.wait_health("http://127.0.0.1:9186/health", lambda x: x.get("ok") is True, 1800)
            elif gpu == 2:
                for m in ("flux", "cutout"):
                    self.call(m, "place", gpu)
            elif gpu == 3:
                for m in ("moss", "sa3"):
                    self.call(m, "place", gpu)
        else:
            required = required_memory(str(new), gpu)
            if required: self.wait_free(gpu, required)
            self.start_worker(str(new), gpu)

    @staticmethod
    def systemctl(action, unit):
        r = subprocess.run(["systemctl", "--user", action, unit], capture_output=True, text=True, timeout=60)
        if r.returncode: raise RuntimeError((r.stderr or r.stdout or f"systemctl {action} failed")[:400])

    def wait_free(self, gpu, needed):
        end = time.monotonic() + 1800
        last = 0
        while time.monotonic() < end:
            r = subprocess.run(["nvidia-smi", "-i", str(gpu), "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=8, check=True)
            last = int(r.stdout.strip().splitlines()[0].split()[0])
            if last >= needed: return
            time.sleep(2)
        raise RuntimeError(f"GPU {gpu} has only {last} MiB free; {needed} MiB is required")

    @staticmethod
    def call(model, action, gpu):
        path = f"/internal/gpu-pool/{action}" if model == "llm" else f"/internal/{action}"
        req = urllib.request.Request(PRIMARY[model] + path,
            data=json.dumps({"gpu": gpu}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=1800) as r: result = json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RuntimeError(f"{model} worker {action} failed on GPU {gpu}: {e}") from e
        if result.get("ok") is not True: raise RuntimeError(f"{model} worker {action} failed: {result.get('error') or result}")

    def start_worker(self, model, gpu):
        if model == "llm":
            # Strata's main process owns these replicas and starts them after this GPU lease is granted.
            return
        key = (model, gpu)
        if key in self.children and all(p.poll() is None for p in self.children[key]): return
        port = PORT[model](gpu)
        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = str(gpu)
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        env["GPU_QUEUE_HOME_GPU"] = str(gpu)
        env["GPU_QUEUE_PHYSICAL_GPU"] = str(gpu)
        env["HF_HOME"] = "/home/dashboardadmin/.cache/huggingface"
        logs = ROOT / "state" / f"gpu-{model}-{gpu}.log"
        logs.parent.mkdir(parents=True, exist_ok=True)
        handle = open(logs, "ab", buffering=0)
        log_handles = [handle]
        procs = []
        if model == "trellis":
            # The home worker unit sets xformers explicitly. Borrowed workers are
            # started directly by this manager, so carry the same backend setting
            # instead of TRELLIS's flash_attn default (which is not installed in
            # this V100 environment).
            env["ATTN_BACKEND"] = "xformers"
            env["SPARSE_ATTN_BACKEND"] = "xformers"
            app, cwd, py, path = "worker_trellis2:app", ROOT / "src" / "TRELLIS.2", ROOT / "envs/trellis2/bin/python", \
                f"{ROOT}/studio/services:{ROOT}/src/TRELLIS.2"
        elif model == "flux":
            # Match the home service's dependency path; the API venv alone lacks Torch.
            app, cwd, py, path = "flux_api:app", ROOT, ROOT / "envs/flux2-api/bin/python", \
                f"{ROOT}/services:{ROOT}/envs/flux2-api/lib/python3.14/site-packages:/home/max/deepseek-v4/dashboard/flux2-venv/lib/python3.14/site-packages"
            env["FLUX_POOL_WORKER"] = "1"
        elif model == "cutout":
            env["CUTOUT_POOL_WORKER"] = "1"
            app, cwd, py, path = "cutout_api:app", ROOT / "cutout", ROOT / "envs/trellis2/bin/python", f"{ROOT}/cutout"
        elif model == "sound":
            sa3port = 9300 + gpu
            env_sa3 = env.copy()
            env_sa3["PYTHONPATH"] = f"{ROOT}/sfx"
            sa3log = open(ROOT / "state" / f"gpu-sa3-{gpu}.log", "ab", buffering=0)
            log_handles.append(sa3log)
            sa3 = subprocess.Popen([str(ROOT / "envs/sa3/bin/python"), "-m", "uvicorn", "sa3_worker:app",
                "--app-dir", str(ROOT / "sfx"), "--host", "127.0.0.1", "--port", str(sa3port), "--workers", "1"],
                cwd=ROOT / "sfx", env=env_sa3, stdout=sa3log, stderr=sa3log, start_new_session=True)
            try:
                self.wait_health(f"http://127.0.0.1:{sa3port}/health", lambda x: x.get("state") == "ready", 1800, sa3)
            except Exception:
                try: os.killpg(sa3.pid, signal.SIGTERM)
                except OSError: sa3.terminate()
                try: sa3.wait(timeout=10)
                except subprocess.TimeoutExpired: sa3.kill()
                for fh in log_handles:
                    fh.close()
                raise
            env["SA3_URL"] = f"http://127.0.0.1:{sa3port}"
            env["SFX_POOL_WORKER"] = "1"
            app, cwd, py, path = "sfx_api:app", ROOT / "sfx", ROOT / "envs/sfx/bin/python", f"{ROOT}/sfx"
            procs.append(sa3)
        else:
            raise RuntimeError(f"No worker adapter for {model}")
        env["PYTHONPATH"] = path
        try:
            proc = subprocess.Popen([str(py), "-m", "uvicorn", app, "--app-dir", str(cwd),
                "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
                cwd=cwd, env=env, stdout=handle, stderr=handle, start_new_session=True)
        except Exception:
            for p in procs:
                try: os.killpg(p.pid, signal.SIGTERM)
                except OSError: p.terminate()
            for fh in log_handles:
                fh.close()
            raise
        procs.append(proc)
        self.children[key] = procs
        self.child_logs[key] = log_handles
        check_url = f"http://127.0.0.1:{port}/health"
        pred = (lambda x: x.get("ok") is True)
        if model == "sound": pred = lambda x: x.get("model_state") == "ready"
        try:
            self.wait_health(check_url, pred, 1800, proc)
        except Exception:
            self.stop_worker(model, gpu)
            raise
        log.info("Started %s clone on GPU %s (port %s)", model, gpu, port)

    def stop_worker(self, model, gpu):
        if model == "llm":
            req = urllib.request.Request(PRIMARY["llm"] + "/internal/gpu-pool/stop",
                data=json.dumps({"gpu": gpu}).encode(), headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=1800) as response:
                    result = json.loads(response.read())
                if result.get("ok") is not True:
                    raise RuntimeError(result.get("error") or result)
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise RuntimeError(f"Strata replica stop failed on GPU {gpu}: {e}") from e
            return
        procs = self.children.pop((model, gpu), [])
        for p in procs:
            if p.poll() is None:
                try: os.killpg(p.pid, signal.SIGINT)
                except OSError: p.terminate()
        for p in procs:
            try: p.wait(timeout=120)
            except subprocess.TimeoutExpired:
                try: os.killpg(p.pid, signal.SIGKILL)
                except OSError: p.kill()
                p.wait(timeout=10)
        for handle in self.child_logs.pop((model, gpu), []):
            try: handle.close()
            except OSError: pass

    @staticmethod
    def wait_health(url, pred, seconds, proc=None):
        end = time.monotonic() + seconds
        last = None
        while time.monotonic() < end:
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(f"Worker exited with status {proc.returncode}")
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    value = json.loads(r.read())
                if pred(value): return value
                last = value
            except Exception as exc: last = str(exc)
            time.sleep(2)
        raise RuntimeError(f"Worker did not become ready at {url}: {last}")

    def phase(self, rid, phase):
        if phase not in {"loading", "running"}:
            raise HTTPException(422, "Invalid queue phase")
        with self.cv:
            j = self.jobs.get(rid)
            if not j:
                raise HTTPException(404, "Queue request not found")
            gpu = j.get("gpu")
            if gpu is None or self.gpus[gpu]["active_job"] != rid:
                raise HTTPException(409, "Queue request does not hold a GPU lease")
            j["state"] = "switching" if phase == "loading" else "running"
            self.write()
            return self.view(j)

    def release(self, rid, failed=False, error=None):
        with self.cv:
            j = self.jobs.get(rid)
            # Release is idempotent across scheduler restarts. A completed job
            # may outlive the manager process that originally granted its lease.
            if not j: return {"ok": True, "missing": True}
            g = j.get("gpu")
            if g is not None and self.gpus[g]["active_job"] == rid: self.gpus[g]["active_job"] = None
            j.update(state="failed" if failed else "succeeded", finished_at=time.time(), error=error)
            self.write()
            self.cv.notify_all()
        # A borrowed worker stays loaded until a request for that GPU's home model arrives.
        return {"ok": True}

    def snapshot(self):
        with self.cv:
            self.write()
            return json.loads(STATUS_PATH.read_text(encoding="utf-8"))

    def bootstrap(self):
        # A manager restart must not leave a borrowed native worker resident while
        # home models are restored.
        for gpu in (1, 2):
            try:
                req = urllib.request.Request(PRIMARY["llm"] + "/internal/gpu-pool/stop",
                    data=json.dumps({"gpu": gpu}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=120) as response:
                    response.read()
            except Exception:
                log.info("No stale Strata replica on GPU %s to stop", gpu)
        self.systemctl("start", "ai3d-trellis2-worker.service")
        self.wait_health("http://127.0.0.1:9186/health", lambda x: x.get("ok") is True, 1800)
        self.wait_health("http://127.0.0.1:9181/health", lambda x: x.get("ok") is True, 1800)
        self.wait_health("http://127.0.0.1:9188/health", lambda x: x.get("ok") is True, 1800)
        self.wait_health("http://127.0.0.1:9183/health", lambda x: x.get("model_state") == "ready", 1800)
        self.wait_health("http://127.0.0.1:9184/health", lambda x: x.get("state") == "ready", 1800)
        for model, gpu in (("flux", 2), ("cutout", 2), ("moss", 3), ("sa3", 3)):
            self.call(model, "place", gpu)
        self.wait_health("http://127.0.0.1:8181/health", lambda x: x.get("status") == "ok", 1800)
        self.call("llm", "place", 0)
        with self.cv:
            for gpu, models in HOME.items():
                self.gpus[gpu].update(mode=list(models), models=list(models),
                                      active_job=None, transitioning_to=None)
            self.write()

pool = Pool()
app = FastAPI(title="Shared GPU queue")

@app.on_event("startup")
def bootstrap_pool():
    pool.bootstrap()

@app.get("/health")
def health(): return {"ok": True, "queue_scheduler": "ready"}
@app.post("/queue/enqueue")
def enqueue(req: Enqueue): return pool.submit(req)
@app.post("/queue/claim/{rid}")
def claim(rid: str): return pool.claim(rid)
@app.post("/queue/phase/{rid}")
def phase(rid: str, body: dict):
    return pool.phase(rid, str(body.get("state", "")))
@app.post("/queue/release/{rid}")
def release(rid: str, body: dict | None = None):
    body = body or {}
    return pool.release(rid, bool(body.get("failed")), body.get("error"))
@app.get("/queue/snapshot")
def snapshot(): return pool.snapshot()
