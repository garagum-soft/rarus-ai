"""``rarus explain``: why a refusal happened and which config value lifts it."""

from __future__ import annotations

import json
import sys

import typer
from rich.console import Console

console = Console()


def register(app: typer.Typer) -> None:
    @app.command()
    def explain(
        text: str = typer.Argument("-", help="The refusal text; '-' reads it from stdin"),
        json_output: bool = typer.Option(False, "--json", help="Emit the rule, hint and config values as JSON"),
    ) -> None:
        """Explain a refusal: the rule that recognises it and the config value behind it."""
        from raven.agent.block_hints import explain as explain_block
        from raven.config.live import LiveConfig

        refusal = sys.stdin.read() if text == "-" else text
        hint = explain_block(refusal, LiveConfig().raw())
        if json_output:
            payload = None if hint is None else {"rule": hint.rule, "hint": hint.text, "settings": hint.settings}
            typer.echo(json.dumps(payload, default=str))
        elif hint is None:
            console.print("No known block in this text.")
        else:
            from rich.markup import escape

            console.print(f"[bold]{hint.rule}[/bold]: {escape(hint.text)}", highlight=False, soft_wrap=True)
        if hint is None:
            raise typer.Exit(1)
