"""``python -m rarus_app`` runs the program; ``--engine ...`` runs the engine's command line.

The engine mode exists so a frozen build can start its engine from its own executable. A frozen
build has no ``python`` either, and ``sys.executable`` is this program, so it stands in for one:
the engine starts an agent as ``<python> <agent>/run.py --acp``, the agent re-executes itself as
``<python> -m raven acp ...``, and the memory component's server runs as ``<python> --everos
server start``. Each of those arrives here, and the installed components are put on ``sys.path``
before anything else is imported (rarus_app/component_paths.py), so every process sees them.
"""

from __future__ import annotations

import os
import sys

# Interpreter options a caller may put before the script or ``-m``: accepted and ignored. The
# engine starts its children with ``-P``; ``-X``/``-W`` take a value.
_FLAGS = frozenset({"-P", "-u", "-B", "-E", "-s", "-S", "-I", "-O", "-OO", "-q", "-b", "-bb", "-d", "-v"})
_FLAGS_WITH_VALUE = frozenset({"-X", "-W"})


def _give_streams_a_sink() -> None:
    """A windowed frozen program has no console, so ``sys.stdout`` and ``sys.stderr`` are None.

    The engine prints and logs through them; writing to None raises. Anything the engine says
    belongs in the log file its parent redirected it to, so only a missing stream is replaced.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115 -- lives as long as the process


def python_call(args: list[str]) -> list[str] | None:
    """What follows the interpreter options when ``args`` is a python command line, else None."""
    i = 0
    while i < len(args):
        if args[i] in _FLAGS_WITH_VALUE:
            i += 2
        elif args[i] in _FLAGS:
            i += 1
        else:
            break
    rest = args[i:]
    if len(rest) >= 2 and rest[0] in ("-m", "-c"):
        return rest
    if rest and rest[0].lower().endswith((".py", ".pyw")):
        return rest
    return None


def run_python(rest: list[str]) -> None:
    """``-m module ...``, ``-c code ...`` or ``script.py ...``, as the interpreter would run them."""
    import runpy

    if rest[0] == "-m":
        sys.argv = [rest[1], *rest[2:]]
        runpy.run_module(rest[1], run_name="__main__", alter_sys=True)
    elif rest[0] == "-c":
        sys.argv = ["-c", *rest[2:]]
        exec(compile(rest[1], "<string>", "exec"), {"__name__": "__main__"})  # noqa: S102 -- the interpreter's own -c
    else:
        script = os.path.abspath(rest[0])
        sys.argv = [script, *rest[1:]]
        sys.path.insert(0, os.path.dirname(script))
        runpy.run_path(script, run_name="__main__")


def _activate_components() -> None:
    from rarus_app.component_paths import activate

    if getattr(sys, "frozen", False):
        # The memory server's CLI is this program too (raven_everos/server.py reads it).
        import json

        os.environ.setdefault("EVEROS_CLI", json.dumps([sys.executable, "--everos"]))
    try:
        activate()
    except OSError:
        pass  # a damaged components folder must not keep the program from starting


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--engine"] or args[:1] in (["rarus"], ["raven"]):
        # ``rarus``/``raven``: the gateway restarts itself as ``<python> <its argv>``.
        _activate_components()
        _give_streams_a_sink()
        from raven.cli.entry import run

        sys.argv = ["rarus", *args[1:]]
        run()
        return 0
    if args[:1] == ["--everos"]:
        _activate_components()
        _give_streams_a_sink()
        from everos.entrypoints.cli.main import app

        sys.argv = ["everos", *args[1:]]
        app()
        return 0
    rest = python_call(args)
    if rest is not None:
        _activate_components()
        _give_streams_a_sink()
        run_python(rest)
        return 0
    from rarus_app.main import run_app

    return run_app(args)


if __name__ == "__main__":
    raise SystemExit(main())
