"""Voice transcription: a clip in, text out, within a time budget and a number of slots.

The recognizers are OpenAI-compatible ``/audio/transcriptions`` endpoints --
Groq's Whisper (the default, as it always was), OpenAI's, or a local Whisper
server a configured provider points at. :func:`transcribe` is the one door the
channels use: it applies the ``transcription`` section of the config (read
live) -- a clip longer than ``maxSeconds`` is not sent, at most ``slots`` run at
once and the rest wait ``queueWaitS`` for one, ``timeoutS`` bounds a single
call -- and says why when there is no text, so the agent can be told rather
than handed a bare file path.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import wave
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

GROQ_BASE = "https://api.groq.com/openai/v1"
OPENAI_BASE = "https://api.openai.com/v1"
_ENV_KEYS = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY"}


def transcription_api_key(config) -> str:
    """The key channels transcribe voice with: the configured groq provider's."""
    groq = config.providers.get("groq")
    return getattr(groq, "api_key", "") or ""


class TranscriptionError(RuntimeError):
    """A clip the recognizer could not turn into text, with the reason."""


class OpenAICompatibleTranscription:
    """One ``/audio/transcriptions`` endpoint."""

    def __init__(self, api_key: str | None, api_base: str, model: str, language: str = "") -> None:
        self.api_key = api_key
        self.api_url = f"{api_base.rstrip('/')}/audio/transcriptions"
        self.model = model
        self.language = language

    async def transcribe_or_raise(self, file_path: str | Path) -> str:
        """The text, or a :class:`TranscriptionError` saying why there is none."""
        path = Path(file_path)
        if not path.exists():
            raise TranscriptionError(f"the audio file is gone: {file_path}")
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        fields: dict[str, Any] = {"model": (None, self.model)}
        if self.language:
            fields["language"] = (None, self.language)
        try:
            async with httpx.AsyncClient() as client:
                with open(path, "rb") as f:
                    response = await client.post(
                        self.api_url, headers=headers, files={"file": (path.name, f), **fields}, timeout=60.0
                    )
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"{self.api_url} could not be reached: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise TranscriptionError(f"the recognizer answered {response.status_code}: {response.text[:200]}")
        try:
            return str(response.json().get("text", "") or "")
        except ValueError as exc:
            raise TranscriptionError("the recognizer's answer was not JSON") from exc

    async def transcribe(self, file_path: str | Path) -> str:
        """The text, or "" when there is none (the reason is logged)."""
        try:
            return await self.transcribe_or_raise(file_path)
        except TranscriptionError as exc:
            logger.error("transcription error: {}", exc)
            return ""


class GroqTranscriptionProvider(OpenAICompatibleTranscription):
    """Groq's Whisper: extremely fast, with a generous free tier. The default recognizer."""

    def __init__(self, api_key: str | None = None, *, model: str = "whisper-large-v3", language: str = ""):
        super().__init__(api_key or os.environ.get("GROQ_API_KEY"), GROQ_BASE, model, language)

    async def transcribe_or_raise(self, file_path: str | Path) -> str:
        if not self.api_key:
            raise TranscriptionError("no Groq key: set providers.groq.apiKey or GROQ_API_KEY")
        return await super().transcribe_or_raise(file_path)


# -- the budgeted door -----------------------------------------------------------------------


@dataclass
class Transcript:
    text: str
    status: str
    """``ok``; ``too_long``, ``busy``, ``timeout`` or ``failed`` when there is no text."""
    reason: str = ""
    seconds: float = 0.0
    duration_s: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def settings() -> Any:
    """The ``transcription`` section as the config file has it now; the defaults when it is absent or invalid."""
    from raven.config.live import default_live
    from raven.config.schema import TranscriptionConfig

    raw = default_live().get("transcription")
    if isinstance(raw, dict):
        try:
            return TranscriptionConfig.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 -- a half-written section must not cost a voice message
            logger.warning("transcription settings are invalid, using the defaults: {}", exc)
    return TranscriptionConfig()


def audio_duration(path: str | Path) -> float | None:
    """The clip's length in seconds: read from a WAV header, else asked of ffprobe; None when neither can tell."""
    path = Path(path)
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as clip:
                return clip.getnframes() / float(clip.getframerate() or 1)
        except (OSError, wave.Error, EOFError):
            pass
    probe = shutil.which("ffprobe")
    if probe is None:
        return None
    try:
        done = subprocess.run(  # noqa: S603 -- a resolved binary, the file path as one argument
            [probe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return float(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


_SLOTS: dict[tuple[int, int], asyncio.Semaphore] = {}


def _slot(size: int) -> asyncio.Semaphore:
    # Per event loop: a semaphore binds to the loop it is first awaited on.
    key = (id(asyncio.get_running_loop()), size)
    if key not in _SLOTS:
        _SLOTS[key] = asyncio.Semaphore(size)
    return _SLOTS[key]


def _recognizer(cfg: Any, channel_key: str) -> Any:
    """The recognizer the settings name; the module global is read per call, so a test can stand one in."""
    name = (cfg.provider or "groq").strip()
    if name == "groq":
        if cfg.model or cfg.language:
            return GroqTranscriptionProvider(
                channel_key or None, model=cfg.model or "whisper-large-v3", language=cfg.language
            )
        return GroqTranscriptionProvider(api_key=channel_key or None)
    from raven.config.live import default_live

    row = default_live().get(f"providers.{name}") or {}
    if not isinstance(row, dict):
        row = {}
    key = row.get("apiKey") or row.get("api_key") or os.environ.get(_ENV_KEYS.get(name, ""), "")
    base = row.get("apiBase") or row.get("api_base") or (OPENAI_BASE if name == "openai" else "")
    if not base:
        raise TranscriptionError(f"provider {name!r} has no apiBase to send audio to")
    return OpenAICompatibleTranscription(key or None, base, cfg.model or "whisper-1", cfg.language)


async def transcribe(
    file_path: str | Path,
    *,
    api_key: str = "",
    channel: str = "",
    duration_s: float | None = None,
    config: Any = None,
) -> Transcript:
    """Transcribe one clip under the configured budget; the result says why when there is no text."""
    cfg = config if config is not None else settings()
    began = time.monotonic()
    where = channel or "channel"
    if cfg.max_seconds:
        length = duration_s if duration_s is not None else await asyncio.to_thread(audio_duration, file_path)
        if length is not None and length > cfg.max_seconds:
            reason = f"{length:.0f}s is over the {cfg.max_seconds}s limit"
            logger.info("{}: voice not transcribed: {}", where, reason)
            return Transcript("", "too_long", reason, duration_s=length)
        duration_s = length
    gate = _slot(cfg.slots) if cfg.slots else None
    if gate is not None:
        try:
            await asyncio.wait_for(gate.acquire(), cfg.queue_wait_s or None)
        except TimeoutError:
            reason = f"all {cfg.slots} transcription slot(s) stayed busy for {cfg.queue_wait_s:g}s"
            logger.warning("{}: voice not transcribed: {}", where, reason)
            return Transcript("", "busy", reason, duration_s=duration_s)
    try:
        recognizer = _recognizer(cfg, api_key)
        call = getattr(recognizer, "transcribe_or_raise", None) or recognizer.transcribe
        text = await (asyncio.wait_for(call(file_path), cfg.timeout_s) if cfg.timeout_s else call(file_path))
    except TimeoutError:
        reason = f"took longer than {cfg.timeout_s:g}s"
        logger.warning("{}: voice not transcribed: {}", where, reason)
        return Transcript("", "timeout", reason, time.monotonic() - began, duration_s)
    except Exception as exc:  # noqa: BLE001 -- a failed transcription is a message without text, not a crash
        logger.warning("{}: audio transcription failed: {}", where, exc)
        return Transcript("", "failed", str(exc) or type(exc).__name__, time.monotonic() - began, duration_s)
    finally:
        if gate is not None:
            gate.release()
    text = (text or "").strip()
    if not text:
        return Transcript("", "failed", "the recognizer returned no text", time.monotonic() - began, duration_s)
    return Transcript(text, "ok", "", time.monotonic() - began, duration_s)


# Why the last clips came back without text, by file, for the channel that renders them.
_REASONS: OrderedDict[str, str] = OrderedDict()
_REASONS_KEPT = 64


def remember_failure(file_path: str | Path, reason: str) -> None:
    _REASONS[str(file_path)] = reason
    _REASONS.move_to_end(str(file_path))
    while len(_REASONS) > _REASONS_KEPT:
        _REASONS.popitem(last=False)


def failure_reason(file_path: str | Path) -> str:
    """Why ``file_path`` came back without text, or "" when nothing is known."""
    return _REASONS.get(str(file_path), "")


__all__ = [
    "GroqTranscriptionProvider",
    "OpenAICompatibleTranscription",
    "Transcript",
    "TranscriptionError",
    "audio_duration",
    "failure_reason",
    "remember_failure",
    "settings",
    "transcribe",
    "transcription_api_key",
]
