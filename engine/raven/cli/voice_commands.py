"""``raven voice``: the transcription settings in force, and a clip through the same door the channels use."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import typer

voice_app = typer.Typer(name="voice", help="Voice input: transcription settings and a test transcription.")


@voice_app.command("status")
def status_cmd() -> None:
    """Which recognizer transcribes voice messages, under which limits, and whether it has a key."""
    from raven.config.live import default_live
    from raven.providers.auth import credential_status
    from raven.providers.transcription import settings

    cfg = settings()
    provider = cfg.provider or "groq"
    row = default_live().get(f"providers.{provider}") or {}
    row = row if isinstance(row, dict) else {}
    env = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider, "")
    # Whether the provider section is set up is providers.auth's to say; the
    # environment variable the recognizer also reads is reported beside it.
    from raven.config.loader import load_config

    section = load_config().providers.get(provider)
    configured = section is not None and credential_status(provider, section).ok
    in_env = bool(env and os.environ.get(env))
    key = "set" if configured else (f"set in {env}" if in_env else "not set")
    base = row.get("apiBase") or row.get("api_base") or ""
    typer.echo(f"recognizer: {provider}" + (f" at {base}" if base else ""))
    typer.echo(f"model: {cfg.model or ('whisper-large-v3' if provider == 'groq' else 'whisper-1')}")
    typer.echo(f"language: {cfg.language or 'detected'}")
    typer.echo(f"key: {key}" + (f" (providers.{provider}.apiKey or {env})" if env else ""))
    typer.echo(f"time budget per clip: {f'{cfg.max_seconds}s' if cfg.max_seconds else 'no limit'}")
    typer.echo(f"timeout per transcription: {f'{cfg.timeout_s:g}s' if cfg.timeout_s else 'the HTTP client default'}")
    typer.echo(
        f"slots: {cfg.slots} at once, a clip waits {cfg.queue_wait_s:g}s for one" if cfg.slots else "slots: no limit"
    )
    typer.echo(
        f"clip length without a header to read: {'ffprobe' if shutil.which('ffprobe') else 'unknown (no ffprobe)'}"
    )


@voice_app.command("transcribe")
def transcribe_cmd(
    path: Path = typer.Argument(..., help="An audio file"),
    duration: float = typer.Option(None, "--duration", help="The clip's length in seconds, when known"),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON"),
) -> None:
    """Transcribe one clip under the configured budget and say why when there is no text."""
    import asyncio

    from raven.config.loader import load_config
    from raven.providers.transcription import transcribe, transcription_api_key

    if not path.is_file():
        typer.echo(f"{path} is not a file", err=True)
        raise typer.Exit(2)
    key = transcription_api_key(load_config())
    result = asyncio.run(transcribe(path, api_key=key, channel="cli", duration_s=duration))
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "status": result.status,
                    "text": result.text,
                    "reason": result.reason,
                    "seconds": round(result.seconds, 2),
                    "duration_s": result.duration_s,
                },
                ensure_ascii=False,
            )
        )
    elif result.ok:
        typer.echo(result.text)
    else:
        typer.echo(f"not transcribed ({result.status}): {result.reason}", err=True)
    if not result.ok:
        raise typer.Exit(1)
