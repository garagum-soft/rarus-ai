"""Routing by how demanding the request is: a model per tier, and the stronger tiers behind it.

``routing.backend = "tiers"`` maps ``simple`` / ``standard`` / ``complex`` to
models the user already has -- any provider, a local one for the simple tier is
the usual reason -- and :mod:`raven.routing.complexity` places each request in
a tier. The fallback chain is the escalation: when the chosen model fails (an
error, a rate limit, an overload), the turn moves to the next stronger tier's
model, and to the default model last. A tier left empty borrows the next
stronger one's model, so configuring only ``simple`` sends easy requests to a
cheap model and everything else to the default.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from raven.routing.complexity import TIERS, Assessment, assess


@dataclass(frozen=True)
class Decision:
    assessment: Assessment
    primary: str | None
    fallbacks: list[str]


class TierRouter:
    def __init__(
        self,
        settings: Any,
        default_model: str | None,
        *,
        settings_source: Callable[[], Any] | None = None,
        default_source: Callable[[], str | None] | None = None,
    ) -> None:
        self._settings = settings
        self._default = default_model
        # Live readers, as the other routers take them: an edited tier or default
        # routes the next turn without a restart.
        self._settings_source = settings_source
        self._default_source = default_source

    def _current(self) -> tuple[Any, str | None]:
        settings, default = self._settings, self._default
        if self._settings_source is not None:
            try:
                settings = self._settings_source() or settings
            except Exception:  # noqa: BLE001 -- a bad read keeps the settings the router was built with
                pass
        if self._default_source is not None:
            try:
                default = self._default_source() or default
            except Exception:  # noqa: BLE001
                pass
        return settings, default

    def decide(self, prompt: str) -> Decision:
        settings, default = self._current()
        found = assess(prompt, simple_below=settings.simple_below, complex_from=settings.complex_from)
        models = [str(getattr(settings.tiers, tier, "") or "") for tier in TIERS]
        start = TIERS.index(found.tier)
        # The chosen tier's model, else the next stronger one that is set, else the default.
        primary = next((m for m in models[start:] if m), None) or default
        chain: list[str] = []
        for model in [*models[start:], default or ""]:
            if model and model != primary and model not in chain:
                chain.append(model)
        return Decision(found, primary, chain)

    async def select_model_chain(self, prompt: str) -> tuple[str | None, list[str]]:
        decision = self.decide(prompt)
        logger.info("Router: tier {} -> {}", decision.assessment.summary(), decision.primary)
        return decision.primary, decision.fallbacks


__all__ = ["Decision", "TierRouter"]
