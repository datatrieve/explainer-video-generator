"""Explainer video generator: web UI + job queue."""
import io
import os
import queue
import shutil
import threading
import time
import traceback
import uuid
import zipfile

import requests
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from pipeline import JobCancelled, Pipeline

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.getenv("JOBS_DIR", "/tmp/jobs")
KEEP_JOBS = int(os.getenv("KEEP_JOBS", "15"))
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

DEFAULTS = {
    "llm_base_url": os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
    "llm_model": os.getenv("LLM_MODEL", "gpt-4.1"),
    "tts_base_url": os.getenv("TTS_BASE_URL", "https://datatrieve--kokoro-tts-web.us-east.modal.run/v1"),
    "tts_model": os.getenv("TTS_MODEL", "tts-1"),
    "voice": os.getenv("TTS_VOICE", "af_heart"),
}
SERVER_KEYS = {"llm_api_key": os.getenv("LLM_API_KEY", ""), "tts_api_key": os.getenv("TTS_API_KEY", "")}

app = FastAPI(title="Explainer Video Generator")
jobs: dict[str, dict] = {}
order: list[str] = []
q: "queue.Queue[str]" = queue.Queue()
lock = threading.Lock()


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
    if APP_PASSWORD and pw != APP_PASSWORD:
        raise HTTPException(401, "Wrong or missing app password")


def public(job):
    keys = ["id", "topic", "title", "state", "stage", "progress", "error", "created", "duration", "elapsed", "scenes", "queue_position"]
    out = {k: job.get(k) for k in keys}
    out["log"] = job["log"][-200:]
    if job["state"] == "queued":
        try:
            out["queue_position"] = list(q.queue).index(job["id"]) + 1
        except ValueError:
            out["queue_position"] = None
    return out


def worker():
    while True:
        jid = q.get()
        job = jobs.get(jid)
        if not job or job["state"] == "cancelled":
            continue
        job["state"] = "running"

        def log(msg):
            job["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")

        def prog(stage, pct):
            job["stage"], job["progress"] = stage, pct

        try:
            Pipeline(job, os.path.join(JOBS_DIR, jid), log, prog, lambda: job["cancel"]).run()
            job["state"], job["stage"], job["progress"] = "done", "done", 100
        except JobCancelled:
            job["state"], job["stage"] = "cancelled", "cancelled"
            log("Cancelled")
        except Exception as e:  # noqa: BLE001
            job["state"], job["stage"], job["error"] = "failed", "failed", str(e)[:1000]
            log("Failed: " + str(e)[:500])
            traceback.print_exc()
        finally:
            job["cfg"] = {k: v for k, v in job["cfg"].items() if "key" not in k}  # forget secrets


@app.on_event("startup")
def startup():
    os.makedirs(JOBS_DIR, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index():
    return open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()


@app.get("/api/config")
def config():
    return {**DEFAULTS, "password_required": bool(APP_PASSWORD),
            "server_llm_key": bool(SERVER_KEYS["llm_api_key"]), "server_tts_key": bool(SERVER_KEYS["tts_api_key"])}


@app.get("/api/voices")
def voices(base_url: str = "", x_tts_key: str | None = Header(default=None)):
    base = (base_url or DEFAULTS["tts_base_url"]).rstrip("/")
    key = x_tts_key or SERVER_KEYS["tts_api_key"]
    try:
        r = requests.get(base + "/audio/voices", headers={"Authorization": "Bearer " + key} if key else {}, timeout=20)
        if r.ok:
            return r.json()
    except requests.RequestException:
        pass
    return {"voices": []}


@app.post("/api/jobs")
def create(job_in: JobIn, x_app_password: str | None = Header(default=None)):
    check_pw(x_app_password)
    cfg = job_in.model_dump()
    for k, v in DEFAULTS.items():
        cfg[k] = (cfg.get(k) or "").strip() or v
    for k, v in SERVER_KEYS.items():
        cfg[k] = (cfg.get(k) or "").strip() or v
    if not cfg["llm_api_key"] and "api.openai.com" in cfg["llm_base_url"]:
        raise HTTPException(400, "Add an LLM API key (or set LLM_API_KEY on the server)")
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "topic": cfg["topic"], "title": None, "state": "queued", "stage": "queued", "progress": 0,
           "error": None, "created": time.time(), "log": [], "scenes": [], "cfg": cfg, "cancel": False}
    with lock:
        jobs[jid] = job
        order.append(jid)
        while len(order) > KEEP_JOBS:
            old = order.pop(0)
            if jobs.get(old, {}).get("state") in ("done", "failed", "cancelled"):
                jobs.pop(old, None)
                shutil.rmtree(os.path.join(JOBS_DIR, old), ignore_errors=True)
            else:
                order.insert(0, old)
                break
    q.put(jid)
    return public(job)


@app.get("/api/jobs")
def list_jobs():
    return [public(jobs[j]) for j in reversed(order) if j in jobs]


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    if jid not in jobs:
        raise HTTPException(404, "No such job")
    return public(jobs[jid])


@app.post("/api/jobs/{jid}/cancel")
def cancel(jid: str, x_app_password: str | None = Header(default=None)):
    check_pw(x_app_password)
    if jid not in jobs:
        raise HTTPException(404, "No such job")
    jobs[jid]["cancel"] = True
    if jobs[jid]["state"] == "queued":
        jobs[jid]["state"] = "cancelled"
    return public(jobs[jid])


@app.get("/api/jobs/{jid}/video")
def video(jid: str, download: int = 0):
    path = os.path.join(JOBS_DIR, jid, "video.mp4")
    if jid not in jobs or not os.path.exists(path):
        raise HTTPException(404, "Video not ready")
    name = "".join(c if c.isalnum() else "_" for c in (jobs[jid].get("title") or "video"))[:60] + ".mp4"
    return FileResponse(path, media_type="video/mp4", filename=name if download else None)


@app.get("/api/jobs/{jid}/sources")
def sources(jid: str):
    d = os.path.join(JOBS_DIR, jid)
    if jid not in jobs or not os.path.isdir(d):
        raise HTTPException(404, "No such job")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(d)):
            if f.endswith((".py", ".json")):
                z.write(os.path.join(d, f), f)
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{jid}_sources.zip"'})


@app.get("/health")
def health():
    return {"ok": True, "queued": q.qsize(), "running": sum(1 for j in jobs.values() if j["state"] == "running")}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "7860")))
