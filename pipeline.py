"""Topic -> narrated Manim explainer video.

LLM (OpenAI-compatible chat API) plans scenes and writes Manim code,
TTS (OpenAI-compatible /audio/speech) narrates, Manim renders, ffmpeg muxes.
"""
import ast
import concurrent.futures as cf
import io
import json
import os
import re
import shutil
import subprocess
import random
import sys
import threading
import time
import wave

import requests

import prompts

HERE = os.path.dirname(os.path.abspath(__file__))
WIDTH, HEIGHT, FPS = 854, 480, 30
RENDER_TIMEOUT = int(os.getenv("RENDER_TIMEOUT", "420"))
MAX_FIXES = int(os.getenv("MAX_FIXES", "3"))
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "240"))
LLM_PARALLEL = int(os.getenv("LLM_PARALLEL", "2"))  # many providers allow only 2 requests in flight
LLM_429_RETRIES = int(os.getenv("LLM_429_RETRIES", "8"))
_SEMS = {}
_SEMS_LOCK = threading.Lock()


def _llm_sem(cfg):
    """One limiter per (provider, parallel) so plan, code and fix calls never exceed the cap together."""
    n = max(1, min(8, int(cfg.get("llm_parallel") or LLM_PARALLEL)))
    key = (cfg.get("llm_base_url", ""), n)
    with _SEMS_LOCK:
        if key not in _SEMS:
            _SEMS[key] = threading.BoundedSemaphore(n)
        return _SEMS[key]

BANNED_NAMES = {"Tex", "MathTex", "DecimalNumber", "Integer", "Variable", "SingleStringMathTex", "ImageMobject", "SVGMobject", "open", "exec", "eval",
                "__import__", "compile", "input", "globals", "locals", "getattr", "setattr", "delattr"}
ALLOWED_IMPORTS = {"vg", "manim", "math", "random", "numpy", "itertools", "functools"}


class JobCancelled(Exception):
    pass


# ---------------------------------------------------------------- API clients
def llm_chat(cfg, messages, temperature=0.4, max_tokens=6000):
    url = cfg["llm_base_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.get("llm_api_key"):
        headers["Authorization"] = "Bearer " + cfg["llm_api_key"]
    body = {"model": cfg["llm_model"], "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    last = None
    errors = 0
    rate_limited = 0
    while True:
        wait = None
        try:
            with _llm_sem(cfg):
                r = requests.post(url, headers=headers, json=body, timeout=LLM_TIMEOUT)
            if r.status_code == 429:
                rate_limited += 1
                last = f"LLM HTTP 429: {r.text[:300]}"
                if rate_limited > LLM_429_RETRIES:
                    break
                try:
                    wait = float(r.headers.get("Retry-After", ""))
                except ValueError:
                    wait = None
                wait = min(60.0, wait if wait and wait > 0 else 4 * 2 ** min(rate_limited - 1, 4)) + random.uniform(0, 2)
            elif r.status_code >= 500:
                errors += 1
                last = f"LLM HTTP {r.status_code}: {r.text[:300]}"
                if errors >= 3:
                    break
                wait = 5 * errors
            elif r.status_code != 200:
                raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:500]}")
            else:
                msg = r.json()["choices"][0]["message"]
                return msg.get("content") or ""
        except requests.RequestException as e:
            errors += 1
            last = f"LLM request failed: {e}"
            if errors >= 3:
                break
            wait = 5 * errors
        time.sleep(wait)
    raise RuntimeError(last or "LLM failed")


def tts(cfg, text, path):
    url = cfg["tts_base_url"].rstrip("/") + "/audio/speech"
    headers = {"Content-Type": "application/json"}
    if cfg.get("tts_api_key"):
        headers["Authorization"] = "Bearer " + cfg["tts_api_key"]
    body = {"model": cfg.get("tts_model") or "tts-1", "input": text, "voice": cfg["voice"],
            "response_format": "wav", "speed": float(cfg.get("speed") or 1.0)}
    last = None
    for attempt in range(4):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=300)
            if r.status_code == 200 and r.content:
                raw = path + ".raw"
                open(raw, "wb").write(r.content)
                # normalise whatever came back (wav/mp3) to 24 kHz mono wav with 0.3 s lead and 0.5 s tail
                ff(["-i", raw, "-af", "adelay=300:all=1,apad=pad_dur=0.5", "-ar", "24000", "-ac", "1", path])
                os.remove(raw)
                return wav_duration(path)
            last = f"TTS HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code in (400, 401, 403, 404):
                break
        except requests.RequestException as e:
            last = f"TTS request failed: {e}"
        time.sleep(4 * (attempt + 1))
    raise RuntimeError(last or "TTS failed")


# ---------------------------------------------------------------- helpers
def ff(args, timeout=600):
    p = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + p.stderr[-800:])


def wav_duration(path):
    with wave.open(path) as w:
        return w.getnframes() / float(w.getframerate())


def media_duration(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def extract_json(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    s = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(s)


def extract_code(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    code = max(blocks, key=len) if blocks else text
    code = code.strip()
    if "from vg import" not in code:
        code = "from vg import *\n" + code
    return code


def normalize_code(code, cls):
    """Cheap fixes for common LLM slips before rendering: missing math/numpy imports, wrong class name."""
    head = []
    if re.search(r"\bmath\.", code) and not re.search(r"^\s*import math\b", code, re.M):
        head.append("import math")
    if re.search(r"\bnp\.", code) and not re.search(r"^\s*import numpy as np\b", code, re.M):
        head.append("import numpy as np")
    if re.search(r"\brandom\.", code) and not re.search(r"^\s*import random\b", code, re.M):
        head.append("import random")
    if head:
        code = re.sub(r"^(from vg import \*)", lambda m: m.group(1) + "\n" + "\n".join(head), code, count=1, flags=re.M)
    if not re.search(rf"^class {cls}\b", code, re.M):
        names = re.findall(r"^class (\w+)\((?:Timed|Scene)\)", code, re.M)
        if len(names) == 1:
            code = re.sub(rf"\b{names[0]}\b", cls, code)
        code = re.sub(rf"^class {cls}\(Scene\)", f"class {cls}(Timed)", code, flags=re.M)
    return code


def error_summary(err):
    """The most informative line of a render error (the exception line, not rich's wrapped box)."""
    lines = [re.sub(r"^[\s│|╭╰─]+|[\s│|╮╯─]+$", "", l) for l in (err or "").splitlines()]
    lines = [l for l in lines if l]
    for l in reversed(lines):
        if re.match(r"^([A-Za-z_][\w.]*(Error|Exception|Interrupt)|Scene runs|The file must|Import of|Render timed)\b", l):
            return l[:200]
    return (lines[-1] if lines else "unknown")[:200]


def check_code(code, cls):
    """Static safety + sanity check. Returns an error string or None."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"SyntaxError: {e}"
    has_cls = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for m in mods:
                if m.split(".")[0] not in ALLOWED_IMPORTS:
                    return f"Import of '{m}' is not allowed. Only use `from vg import *` (plus math, random, numpy)."
        if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            return f"'{node.id}' is not allowed (no LaTeX, no images, no file or code access). Use T(...) for text."
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return "Dunder attribute access is not allowed."
        if isinstance(node, ast.ClassDef) and node.name == cls:
            has_cls = True
    if not has_cls:
        return f"The file must define class {cls}(Timed)."
    return None


def fallback_code(cls, title, narration):
    """A simple scene that always renders: title + key sentences as bullets."""
    sents = [s.strip() for s in re.split(r"(?<=[.!?\u0964])\s+", narration) if len(s.strip()) > 3][:4]
    items = []
    for s in sents:
        s = s if len(s) <= 48 else s[:45].rsplit(" ", 1)[0] + "..."
        items.append(s)
    return f'''from vg import *

class {cls}(Timed):
    def construct(self):
        head = fit(T({title!r}, 40, weight=BOLD), 12.5).to_edge(UP)
        self.play(Write(head))
        items = {items!r}
        pts = bullets(items, 28).next_to(head, DOWN, buff=0.7)
        step = max(0.5, (SCENE_DURATION - 3) / max(1, len(items)) - 0.8)
        for p in pts:
            self.play(FadeIn(p, shift=RIGHT*0.3), run_time=0.8)
            self.wait(step)
        self.finish()
'''


# ---------------------------------------------------------------- the job
class Pipeline:
    def __init__(self, job, workdir, log, set_progress, cancelled):
        self.job = job
        self.cfg = job["cfg"]
        self.dir = workdir
        self.log = log
        self.progress = set_progress
        self.cancelled = cancelled
        os.makedirs(self.dir, exist_ok=True)
        shutil.copy(os.path.join(HERE, "vg.py"), os.path.join(self.dir, "vg.py"))

    def check(self):
        if self.cancelled():
            raise JobCancelled()

    # 1. plan
    def plan(self):
        minutes = float(self.cfg.get("minutes") or 3)
        n = max(2, min(16, round(minutes * 60 / 24)))
        msgs = [{"role": "system", "content": prompts.PLAN_SYSTEM},
                {"role": "user", "content": prompts.PLAN_USER.format(
                    topic=self.cfg["topic"], minutes=minutes, n_scenes=n,
                    language=self.cfg.get("language") or "English", extra=self.cfg.get("extra") or "none")}]
        for attempt in range(3):
            self.check()
            out = llm_chat(self.cfg, msgs, temperature=0.6)
            try:
                plan = extract_json(out)
                scenes = [s for s in plan["scenes"] if s.get("narration", "").strip()]
                if scenes:
                    for i, s in enumerate(scenes):
                        s["id"] = f"s{i + 1:02d}"
                        s["cls"] = f"S{i + 1:02d}"
                    plan["scenes"] = scenes
                    json.dump(plan, open(os.path.join(self.dir, "plan.json"), "w"), indent=1, ensure_ascii=False)
                    return plan
            except Exception as e:  # noqa: BLE001
                self.log(f"Plan parse failed ({e}), retrying")
                msgs += [{"role": "assistant", "content": out[:4000]},
                         {"role": "user", "content": "That was not valid JSON of the required shape. Return only the JSON object."}]
        raise RuntimeError("LLM did not return a usable plan")

    # 2. narration
    def narrate(self, scenes):
        def one(s):
            self.check()
            d = tts(self.cfg, s["narration"], os.path.join(self.dir, s["id"] + ".wav"))
            s["duration"] = round(d, 2)
            return s
        with cf.ThreadPoolExecutor(int(os.getenv("TTS_PARALLEL", "2"))) as ex:
            for i, s in enumerate(ex.map(one, scenes)):
                self.log(f"Narrated {s['id']}: {s['duration']:.1f} s")

    # 3. code
    def write_code(self, plan, s, idx):
        prev = ""
        if idx > 0:
            prev = "Previous scene storyboard (for continuity, do not repeat it): " + plan["scenes"][idx - 1].get("visual", "")[:600]
        msgs = [{"role": "system", "content": prompts.CODE_SYSTEM.format(cls=s["cls"], dur=s["duration"], target=max(3, s["duration"] - 1.5), example=prompts.EXAMPLE)},
                {"role": "user", "content": prompts.CODE_USER.format(title=plan.get("title", ""), idx=idx + 1, total=len(plan["scenes"]),
                                                                     narration=s["narration"], visual=s.get("visual", ""), prev=prev)}]
        out = llm_chat(self.cfg, msgs)
        s["messages"] = msgs + [{"role": "assistant", "content": out}]
        return extract_code(out)

    def render(self, s, code):
        path = os.path.join(self.dir, s["id"] + ".py")
        code = normalize_code(code, s["cls"])
        open(path, "w").write(code)
        err = check_code(code, s["cls"])
        if err:
            return None, err
        media = os.path.join(self.dir, "media_" + s["id"])
        env = dict(os.environ, VG_DURATION=str(s["duration"]), PYTHONPATH=self.dir)
        cmd = [sys.executable, "-m", "manim", "render", "-q", "l", "-r", f"{WIDTH},{HEIGHT}", "--fps", str(FPS),
               "--disable_caching", "--media_dir", media, "-o", s["id"], path, s["cls"]]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=RENDER_TIMEOUT, cwd=self.dir, env=env)
        except subprocess.TimeoutExpired:
            return None, f"Render timed out after {RENDER_TIMEOUT} s. Use fewer and simpler objects and animations."
        out = None
        for root, _, files in os.walk(media):
            for f in files:
                if f == s["id"] + ".mp4" and "partial_movie_files" not in root:
                    out = os.path.join(root, f)
        if p.returncode != 0 or not out:
            tail = (p.stderr or p.stdout or "")[-2500:]
            tail = re.sub(r"\x1b\[[0-9;]*m", "", tail)
            return None, tail or "render failed with no output"
        vd = media_duration(out)
        if vd > s["duration"] + 6:
            return None, f"Scene runs {vd:.1f} s but narration is only {s['duration']:.1f} s. Shorten waits/run_times so the total is under {s['duration']:.1f} s."
        return out, None

    def build_scene(self, plan, s, idx, code):
        for attempt in range(MAX_FIXES + 1):
            self.check()
            video, err = self.render(s, code)
            if video:
                s["status"] = "ok" if attempt == 0 else f"ok after {attempt} fix(es)"
                return video
            self.log(f"{s['id']}: render error (try {attempt + 1}): {error_summary(err)}")
            if attempt == MAX_FIXES:
                break
            msgs = s["messages"] + [{"role": "user", "content": prompts.FIX_USER.format(error=err, cls=s["cls"], dur=s["duration"])}]
            out = llm_chat(self.cfg, msgs, temperature=0.2)
            s["messages"] = msgs + [{"role": "assistant", "content": out}]
            code = extract_code(out)
        self.log(f"{s['id']}: using a simple fallback slide")
        s["status"] = "fallback"
        video, err = self.render(s, fallback_code(s["cls"], plan.get("title", ""), s["narration"]))
        if not video:
            raise RuntimeError(f"Fallback render failed for {s['id']}: {err[-300:]}")
        return video

    # 4. mux
    def mux(self, s, video):
        out = os.path.join(self.dir, s["id"] + "_av.mp4")
        vd = media_duration(video)
        hold = max(0.0, s["duration"] - vd)
        vf = f"tpad=stop_mode=clone:stop_duration={hold:.2f}" if hold > 0.05 else "null"
        ff(["-i", video, "-i", os.path.join(self.dir, s["id"] + ".wav"), "-vf", vf,
            "-af", "apad", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-shortest", out])
        return out

    def run(self):
        t0 = time.time()
        self.progress("planning", 2)
        self.log("Writing the script...")
        plan = self.plan()
        scenes = plan["scenes"]
        self.job["title"] = plan.get("title") or self.cfg["topic"]
        self.job["scenes"] = [{"id": s["id"], "narration": s["narration"], "status": "pending"} for s in scenes]
        self.log(f"Script ready: {len(scenes)} scenes, \"{self.job['title']}\"")

        self.progress("narrating", 10)
        self.narrate(scenes)
        total_audio = sum(s["duration"] for s in scenes)
        self.log(f"Narration ready: {total_audio / 60:.1f} min")

        self.progress("coding", 20)
        self.log("Writing animation code for every scene...")
        codes = {}
        failed = []
        n_par = max(1, min(8, int(self.cfg.get("llm_parallel") or LLM_PARALLEL)))
        with cf.ThreadPoolExecutor(n_par) as ex:
            futs = {ex.submit(self.write_code, plan, s, i): (s, i) for i, s in enumerate(scenes)}
            for f in cf.as_completed(futs):
                s, i = futs[f]
                try:
                    codes[s["id"]] = f.result()
                except Exception as e:  # noqa: BLE001
                    self.log(f"{s['id']}: code generation failed ({str(e)[:160]}); retrying after the others")
                    failed.append((s, i))
        for s, i in sorted(failed, key=lambda x: x[1]):
            self.check()
            try:
                codes[s["id"]] = self.write_code(plan, s, i)
                self.log(f"{s['id']}: code generation ok on retry")
            except Exception as e:  # noqa: BLE001
                self.log(f"{s['id']}: code generation failed again ({str(e)[:160]}); will use fallback")
                s["messages"] = []
                codes[s["id"]] = fallback_code(s["cls"], plan.get("title", ""), s["narration"])
        parts = []
        for i, s in enumerate(scenes):
            self.check()
            self.progress(f"rendering scene {i + 1}/{len(scenes)}", 25 + int(65 * i / len(scenes)))
            video = self.build_scene(plan, s, i, codes[s["id"]])
            parts.append(self.mux(s, video))
            self.job["scenes"][i]["status"] = s["status"]
            self.log(f"{s['id']}: {s['status']}")

        self.progress("stitching", 93)
        lst = os.path.join(self.dir, "list.txt")
        open(lst, "w").write("".join(f"file '{p}'\n" for p in parts))
        final = os.path.join(self.dir, "video.mp4")
        ff(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart", final])
        self.job["duration"] = round(media_duration(final), 1)
        self.job["elapsed"] = round(time.time() - t0, 1)
        self.log(f"Done: {self.job['duration'] / 60:.1f} min video in {self.job['elapsed'] / 60:.1f} min")
        # keep the scene sources for download; drop heavy intermediates
        for d in os.listdir(self.dir):
            if d.startswith("media_"):
                shutil.rmtree(os.path.join(self.dir, d), ignore_errors=True)
        return final
