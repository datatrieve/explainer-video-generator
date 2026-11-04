FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg build-essential pkg-config libcairo2-dev libpango1.0-dev \
        fonts-dejavu-core fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user . .
USER user
ENV HOME=/home/user PYTHONUNBUFFERED=1 JOBS_DIR=/tmp/jobs PORT=7860
EXPOSE 7860
CMD ["python", "app.py"]
