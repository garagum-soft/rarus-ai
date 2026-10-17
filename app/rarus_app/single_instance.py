"""One copy of the program at a time (spec 7.2: the named mutex "Local" + backslash + "RarusAI").

On Windows the guard is a named mutex, which the system releases when the process
dies however it dies. Elsewhere (and in tests, which must not collide with a real
copy) it is an exclusive lock on a file under the home.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType

MUTEX_NAME = "Local\\RarusAI"
_ERROR_ALREADY_EXISTS = 183


class AlreadyRunningError(RuntimeError):
    """Another copy of the program holds the guard."""


class SingleInstance:
    """``with SingleInstance(): ...`` raises :class:`AlreadyRunningError` when a copy is up."""

    def __init__(self, *, name: str = MUTEX_NAME, lock_file: Path | None = None) -> None:
        self.name = name
        self._lock_file = lock_file
        self._handle: object | None = None
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._lock_file is None and sys.platform == "win32":
            self._acquire_mutex()
        else:
            self._acquire_file(self._lock_file or _default_lock_file())

    def release(self) -> None:
        if self._handle is not None:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            kernel32.ReleaseMutex(self._handle)
            kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._fd is not None:
            try:
                _unlock(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None

    def _acquire_mutex(self) -> None:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise OSError("could not create the single-instance mutex")
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise AlreadyRunningError(self.name)
        self._handle = ctypes.c_void_p(handle)

    def _acquire_file(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock(fd)
        except OSError as exc:
            os.close(fd)
            raise AlreadyRunningError(str(path)) from exc
        self._fd = fd

    def __enter__(self) -> SingleInstance:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def _default_lock_file() -> Path:
    from rarus_app.paths import app_home

    return app_home() / "app.lock"


if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
