"""``python -m rarus_app`` runs the program; ``--engine ...`` runs the engine's command line.

The engine mode exists so a frozen build can start its engine from its own executable.
"""

from __future__ import annotations

import os
import sys


def _give_streams_a_sink() -> None:
    """A windowed frozen program has no console, so ``sys.stdout`` and ``sys.stderr`` are None.

    The engine prints and logs through them; writing to None raises. Anything the engine says
    belongs in the log file its parent redirected it to, so only a missing stream is replaced.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115 -- lives as long as the process


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--engine"]:
        _give_streams_a_sink()
        from raven.cli.entry import run

        sys.argv = ["rarus", *args[1:]]
        run()
        return 0
    from rarus_app.main import run_app

    return run_app(args)


if __name__ == "__main__":
    raise SystemExit(main())
