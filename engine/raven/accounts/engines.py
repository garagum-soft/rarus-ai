"""Each account's engine: an ordinary ``raven gateway`` on a home of its own, started and watched here.

An engine is reached on ``127.0.0.1`` only, through its page port, with the
token this supervisor gave it (``RAVEN_SERVE_TOKEN``); nobody else learns the
token. One that is already running on its home -- the administrator's own
gateway, or engines that outlived a restart of the account gateway -- is
adopted from its ``serve.json`` instead of being started a second time.

What separates one account's engine from another's:

- its own ``RAVEN_HOME``: config and keys, conversations, agents, messengers,
  the usage files it is charged by;
- its own logins for the CLI agents (``CLAUDE_CONFIG_DIR``, ``CODEX_HOME``)
  and none of the server's API keys in its environment;
- what the administrator lends it, and only that: a provider of the server's
  config is copied into the engine's config, a subscription is reached by
  pointing the engine at the server's login (:func:`env_for`).

They run as the same operating-system user as the server, so the separation is
Raven's, not the operating system's: an agent allowed to run shell commands
without a sandbox can read other homes. The admin page says so.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from raven.accounts.gate import ENV_ACCOUNT
from raven.accounts.paths import engine_home, user_dir
from raven.accounts.store import Account, AccountStore
from raven.home import raven_home

SUBSCRIPTIONS = ("claude_subscription", "openai_codex")
"""The server's logins an administrator can lend, beside the providers in its config."""

# Never handed to an account's engine: the server's own credentials, and the
# variables that would point an engine at the server's logins.
_STRIPPED = {
    "CHATGPT_TOKEN_DIR",
    "CHATGPT_AUTH_FILE",
    "GITHUB_COPILOT_TOKEN_DIR",
    "MINIMAX_OAUTH_TOKEN_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "RAVEN_SERVE_TOKEN",
    "RAVEN_SERVE_COOKIE",
    "RAVEN_ACCOUNTS_ROOT",
}
_SECRET_VAR = re.compile(r"(_API_KEY|_API_TOKEN|_AUTH_TOKEN|_ACCESS_TOKEN|_SECRET|_SECRET_KEY)$", re.I)

START_TIMEOUT_S = 180.0
_RESTARTS_PER_HOUR = 5


@dataclass
class Engine:
    account_id: str
    home: Path
    page_port: int
    token: str
    pid: int | None = None
    state: str = "stopped"
    """``starting``, ``running``, ``stopped`` or ``failed``."""
    started_at: float = 0.0
    error: str = ""
    adopted: bool = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.page_port}"

    def public(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "pid": self.pid,
            "port": self.page_port,
            "startedAt": self.started_at,
            "error": self.error,
            "adopted": self.adopted,
        }


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _alive(pid: int | None) -> bool:
    from raven.utils.pid import pid_alive

    try:
        return bool(pid) and pid_alive(int(pid))
    except (TypeError, ValueError):
        return False


def server_providers(server_home: Path | None = None) -> dict[str, Any]:
    """The provider blocks of the server's own config, which an administrator can lend."""
    try:
        raw = json.loads(((server_home or raven_home()) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    providers = raw.get("providers") if isinstance(raw, dict) else None
    return {k: v for k, v in (providers or {}).items() if isinstance(v, dict)}


def lendable(server_home: Path | None = None) -> list[str]:
    """What an administrator can lend: the server's configured providers, then its subscriptions."""
    return sorted(server_providers(server_home)) + list(SUBSCRIPTIONS)


def env_for(account: Account, home: Path, token: str, *, server_home: Path | None = None) -> dict[str, str]:
    """The environment one account's engine is started with."""
    server_home = server_home or raven_home()
    env = dict(os.environ)
    if not account.server_home:
        for name in list(env):
            if name in _STRIPPED or _SECRET_VAR.search(name):
                env.pop(name)
        cli = home / "cli"
        if "claude_subscription" not in account.lent:
            env["CLAUDE_CONFIG_DIR"] = str(cli / "claude")
        if "openai_codex" in account.lent:
            env["CHATGPT_TOKEN_DIR"] = str(server_home / "oauth" / "chatgpt")
        else:
            env["CODEX_HOME"] = str(cli / "codex")
    env.update(
        {
            "RAVEN_HOME": str(home),
            ENV_ACCOUNT: account.id,
            "RAVEN_SERVE_TOKEN": token,
            "RAVEN_SERVE_PORT_STRICT": "1",
            "RAVEN_NO_UPDATE_CHECK": "1",
            "PYTHONUTF8": "1",
        }
    )
    return env


def apply_lent_providers(account: Account, home: Path, *, server_home: Path | None = None) -> list[str]:
    """Copy the server's provider blocks the account is lent into its engine config; drop withdrawn ones.

    A withdrawn block is removed only while it is still the server's copy, so a
    key the account entered for itself under the same name is never deleted.
    Returns the providers now lent. Written live: an engine re-reads its
    provider keys when they change.
    """
    if account.server_home:
        return []
    server = server_providers(server_home)
    path = home / "config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    providers = config.setdefault("providers", {})
    record = user_dir(account.id) / "lent_providers.json"
    try:
        applied = set(json.loads(record.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        applied = set()
    wanted = {name for name in account.lent if name in server}
    changed = False
    for name in sorted(wanted):
        if providers.get(name) != server[name]:
            providers[name] = server[name]
            changed = True
    for name in sorted(applied - wanted):
        if name in providers and providers[name] == server.get(name):
            providers.pop(name)
            changed = True
    if changed:
        from raven.utils.atomic_io import atomic_replace

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(path, json.dumps(config, ensure_ascii=False, indent=2), mode=0o600)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(sorted(wanted)), encoding="utf-8")
    return sorted(wanted)


def _loopback(url: str) -> bool:
    from urllib.parse import urlparse

    return (urlparse(url or "").hostname or "") in ("127.0.0.1", "localhost", "::1")


def apply_server_transcription(account: Account, home: Path, *, server_home: Path | None = None) -> bool:
    """Give an engine the server's speech recognizer when it has none of its own; True when it did.

    Only a recognizer this machine runs itself (its provider's apiBase on
    loopback, e.g. a local GigaAM or Whisper server): that costs no one's key
    and no credits, so every account may use it. A recognizer behind someone's
    key is never copied; an account that set its own keeps it.
    """
    if account.server_home:
        return False
    try:
        server = json.loads(((server_home or raven_home()) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    section = server.get("transcription") if isinstance(server, dict) else None
    name = str((section or {}).get("provider") or "")
    block = (server.get("providers") or {}).get(name) if name else None
    if not isinstance(section, dict) or not isinstance(block, dict):
        return False
    if not _loopback(str(block.get("apiBase") or block.get("api_base") or "")):
        return False
    path = home / "config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    if not isinstance(config, dict) or config.get("transcription"):
        return False
    config["transcription"] = section
    config.setdefault("providers", {}).setdefault(name, {"apiBase": block.get("apiBase") or block.get("api_base")})
    from raven.utils.atomic_io import atomic_replace

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace(path, json.dumps(config, ensure_ascii=False, indent=2), mode=0o600)
    return True


def read_serve(home: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads((home / "serve.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and raw.get("port") and raw.get("token") else None


async def healthy(port: int, *, timeout: float = 2.0) -> bool:
    import aiohttp

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(f"http://127.0.0.1:{port}/health") as response:
                if response.status != 200:
                    return False
                body = await response.json(content_type=None)
                return isinstance(body, dict) and body.get("service") == "raven-serve"
    except Exception:  # noqa: BLE001 - not answering is the answer
        return False


class EngineSupervisor:
    def __init__(
        self,
        store: AccountStore | None = None,
        *,
        port_base: int = 18900,
        python: str | None = None,
        server_home: Path | None = None,
        start_timeout: float = START_TIMEOUT_S,
        command: list[str] | None = None,
    ) -> None:
        self.store = store or AccountStore()
        self.port_base = port_base
        self.python = python or sys.executable
        self.server_home = server_home or raven_home()
        self.start_timeout = start_timeout
        # A stand-in engine command for tests, `{page}` and `{health}` filled in;
        # the real one is `python -m raven gateway`.
        self.command = command
        self.engines: dict[str, Engine] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._restarts: dict[str, list[float]] = {}
        self._watch: asyncio.Task | None = None

    # -- ports -----------------------------------------------------------------------------

    def _ports(self, account: Account) -> tuple[int, int]:
        if account.page_port and account.health_port:
            return account.page_port, account.health_port
        taken = {p for a in self.store.all() for p in (a.page_port, a.health_port) if p}
        port = self.port_base
        while port in taken or (port + 1) in taken or not _free(port) or not _free(port + 1):
            port += 2

        def change(row: Account) -> None:
            row.page_port, row.health_port = port, port + 1

        self.store.update(account.id, change)
        return port, port + 1

    # -- lifecycle -------------------------------------------------------------------------

    def engine(self, account_id: str) -> Engine | None:
        return self.engines.get(account_id)

    async def ensure(self, account: Account) -> Engine:
        """The account's engine, running and answering; started, or adopted, when it is not."""
        lock = self._locks.setdefault(account.id, asyncio.Lock())
        async with lock:
            current = self.engines.get(account.id)
            if current is not None and current.state == "running" and await healthy(current.page_port):
                return current
            home = engine_home(account)
            adopted = await self._adopt(account, home)
            if adopted is not None:
                return adopted
            return await self._start(account, home)

    async def _adopt(self, account: Account, home: Path) -> Engine | None:
        serve = read_serve(home)
        if serve is None or not _alive(serve.get("pid")) or not await healthy(int(serve["port"])):
            return None
        engine = Engine(
            account.id,
            home,
            int(serve["port"]),
            str(serve["token"]),
            pid=serve.get("pid"),
            state="running",
            started_at=time.time(),
            adopted=True,
        )
        self.engines[account.id] = engine
        logger.info("account {}: adopted the engine already running on {}", account.username, engine.page_port)
        return engine

    async def _start(self, account: Account, home: Path) -> Engine:
        home.mkdir(parents=True, exist_ok=True)
        apply_lent_providers(account, home, server_home=self.server_home)
        apply_server_transcription(account, home, server_home=self.server_home)
        page, health = self._ports(account)
        token = secrets.token_urlsafe(32)
        engine = Engine(account.id, home, page, token, state="starting", started_at=time.time())
        self.engines[account.id] = engine
        argv = (
            [part.format(page=page, health=health) for part in self.command]
            if self.command
            else [self.python, "-m", "raven", "gateway", "--page-port", str(page), "--port", str(health)]
        )
        log_path = user_dir(account.id) / "engine.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        flags = 0
        if sys.platform == "win32":
            # Its own process group and no console: the account gateway's Ctrl-C
            # must not reach the engines, and a console window per account helps nobody.
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        with log_path.open("ab") as log:
            proc = subprocess.Popen(
                [*argv],
                env=env_for(account, home, token, server_home=self.server_home),
                cwd=str(home),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                start_new_session=sys.platform != "win32",
            )
        self._procs[account.id] = proc
        engine.pid = proc.pid
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                engine.state, engine.error = "failed", f"the engine exited with code {proc.returncode}; see {log_path}"
                logger.warning("account {}: {}", account.username, engine.error)
                return engine
            if await healthy(page, timeout=1.0):
                engine.state = "running"
                logger.info("account {}: engine running on {} (pid {})", account.username, page, proc.pid)
                return engine
            await asyncio.sleep(0.5)
        engine.state, engine.error = (
            "failed",
            f"the engine did not answer within {self.start_timeout:.0f}s; see {log_path}",
        )
        self._kill(proc)
        return engine

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                from raven.utils.proc_tree import kill_tree

                kill_tree(proc.pid)
            else:
                proc.terminate()
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001 - a kill that cannot be confirmed is left to the OS
            pass

    async def stop(self, account_id: str) -> None:
        engine = self.engines.get(account_id)
        if engine is None:
            return
        proc = self._procs.pop(account_id, None)
        if self.command is None:
            env = dict(os.environ, RAVEN_HOME=str(engine.home))
            await asyncio.to_thread(
                subprocess.run,
                [self.python, "-m", "raven", "gateway", "stop"],
                env=env,
                capture_output=True,
                timeout=60,
                check=False,
            )
        if proc is not None:
            for _ in range(40):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.25)
            self._kill(proc)
        engine.state = "stopped"

    async def stop_all(self) -> None:
        """Stop the engines this supervisor started; adopted ones were running before it and stay."""
        for account_id, engine in list(self.engines.items()):
            if not engine.adopted:
                await self.stop(account_id)

    async def restart(self, account: Account) -> Engine:
        await self.stop(account.id)
        self.engines.pop(account.id, None)
        return await self.ensure(account)

    # -- the watch -------------------------------------------------------------------------

    async def watch_once(self) -> None:
        """Restart an engine that exited on its own, at most five times an hour."""
        for account_id, proc in list(self._procs.items()):
            engine = self.engines.get(account_id)
            if engine is None or engine.state != "running" or proc.poll() is None:
                continue
            account = self.store.get(account_id)
            recent = [t for t in self._restarts.get(account_id, []) if time.time() - t < 3600]
            if account is None or account.disabled or len(recent) >= _RESTARTS_PER_HOUR:
                engine.state, engine.error = "failed", f"the engine exited with code {proc.returncode}"
                continue
            self._restarts[account_id] = [*recent, time.time()]
            logger.warning("account {}: engine exited ({}); restarting", account.username, proc.returncode)
            self._procs.pop(account_id, None)
            engine.state = "stopped"
            await self.ensure(account)

    def start_watch(self, every_s: float = 10.0) -> None:
        async def loop() -> None:
            while True:
                try:
                    await self.watch_once()
                except Exception:  # noqa: BLE001 - one bad pass is logged; the next one runs
                    logger.exception("engine watch failed")
                await asyncio.sleep(every_s)

        if self._watch is None:
            self._watch = asyncio.create_task(loop(), name="accounts-engine-watch")

    def stop_watch(self) -> None:
        if self._watch is not None:
            self._watch.cancel()
            self._watch = None


__all__ = [
    "SUBSCRIPTIONS",
    "Engine",
    "EngineSupervisor",
    "apply_lent_providers",
    "env_for",
    "healthy",
    "lendable",
    "read_serve",
    "server_providers",
]
