"""Optional: run the generator on Modal (scale to zero). Deploy with: modal deploy modal_app.py"""
import modal

image = (modal.Image.debian_slim(python_version="3.11")
         .apt_install("ffmpeg", "build-essential", "pkg-config", "libcairo2-dev", "libpango1.0-dev", "fonts-dejavu-core", "fonts-noto-core")
         .pip_install_from_requirements("requirements.txt")
         .add_local_dir(".", "/app", ignore=["**/__pycache__", ".git"]))
app = modal.App("explainer-video-generator")
vol = modal.Volume.from_name("videogen-jobs", create_if_missing=True)


@app.function(image=image, cpu=2.0, memory=2048, max_containers=1, scaledown_window=900, timeout=3600,
              volumes={"/data": vol}, secrets=[modal.Secret.from_name("videogen", required_keys=[])])
@modal.concurrent(max_inputs=50)
@modal.asgi_app()
def web():
    import os, sys
    os.environ.setdefault("JOBS_DIR", "/data/jobs")
    sys.path.insert(0, "/app"); os.chdir("/app")
    from app import app as fastapi_app
    return fastapi_app
