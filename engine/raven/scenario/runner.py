"""Running one scenario: load its file, hand ``main`` the context, keep the record."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import re
import sys
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from raven.scenario.api import SCRIPT, Scenario, ScenarioError
from raven.scenario.catalog import ScenarioSpec, bind_args
from raven.scenario.journal import Journal, default_runs_dir


@dataclass
class Outcome:
    run_id: str
    name: str
    status: str
    """``succeeded``, ``failed`` or ``timed out``."""
    result: str
    error: str
    seconds: float
    record: Path
    runs: list[dict[str, Any]]

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


def _load(spec: ScenarioSpec, module_name: str) -> Any:
    loader_spec = importlib.util.spec_from_file_location(module_name, spec.path)
    if loader_spec is None or loader_spec.loader is None:
        raise ScenarioError(f"cannot load {spec.path}")
    module = importlib.util.module_from_spec(loader_spec)
    # Present in sys.modules while it runs: dataclasses and pickling look a
    # module up by name, and a scenario may define its own.
    sys.modules[module_name] = module
    loader_spec.loader.exec_module(module)
    return module


async def run_scenario(
    spec: ScenarioSpec,
    given: dict[str, str],
    *,
    manager: Any,
    workspace: Path,
    timeout: float | None = None,
    echo: Callable[[str], None] = lambda line: None,
    runs_root: Path | None = None,
) -> Outcome:
    """Run ``spec`` to the end (or ``timeout``) and return what happened; the record is on disk either way."""
    if spec.problem:
        raise ScenarioError(f"{spec.name} cannot run: {spec.problem}")
    try:
        args = bind_args(spec, given)
    except ValueError as exc:
        raise ScenarioError(str(exc)) from None
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{spec.name}-{uuid.uuid4().hex[:4]}"
    journal = Journal(runs_root or default_runs_dir(), run_id)
    mail_session = f"scenario:{run_id}"
    started = time.time()
    journal.meta(
        name=spec.name,
        path=str(spec.path),
        source=spec.source,
        args=args,
        workspace=str(workspace),
        mail_session=mail_session,
        status="running",
        started_at=started,
    )
    journal.event("scenario.start", name=spec.name, args=args)
    module_name = "raven_scenario_" + re.sub(r"\W", "_", run_id)
    sc: Scenario | None = None
    status, result, error = "failed", "", ""
    try:
        module = _load(spec, module_name)
        main = getattr(module, "main", None)
        if main is None or not inspect.iscoroutinefunction(main):
            raise ScenarioError(f"{spec.path} has no `async def main(sc)`")
        sc = Scenario(
            name=spec.name,
            run_id=run_id,
            args=args,
            workspace=workspace,
            manager=manager,
            journal=journal,
            mail_session=mail_session,
            echo=echo,
        )
        body = asyncio.ensure_future(main(sc))
        done, _ = await asyncio.wait({body}, timeout=timeout)
        if not done:
            body.cancel()
            await asyncio.gather(body, return_exceptions=True)
            status, error = "timed out", f"the scenario ran past {timeout:g}s"
        else:
            value = body.result()
            status, result = "succeeded", "" if value is None else str(value)
    except ScenarioError as exc:
        error = str(exc)
    except Exception:  # noqa: BLE001 -- the script's own failure is the run's outcome
        error = "".join(traceback.format_exc().splitlines(keepends=True)[-12:]).strip()
    finally:
        runs = await sc.close() if sc is not None else []
        sys.modules.pop(module_name, None)
    seconds = round(time.time() - started, 2)
    journal.event("scenario.end", status=status, error=error[:4000], seconds=seconds)
    journal.result(result)
    journal.meta(status=status, error=error, ended_at=time.time(), seconds=seconds, runs=runs)
    return Outcome(run_id, spec.name, status, result, error, seconds, journal.dir, runs)


__all__ = ["SCRIPT", "Outcome", "run_scenario"]
