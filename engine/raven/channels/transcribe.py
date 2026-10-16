"""Audio transcription for channels: the budgeted door of :mod:`raven.providers.transcription`.

An empty ``api_key`` is passed through so the provider can fall back to the
``GROQ_API_KEY`` env var and decide for itself; returns "" on any failure, and
:func:`untranscribed_note` then says why, for the note the channel leaves the
agent in the transcription's place.
"""

from __future__ import annotations

from pathlib import Path


async def transcribe_audio(
    file_path: str | Path, api_key: str = "", *, channel: str = "", duration_s: float | None = None
) -> str:
    from raven.providers.transcription import remember_failure, transcribe

    result = await transcribe(file_path, api_key=api_key, channel=channel, duration_s=duration_s)
    if not result.ok:
        remember_failure(file_path, result.reason)
    return result.text


def untranscribed_note(file_path: str | Path) -> str:
    """`` (not transcribed: <why>)`` for a clip that came back without text, "" when the reason is unknown."""
    from raven.providers.transcription import failure_reason

    reason = failure_reason(file_path)
    return f" (not transcribed: {reason})" if reason else ""
