"""Russian speech-to-text on the CPU: Sber's GigaAM v3 behind an OpenAI-compatible endpoint.

POST /v1/audio/transcriptions  (multipart: file, model?, language?, response_format?)
  -> {"text": "..."}            (response_format=text -> plain text)
GET  /v1/models, /health

Raven sends voice here when `transcription.provider` names a provider whose
apiBase is http://127.0.0.1:8805/v1. Audio of any length: it is cut at the
quietest moment near every 20 s and each piece is recognised on its own
(GigaAM's transcribe() takes up to 25 s).

Env: GIGAAM_MODEL (v3_e2e_rnnt), GIGAAM_THREADS (16), GIGAAM_HOST (127.0.0.1), GIGAAM_PORT (8805).
"""

import asyncio
import os
import tempfile
import time

import torch
from aiohttp import web

import gigaam
from gigaam.preprocess import SAMPLE_RATE, load_audio

MODEL_NAME = os.environ.get("GIGAAM_MODEL", "v3_e2e_rnnt")
THREADS = int(os.environ.get("GIGAAM_THREADS", "16"))
TARGET_S, MIN_S, MAX_S = 20.0, 14.0, 23.0
FRAME = int(0.05 * SAMPLE_RATE)

# This machine's CPU has no SSE4/AVX: oneDNN cannot build its kernels there, the plain path can.
torch.backends.mkldnn.enabled = False
torch.set_num_threads(THREADS)
MODEL = gigaam.load_model(MODEL_NAME, device="cpu")
LOCK = asyncio.Lock()


def _cut_points(wav: torch.Tensor) -> list[tuple[int, int]]:
    """[start, end) sample ranges of at most MAX_S, each ending at the quietest frame past MIN_S."""
    total, pieces, start = wav.shape[-1], [], 0
    while total - start > int(MAX_S * SAMPLE_RATE):
        lo, hi = start + int(MIN_S * SAMPLE_RATE), start + int(MAX_S * SAMPLE_RATE)
        window = wav[lo:hi]
        frames = window[: (window.shape[-1] // FRAME) * FRAME].reshape(-1, FRAME)
        energy = frames.pow(2).mean(dim=1)
        cut = lo + int(torch.argmin(energy)) * FRAME + FRAME // 2
        pieces.append((start, cut))
        start = cut
    pieces.append((start, total))
    return pieces


@torch.inference_mode()
def _recognise(path: str) -> str:
    wav = load_audio(path)
    texts = []
    for start, end in _cut_points(wav):
        piece = wav[start:end]
        if piece.shape[-1] < SAMPLE_RATE // 10:
            continue
        batch = piece.to(MODEL._dtype).unsqueeze(0)
        length = torch.full([1], batch.shape[-1])
        encoded, encoded_len = MODEL.forward(batch, length)
        text, _words = MODEL._decode(encoded, encoded_len, length, False)[0]
        if text.strip():
            texts.append(text.strip())
    return " ".join(texts)


async def transcriptions(request: web.Request) -> web.StreamResponse:
    reader = await request.multipart()
    audio, fmt = None, "json"
    async for part in reader:
        if part.name == "file":
            audio = await part.read(decode=False)
        elif part.name == "response_format":
            fmt = (await part.text()).strip() or "json"
        else:
            await part.read()
    if not audio:
        return web.json_response({"error": {"message": "no file in the request"}}, status=400)
    with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as tmp:
        tmp.write(audio)
        path = tmp.name
    try:
        started = time.monotonic()
        async with LOCK:
            text = await asyncio.to_thread(_recognise, path)
        print(f"transcribed {len(audio)} bytes in {time.monotonic() - started:.1f}s: {text[:80]!r}", flush=True)
    except Exception as exc:  # noqa: BLE001 - the caller gets the reason, the server keeps serving
        return web.json_response({"error": {"message": f"could not transcribe: {exc}"}}, status=400)
    finally:
        os.unlink(path)
    if fmt == "text":
        return web.Response(text=text)
    return web.json_response({"text": text})


async def models(_request: web.Request) -> web.Response:
    return web.json_response({"object": "list", "data": [{"id": MODEL_NAME, "object": "model"}]})


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "model": MODEL_NAME})


app = web.Application(client_max_size=200 * 1024 * 1024)
app.router.add_post("/v1/audio/transcriptions", transcriptions)
app.router.add_get("/v1/models", models)
app.router.add_get("/health", health)

if __name__ == "__main__":
    web.run_app(app, host=os.environ.get("GIGAAM_HOST", "127.0.0.1"), port=int(os.environ.get("GIGAAM_PORT", "8805")), print=None)
