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


def _utf8_streams() -> None:
    """UTF-8 on the standard streams, as the engine's own CLI sets them (raven/cli/commands.py).

    A frozen program's streams start in the Windows code page, which cannot write most of what EverOS
    and the agents print (a rich help screen died on an arrow), nor read a Russian prompt correctly.
    """
    if sys.platform != "win32":
        return
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


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


def _exec_in_place() -> None:
    """Make ``os.execv``/``os.execve`` of this program continue in the same process.

    An agent's launcher ends with ``os.execv(sys.executable, [..., "-m", "raven", "acp", ...])``.
    On Windows exec starts a new process, and a windowed executable started that way does not get
    the pipes its parent talks ACP over: the agent would answer nothing. Running the new command
    line here instead keeps the pipes; any other program is still exec'd as asked.
    """
    if not getattr(sys, "frozen", False) or sys.platform != "win32" or getattr(os, "_rarus_in_place", False):
        return
    real_execv, real_execve = os.execv, os.execve
    own = os.path.normcase(os.path.abspath(sys.executable))

    def ours(path: object) -> bool:
        return os.path.normcase(os.path.abspath(os.fsdecode(path))) == own  # type: ignore[arg-type]

    def execv(path, args):  # type: ignore[no-untyped-def]
        if not ours(path):
            return real_execv(path, args)
        raise SystemExit(main([os.fsdecode(a) for a in list(args)[1:]]))

    def execve(path, args, env):  # type: ignore[no-untyped-def]
        if not ours(path):
            return real_execve(path, args, env)
        os.environ.clear()
        os.environ.update(env)
        raise SystemExit(main([os.fsdecode(a) for a in list(args)[1:]]))

    os.execv, os.execve = execv, execve
    os._rarus_in_place = True  # type: ignore[attr-defined]


def run_python(rest: list[str]) -> None:
    """``-m module ...``, ``-c code ...`` or ``script.py ...``, as the interpreter would run them."""
    import runpy

    _exec_in_place()
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
        _utf8_streams()
        from raven.cli.entry import run

        sys.argv = ["rarus", *args[1:]]
        run()
        return 0
    if args[:1] == ["--everos"]:
        _activate_components()
        _give_streams_a_sink()
        _utf8_streams()
        from everos.entrypoints.cli.main import app

        sys.argv = ["everos", *args[1:]]
        app()
        return 0
    rest = python_call(args)
    if rest is not None:
        _activate_components()
        _give_streams_a_sink()
        _utf8_streams()
        run_python(rest)
        return 0
    from rarus_app.main import run_app

    return run_app(args)


if __name__ == "__main__":
    raise SystemExit(main())
