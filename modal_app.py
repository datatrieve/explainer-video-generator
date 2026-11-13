"""Explainer video generator on Modal, scale-to-zero with a monthly spend guard.

- `web`: light FastAPI page + API. 0.125 CPU / 256 MiB request, scales to zero 2 min after the last request.
- `render`: one container per video job, spawned by the web API. Requests 0.125 CPU / 512 MiB and
  bursts up to 2 CPU / 4 GiB while it renders; it exits when the job ends, so idle cost is $0.
- Job state lives in a modal.Dict, videos in a Volume.
- Spend guard: both functions meter their own cost (Modal bills max(request, use) each second) into the
  Dict. New jobs are refused once this month's estimate (UTC calendar month, +10% margin) reaches
  MONTHLY_BUDGET, and a running job stops there. Modal Starter's own $30 credit is a hard limit too.

Both the LLM and the TTS are OpenAI-compatible APIs (/v1/chat/completions, /v1/audio/speech): defaults
come from the Modal secret `videogen` (LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, TTS_BASE_URL, TTS_MODEL,
TTS_VOICE, TTS_API_KEY) and the page can override every one of them.
"""
import hashlib
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import modal

APP_NAME = "explainer-video-generator"
MONTHLY_BUDGET = float(os.getenv("MONTHLY_BUDGET", "28.40"))
CPU_RATE = 0.0000131  # $ per physical core-second
MEM_RATE = 0.00000222  # $ per GiB-second
WEB_CPU, WEB_MEM = 0.125, 256
R_CPU, R_CPU_MAX, R_MEM, R_MEM_MAX = 0.125, 2.0, 512, 4096
KEEP_JOBS = 15
# sha256 of the app password (the page asks for it before generating); empty = no password
APP_PASSWORD_SHA256 = ""

render_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "build-essential", "pkg-config", "libcairo2-dev", "libpango1.0-dev",
                 "fonts-dejavu-core", "fonts-noto-core")
    .pip_install_from_requirements("requirements.txt")
    .add_local_dir(".", "/app", ignore=["**/__pycache__", ".git", ".github", "test"])
)
web_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("fastapi[standard]>=0.110", "requests>=2.31", "pydantic>=2.6")
    .add_local_dir("static", "/app/static")
)

app = modal.App(APP_NAME)
vol = modal.Volume.from_name("videogen-jobs", create_if_missing=True)
state = modal.Dict.from_name("videogen-state", create_if_missing=True)
secret = modal.Secret.from_name("videogen", required_keys=[])
JOBS = "/data/jobs"


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _spent() -> float:
    return float(state.get("spend:" + _month(), 0.0))


def _add_spend(cost: float):
    k = "spend:" + _month()
    state[k] = float(state.get(k, 0.0)) + cost  # single writer at a time is close enough for a guard


def _meter(cpu_req, mem_req_mb, stop: threading.Event, every=15, on_budget=None):
    """Adds 1.1 x max(request, use) cost every `every` s to the month's spend."""
    import resource

    def cpu_s():
        a, b = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
        return a.ru_utime + a.ru_stime + b.ru_utime + b.ru_stime

    def mem_gib():
        m = {}
        try:
            for line in open("/proc/meminfo"):
                k, v = line.split(":", 1)
                m[k] = int(v.split()[0]) * 1024
            return (m["MemTotal"] - m.get("MemAvailable", m.get("MemFree", 0))) / 2**30
        except Exception:
            return 0.0

    last_t, last_c = time.time(), cpu_s()
    while True:
        stopped = stop.wait(every)
        now, c = time.time(), cpu_s()
        dt = now - last_t
        cores = max(0.0, (c - last_c) / dt) if dt > 0 else 0.0
        cost = 1.1 * (max(cpu_req, cores) * CPU_RATE + max(mem_req_mb / 1024, mem_gib()) * MEM_RATE) * dt
        try:
            _add_spend(cost)
        except Exception:
            pass
        last_t, last_c = now, c
        if on_budget and _spent() >= MONTHLY_BUDGET:
            on_budget()
        if stopped:
            return


def _public(job):
    keys = ["id", "topic", "title", "state", "stage", "progress", "error", "created", "duration", "elapsed", "scenes"]
    out = {k: job.get(k) for k in keys}
    out["log"] = list(job.get("log", []))[-200:]
    out["queue_position"] = None
    return out


@app.function(image=render_image, volumes={"/data": vol}, secrets=[secret], cpu=(R_CPU, R_CPU_MAX),
              memory=(R_MEM, R_MEM_MAX), timeout=3600, max_containers=1, retries=0)
def render(jid: str, cfg: dict):
    import sys
    import traceback

    sys.path.insert(0, "/app")
    os.chdir("/app")
    from pipeline import JobCancelled, Pipeline

    job = dict(state.get("job:" + jid) or {})
    job.update(state="running", stage="starting", progress=1, cfg=cfg, log=job.get("log") or [], scenes=[])
    flags = {"cancel": False, "over": False}
    stop = threading.Event()

    def over_budget():
        flags["over"] = True

    def log(msg):
        job["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def prog(stage, pct):
        job["stage"], job["progress"] = stage, pct

    def cancelled():
        return flags["cancel"] or flags["over"]

    def sync():
        while not stop.is_set():
            try:
                state["job:" + jid] = _public(job)
                if state.get("cancel:" + jid):
                    flags["cancel"] = True
            except Exception:
                pass
            stop.wait(2)

    threading.Thread(target=_meter, args=(R_CPU, R_MEM, stop), kwargs={"on_budget": over_budget}, daemon=True).start()
    threading.Thread(target=sync, daemon=True).start()
    log("Worker started")
    try:
        Pipeline(job, os.path.join(JOBS, jid), log, prog, cancelled).run()
        job.update(state="done", stage="done", progress=100)
    except JobCancelled:
        if flags["over"]:
            job.update(state="failed", stage="failed", error="Monthly budget reached; job stopped")
        else:
            job.update(state="cancelled", stage="cancelled")
        log("Stopped")
    except Exception as e:  # noqa: BLE001
        job.update(state="failed", stage="failed", error=str(e)[:1000])
        log("Failed: " + str(e)[:500])
        traceback.print_exc()
    finally:
        vol.commit()
        stop.set()
        time.sleep(0.2)
        state["job:" + jid] = _public(job)


@app.function(image=web_image, volumes={"/data": vol}, secrets=[secret], cpu=WEB_CPU, memory=WEB_MEM,
              scaledown_window=120, max_containers=1, timeout=600)
@modal.concurrent(max_inputs=50)
@modal.asgi_app()
def web():
    import io
    import zipfile

    import requests
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse, Response
    from pydantic import BaseModel, Field

    stop = threading.Event()
    threading.Thread(target=_meter, args=(WEB_CPU, WEB_MEM, stop), kwargs={"every": 30}, daemon=True).start()

    defaults = {
        "llm_base_url": os.getenv("LLM_BASE_URL") or "https://api.openai.com/v1",
        "llm_model": os.getenv("LLM_MODEL") or "gpt-4.1",
        "tts_base_url": os.getenv("TTS_BASE_URL") or "https://tts.h-s.site/v1",
        "tts_model": os.getenv("TTS_MODEL") or "tts-1",
        "voice": os.getenv("TTS_VOICE") or "af_heart",
    }
    server_keys = {"llm_api_key": os.getenv("LLM_API_KEY", ""), "tts_api_key": os.getenv("TTS_API_KEY", "")}
    api = FastAPI(title="Explainer Video Generator")

    class JobIn(BaseModel):
        topic: str = Field(min_length=3, max_length=4000)
        minutes: float = Field(default=3, ge=0.5, le=12)
        language: str = "English"
        extra: str = Field(default="", max_length=2000)
        voice: str = ""
        speed: float = Field(default=1.0, ge=0.5, le=2.0)
        llm_base_url: str = ""
        llm_model: str = ""
        llm_api_key: str = ""
        tts_base_url: str = ""
        tts_model: str = ""
        tts_api_key: str = ""
    llm_parallel: int = Field(default=2, ge=1, le=8)

    def check_pw(pw):
        if APP_PASSWORD_SHA256 and hashlib.sha256((pw or "").encode()).hexdigest() != APP_PASSWORD_SHA256:
            raise HTTPException(401, "Wrong or missing app password")

    def get(jid):
        j = state.get("job:" + jid)
        if not j:
            raise HTTPException(404, "No such job")
        return j

    @api.get("/", response_class=HTMLResponse)
    def index():
        return open("/app/static/index.html", encoding="utf-8").read()

    @api.get("/api/config")
    def config():
        return {**defaults, "password_required": bool(APP_PASSWORD_SHA256),
                "server_llm_key": bool(server_keys["llm_api_key"]), "server_tts_key": bool(server_keys["tts_api_key"])}

    @api.get("/api/budget")
    def budget():
        return {"month": _month(), "spent_estimate_usd": round(_spent(), 3), "budget_usd": MONTHLY_BUDGET}

    @api.get("/api/voices")
    def voices(base_url: str = "", x_tts_key: str | None = Header(default=None)):
        base = (base_url or defaults["tts_base_url"]).rstrip("/")
        key = x_tts_key or (server_keys["tts_api_key"] if base == defaults["tts_base_url"].rstrip("/") else "")
        try:
            r = requests.get(base + "/audio/voices", headers={"Authorization": "Bearer " + key} if key else {}, timeout=20)
            if r.ok:
                return r.json()
        except requests.RequestException:
            pass
        return {"voices": []}

    @api.post("/api/jobs")
    def create(job_in: JobIn, x_app_password: str | None = Header(default=None)):
        check_pw(x_app_password)
        if _spent() >= MONTHLY_BUDGET:
            raise HTTPException(429, f"Monthly budget of ${MONTHLY_BUDGET:.2f} reached; try next month")
        cfg = job_in.model_dump()
        for k, v in defaults.items():
            cfg[k] = (cfg.get(k) or "").strip() or v
        for k, v in server_keys.items():
            if not (cfg.get(k) or "").strip():
                base_k = k.replace("_api_key", "_base_url")
                # only send the server's key to the server's own endpoint
                cfg[k] = v if cfg[base_k].rstrip("/") == defaults[base_k].rstrip("/") else ""
        if not cfg["llm_api_key"] and "api.openai.com" in cfg["llm_base_url"]:
            raise HTTPException(400, "Add an LLM API key")
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "topic": cfg["topic"], "title": None, "state": "queued", "stage": "queued", "progress": 0,
               "error": None, "created": time.time(), "log": [], "scenes": []}
        state["job:" + jid] = job
        order = list(state.get("order", [])) + [jid]
        while len(order) > KEEP_JOBS:
            old = order[0]
            if (state.get("job:" + old) or {}).get("state") in ("queued", "running"):
                break
            order.pop(0)
            state.pop("job:" + old, None)
            state.pop("cancel:" + old, None)
            try:
                vol.remove_file(f"jobs/{old}", recursive=True)
            except Exception:
                pass
        state["order"] = order
        render.spawn(jid, cfg)
        return _public(job)

    @api.get("/api/jobs")
    def list_jobs():
        out = []
        for j in reversed(list(state.get("order", []))):
            v = state.get("job:" + j)
            if v:
                out.append(v)
        return out

    @api.get("/api/jobs/{jid}")
    def get_job(jid: str):
        return get(jid)

    @api.post("/api/jobs/{jid}/cancel")
    def cancel(jid: str, x_app_password: str | None = Header(default=None)):
        check_pw(x_app_password)
        j = get(jid)
        state["cancel:" + jid] = True
        if j.get("state") == "queued":
            j["state"] = j["stage"] = "cancelled"
            state["job:" + jid] = j
        return j

    def job_dir(jid):
        get(jid)
        vol.reload()
        return os.path.join(JOBS, jid)

    @api.get("/api/jobs/{jid}/video")
    def video(jid: str, download: int = 0):
        path = os.path.join(job_dir(jid), "video.mp4")
        if not os.path.exists(path):
            raise HTTPException(404, "Video not ready")
        title = get(jid).get("title") or "video"
        name = "".join(c if c.isalnum() else "_" for c in title)[:60] + ".mp4"
        return FileResponse(path, media_type="video/mp4", filename=name if download else None)

    @api.get("/api/jobs/{jid}/sources")
    def sources(jid: str):
        d = job_dir(jid)
        if not os.path.isdir(d):
            raise HTTPException(404, "No files")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(os.listdir(d)):
                if f.endswith((".py", ".json")):
                    z.write(os.path.join(d, f), f)
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{jid}_sources.zip"'})

    @api.get("/health")
    def health():
        return {"ok": True, "spent_estimate_usd": round(_spent(), 3), "budget_usd": MONTHLY_BUDGET}

    return api
