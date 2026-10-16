"""Voice input under a time budget and a number of slots, through any OpenAI-compatible recognizer.

The default -- no ``transcription`` section -- is the path voice always took
(Groq, no limits); everything else here is a setting someone turned on.
"""

from __future__ import annotations

import asyncio
import json
import threading
import wave
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

import raven.providers.transcription as stt
from raven.channels.transcribe import transcribe_audio, untranscribed_note
from raven.config.loader import set_config_path
from raven.config.schema import TranscriptionConfig


def _wav(path: Path, seconds: float) -> Path:
    with wave.open(str(path), "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(8000)
        clip.writeframes(b"\0\0" * int(8000 * seconds))
    return path


@pytest.fixture
def config(tmp_path: Path):
    path = tmp_path / "config.json"

    def write(data: dict) -> Path:
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    write({})
    set_config_path(path)
    yield write
    set_config_path(None)  # type: ignore[arg-type]


class _Recognizer:
    """Stands in for Groq: counts calls, how many ran at once, and takes as long as asked."""

    calls = 0
    running = 0
    peak = 0
    delay = 0.0
    keys: list = []

    def __init__(self, api_key=None, **_kw):
        type(self).keys.append(api_key)

    async def transcribe(self, path):
        cls = type(self)
        cls.calls += 1
        cls.running += 1
        cls.peak = max(cls.peak, cls.running)
        try:
            await asyncio.sleep(cls.delay)
        finally:
            cls.running -= 1
        return f"heard {Path(path).name}"


@pytest.fixture
def recognizer(monkeypatch):
    class R(_Recognizer):
        calls = running = peak = 0
        delay = 0.0
        keys = []

    monkeypatch.setattr(stt, "GroqTranscriptionProvider", R)
    return R


def test_with_no_section_voice_goes_to_groq_with_no_limits(config, recognizer, tmp_path: Path) -> None:
    clip = _wav(tmp_path / "a.wav", 900)

    text = asyncio.run(transcribe_audio(clip, api_key="k"))

    assert stt.settings() == TranscriptionConfig()
    assert TranscriptionConfig().provider == "groq" and not TranscriptionConfig().max_seconds
    assert text == "heard a.wav" and recognizer.keys == ["k"], "a 15-minute clip is sent as before"


def test_a_clip_over_the_time_budget_is_not_sent_and_the_agent_is_told(config, recognizer, tmp_path: Path) -> None:
    config({"transcription": {"maxSeconds": 2}})
    long_clip, short_clip = _wav(tmp_path / "long.wav", 3), _wav(tmp_path / "short.wav", 1)
    ogg = tmp_path / "voice.ogg"
    ogg.write_bytes(b"OggS")

    long_text = asyncio.run(transcribe_audio(long_clip))
    short_text = asyncio.run(transcribe_audio(short_clip))
    stated = asyncio.run(transcribe_audio(ogg, duration_s=1.0))

    assert long_text == "" and recognizer.calls == 2, "the long clip never reached the recognizer"
    assert untranscribed_note(long_clip) == " (not transcribed: 3s is over the 2s limit)"
    assert short_text == "heard short.wav" and stated == "heard voice.ogg"


def test_slots_bound_how_many_run_at_once_and_a_clip_waits_its_turn(config, recognizer, tmp_path: Path) -> None:
    config({"transcription": {"slots": 1, "queueWaitS": 5}})
    recognizer.delay = 0.2
    clips = [_wav(tmp_path / f"{i}.wav", 1) for i in range(3)]

    async def all_three():
        return await asyncio.gather(*(stt.transcribe(c) for c in clips))

    results = asyncio.run(all_three())

    assert [r.status for r in results] == ["ok", "ok", "ok"] and recognizer.peak == 1


def test_a_clip_that_waits_too_long_for_a_slot_is_passed_on_untranscribed(config, recognizer, tmp_path: Path) -> None:
    config({"transcription": {"slots": 1, "queueWaitS": 0.1}})
    recognizer.delay = 0.6
    first, second = _wav(tmp_path / "1.wav", 1), _wav(tmp_path / "2.wav", 1)

    async def both():
        return await asyncio.gather(stt.transcribe(first), stt.transcribe(second))

    results = asyncio.run(both())

    assert sorted(r.status for r in results) == ["busy", "ok"]
    assert "slot(s) stayed busy for 0.1s" in next(r.reason for r in results if r.status == "busy")


def test_a_transcription_past_its_timeout_is_given_up(config, recognizer, tmp_path: Path) -> None:
    config({"transcription": {"timeoutS": 0.1}})
    recognizer.delay = 1.0

    result = asyncio.run(stt.transcribe(_wav(tmp_path / "a.wav", 1)))

    assert result.status == "timeout" and result.reason == "took longer than 0.1s"


class _Whisper(BaseHTTPRequestHandler):
    seen: list[dict] = []
    status = 200

    def log_message(self, *_a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        payload = json.dumps({"text": "привет, ворон"} if type(self).status == 200 else {"error": "bad key"}).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def whisper():
    class W(_Whisper):
        seen = []
        status = 200

    server = ThreadingHTTPServer(("127.0.0.1", 0), W)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield W, f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


def test_any_openai_compatible_provider_can_transcribe_with_its_own_key(config, whisper, tmp_path: Path) -> None:
    handler, base = whisper
    config(
        {
            "providers": {"localWhisper": {"apiBase": base, "apiKey": "local-key"}},
            "transcription": {"provider": "localWhisper", "model": "whisper-small", "language": "ru"},
        }
    )
    clip = _wav(tmp_path / "a.wav", 1)

    ok = asyncio.run(stt.transcribe(clip))
    handler.status = 401
    refused = asyncio.run(stt.transcribe(clip))

    assert ok.ok and ok.text == "привет, ворон"
    first = handler.seen[0]
    assert first["path"] == "/v1/audio/transcriptions" and first["auth"] == "Bearer local-key"
    assert b'name="model"\r\n\r\nwhisper-small' in first["body"] and b'name="language"\r\n\r\nru' in first["body"]
    assert refused.status == "failed" and "answered 401" in refused.reason


def test_without_a_groq_key_the_reason_reaches_the_note(config, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    clip = _wav(tmp_path / "a.wav", 1)

    text = asyncio.run(transcribe_audio(clip))

    assert text == ""
    assert untranscribed_note(clip) == " (not transcribed: no Groq key: set providers.groq.apiKey or GROQ_API_KEY)"


def test_a_channel_tells_the_agent_why_a_voice_message_has_no_text(config, monkeypatch, tmp_path: Path) -> None:
    from raven.channels.adapters.weixin import protocol as p
    from tests.test_channels_weixin import _channel

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    clip = _wav(tmp_path / "v.wav", 1)
    ch = _channel()
    ch._download_media = AsyncMock(return_value=str(clip))
    parts, media = [], []

    asyncio.run(ch._render_media_item({}, p.ITEM_VOICE, parts, media))

    assert parts == [
        f"[voice]\n[Audio: source: {clip}] (not transcribed: no Groq key: set providers.groq.apiKey or GROQ_API_KEY)"
    ]


def test_telegram_reports_a_clips_length_in_either_form() -> None:
    from types import SimpleNamespace

    from raven.channels.adapters.telegram.channel import _seconds

    assert _seconds(SimpleNamespace(duration=42)) == 42.0
    assert _seconds(SimpleNamespace(duration=timedelta(seconds=7.5))) == 7.5
    assert _seconds(SimpleNamespace()) is None


def test_a_clips_length_comes_from_its_header_or_ffprobe(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(stt.shutil, "which", lambda _name: None)
    ogg = tmp_path / "a.ogg"
    ogg.write_bytes(b"OggS")

    assert stt.audio_duration(_wav(tmp_path / "a.wav", 2.5)) == pytest.approx(2.5)
    assert stt.audio_duration(ogg) is None, "without ffprobe an Ogg clip's length is unknown, so it is not refused"


def test_a_section_that_does_not_validate_falls_back_to_the_defaults(config) -> None:
    config({"transcription": {"slots": -1}})

    assert stt.settings() == TranscriptionConfig()


def test_the_command_line_shows_the_settings_and_transcribes_a_clip(
    config, recognizer, monkeypatch, tmp_path: Path
) -> None:
    from raven.cli.commands import app

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    config({"providers": {"groq": {"apiKey": "gsk-secret"}}, "transcription": {"maxSeconds": 60, "slots": 2}})
    runner = CliRunner()

    status = runner.invoke(app, ["voice", "status"])
    ok = runner.invoke(app, ["voice", "transcribe", str(_wav(tmp_path / "a.wav", 1)), "--json"])
    long = runner.invoke(app, ["voice", "transcribe", str(_wav(tmp_path / "b.wav", 61))])

    assert "recognizer: groq" in status.output and "key: set" in status.output and "gsk-secret" not in status.output
    assert "time budget per clip: 60s" in status.output and "slots: 2 at once" in status.output
    assert ok.exit_code == 0 and json.loads(ok.output)["text"] == "heard a.wav"
    assert long.exit_code == 1 and "not transcribed (too_long): 61s is over the 60s limit" in long.output
