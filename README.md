---
title: Explainer Video Generator
emoji: 🎬
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# Explainer Video Generator

Type a topic and get a narrated, animated explainer video (Manim + TTS), like a 3Blue1Brown/StatQuest style lesson.

Pipeline: LLM writes the script (scenes with narration and storyboard), TTS narrates each scene, LLM writes Manim code for each scene timed to its narration, the code is rendered (render errors are sent back to the LLM to fix, up to 3 times, then a simple slide is used), and ffmpeg stitches everything into one MP4.

Both the LLM and the TTS use OpenAI-compatible APIs (`/v1/chat/completions`, `/v1/audio/speech`).

## Settings (Space secrets / variables)

| Name | Default | Meaning |
| --- | --- | --- |
| LLM_BASE_URL | https://api.openai.com/v1 | Any OpenAI-compatible chat API |
| LLM_MODEL | gpt-4.1 | Strong coding models give the best animations |
| LLM_API_KEY | (none) | Used when the page leaves the key blank |
| TTS_BASE_URL | Kokoro on Modal | Any OpenAI-compatible speech API |
| TTS_MODEL | tts-1 | |
| TTS_VOICE | af_heart | Default voice |
| TTS_API_KEY | (none) | Used when the page leaves the key blank |
| APP_PASSWORD | (none) | If set, the page asks for it before generating |
| MAX_FIXES | 3 | LLM fix attempts per scene |

Users can also type their own URLs, models and keys in the page (saved only in their browser).

## API

- `POST /api/jobs` `{topic, minutes, language, voice, speed, extra, llm_*, tts_*}` -> job
- `GET /api/jobs/{id}` -> progress and log
- `GET /api/jobs/{id}/video` -> MP4, `GET /api/jobs/{id}/sources` -> script + Manim code zip

Free CPU Spaces have 2 vCPU: a 3 minute video takes roughly 5 to 10 minutes. Videos are lost when the Space restarts.

## Modal (live deployment)

Pushing to `main` deploys `modal_app.py` through `.github/workflows/deploy.yml` (repo secrets `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, `TTS_API_KEY`, optional `LLM_API_KEY`; optional repo variables `LLM_BASE_URL`, `LLM_MODEL`).

- `web` scales to zero (0.125 CPU / 256 MiB, 2 min after the last request), so idle costs nothing.
- Each video runs in its own `render` container: 0.125 CPU / 512 MiB request, bursting to 2 CPU / 4 GiB. One job at a time; others wait.
- Spend guard: both functions meter their cost; new jobs are refused, and a running job stops, once the month's estimate reaches $28.40 (`GET /api/budget`).
