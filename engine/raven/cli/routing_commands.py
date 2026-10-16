"""``raven routing``: which router is on, and where a given request would go and why."""

from __future__ import annotations

import json

import typer

routing_app = typer.Typer(name="routing", help="Model routing: the router in force, and a request's route explained.")


@routing_app.command("status")
def status_cmd() -> None:
    """Whether routing is on, which backend, and the tier models."""
    from raven.config.loader import load_config

    config = load_config()
    routing = config.routing
    typer.echo(f"routing: {'on' if routing.enabled else 'off'} (backend {routing.backend})")
    typer.echo(f"default model: {config.agents.defaults.model or '-'}")
    if routing.backend == "tiers":
        for tier in ("simple", "standard", "complex"):
            typer.echo(f"  {tier:<9} {getattr(routing.tiers, tier) or '(borrows the next stronger tier)'}")
        typer.echo(f"  splits at: simple below {routing.simple_below:g}, complex from {routing.complex_from:g}")
    elif routing.backend == "ecoclaw":
        typer.echo(f"  profile: {routing.profile}")
    if not routing.enabled:
        typer.echo('turn it on: routing.enabled = true in config.json (see "Model routing" in the docs)')


@routing_app.command("explain")
def explain_cmd(
    text: str = typer.Argument(..., help="A request as a user would send it"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Where the tiers router would send this request, and why. Runs even with routing off."""
    from raven.config.loader import load_config
    from raven.routing.tier_router import TierRouter

    config = load_config()
    decision = TierRouter(config.routing, config.agents.defaults.model).decide(text)
    found = decision.assessment
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "tier": found.tier,
                    "score": found.score,
                    "reasons": found.reasons,
                    "model": decision.primary,
                    "escalation": decision.fallbacks,
                    "routing_on": config.routing.enabled and config.routing.backend == "tiers",
                },
                ensure_ascii=False,
            )
        )
        return
    typer.echo(f"tier: {found.summary()}")
    typer.echo(f"model: {decision.primary or '(the default model)'}")
    typer.echo(f"on failure: {' -> '.join(decision.fallbacks) or 'nothing further'}")
    if not (config.routing.enabled and config.routing.backend == "tiers"):
        typer.echo("(the tiers router is not in force now; this is what it would do)")
