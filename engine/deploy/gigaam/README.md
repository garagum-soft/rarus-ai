# Russian speech-to-text on the CPU: GigaAM

Sber's [GigaAM](https://github.com/salute-developers/GigaAM) v3 (MIT) behind an
OpenAI-compatible `/v1/audio/transcriptions`, so Raven sends voice messages to it
like to any Whisper server. Punctuation and normalisation included (`v3_e2e_rnnt`);
audio of any length is cut at pauses into pieces of at most 23 s.

```bash
uv venv --python 3.12 ~/gigaam/.venv
uv pip install --python ~/gigaam/.venv/bin/python torch torchaudio --index-url https://download.pytorch.org/whl/cpu
git clone https://github.com/salute-developers/GigaAM ~/gigaam/GigaAM
uv pip install --python ~/gigaam/.venv/bin/python -e ~/gigaam/GigaAM aiohttp "numpy<2.4"
~/gigaam/.venv/bin/python deploy/gigaam/server.py          # ffmpeg must be on PATH
```

Raven's config (`~/.raven/config.json`):

```json
"providers": {"gigaam": {"apiBase": "http://127.0.0.1:8805/v1"}},
"transcription": {"provider": "gigaam", "model": "gigaam-v3", "language": "ru"}
```

In accounts mode every account's engine is given this recognizer on start when it
has none of its own (it runs on this machine, behind nobody's key).

`numpy<2.4` and oneDNN off are for virtual CPUs without SSE4/AVX ("Common KVM
processor"); on a CPU with AVX2 neither is needed and recognition is faster.
Measured on 16 threads of such a CPU: 7 s of speech in 1.3 s, 70 s in 11 s.
