"""Program start: one instance, the engine in the background, the window on top."""

from __future__ import annotations

import argparse
import sys

from rarus_app.controller import Controller
from rarus_app.single_instance import AlreadyRunning, SingleInstance


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="rarus-app", description="Rarus AI")
    parser.add_argument("--background", action="store_true", help="start hidden in the tray")
    parser.add_argument("--no-window", action="store_true", help="run the engine only (smoke tests)")
    return parser.parse_args(argv)


def run_app(argv: list[str]) -> int:
    args = parse_args(argv)
    guard = SingleInstance()
    try:
        guard.acquire()
    except AlreadyRunning:
        print("Rarus AI is already running.", file=sys.stderr)
        return 0
    controller = Controller()
    try:
        if args.no_window:
            controller.start_engine(wait=True)
            state = controller.boot()
            print(state.get("phase"))
            return 0 if state.get("phase") in {"signin", "ready"} else 1
        from rarus_app.window import run_window

        return run_window(controller, hidden=args.background)
    finally:
        controller.stop_engine()
        guard.release()
