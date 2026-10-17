"""``python -m rarus_app`` runs the program; ``--engine ...`` runs the engine's command line.

The engine mode exists so a frozen build can start its engine from its own executable.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--engine"]:
        from raven.cli.entry import run

        sys.argv = ["rarus", *args[1:]]
        run()
        return 0
    from rarus_app.main import run_app

    return run_app(args)


if __name__ == "__main__":
    raise SystemExit(main())
