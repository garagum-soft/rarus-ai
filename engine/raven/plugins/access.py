"""Role-based plugin activation: a plugin runs only with the permissions its role or a grant gives it.

A plugin's *permissions* are what it contributes: each contribution kind it
fills (``tools``, ``hooks``, ``services``, ``memory_backends``, ``tool_gates``,
``session_observers``, ``onboard``) plus ``schedule`` for a hook that fires on
its own at a time. They are read off the manifest, not declared beside it, so
a plugin cannot ask for less than it takes.

Its *role* is where it came from -- who put it on this machine:

- ``builtin`` -- shipped inside raven: everything.
- ``installed`` -- put there by the operator (``~/.raven/plugins``, a
  ``plugins.dirs`` root, a pip-installed entry point): every contribution
  except ``schedule``, which is new power (code running with nobody's turn
  behind it) and is granted per plugin.
- ``project`` -- arrived with a repository (``./.raven/plugins``): nothing.
  Opening a checkout is not consent to run the code inside it.

``plugins.roles`` replaces a role's default set. What a role does not cover, a
*grant* can: ``raven plugin grant <id>`` records the operator's consent for
exactly the permissions the plugin needs, at exactly the version they read
(after Multica's install consent: scopes must match the manifest, and an
upgrade is a second consent). A plugin whose role and grant do not cover what
it needs is not activated, and the activation failure says what to grant.

A grant also names the manifest it was given to and a digest of that
plugin's files. An id and a version are only what a manifest says about
itself: another checkout can claim both, and a pull can change the code
under an unchanged version. Either is a different plugin, and asks again.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from raven.plugins.discover import DiscoveredPlugin, ManifestOrigin
from raven.plugins.manifest import PluginManifest
from raven.plugins.registry import PluginError
from raven.utils.atomic_io import atomic_replace, write_transaction

logger = logging.getLogger(__name__)

CONTRIBUTION_PERMISSIONS: tuple[str, ...] = (
    "memory_backends",
    "tools",
    "hooks",
    "services",
    "tool_gates",
    "session_observers",
    "onboard",
)
PERMISSIONS: tuple[str, ...] = (*CONTRIBUTION_PERMISSIONS, "schedule")
ROLES: tuple[str, ...] = ("builtin", "installed", "project")
DEFAULT_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "builtin": frozenset(PERMISSIONS),
    "installed": frozenset(CONTRIBUTION_PERMISSIONS),
    "project": frozenset(),
}


class PluginAccessDeniedError(PluginError):
    """A plugin needs a permission neither its role nor a grant gives it."""


class GrantError(ValueError):
    """A grant that cannot be recorded as asked."""


def required_permissions(manifest: PluginManifest) -> frozenset[str]:
    """What ``manifest`` takes: one permission per contribution kind it fills."""
    c = manifest.contributes
    need = {kind for kind in CONTRIBUTION_PERMISSIONS if getattr(c, kind)}
    if c.scheduled:
        need.add("schedule")
    return frozenset(need)


def role_of(discovered: DiscoveredPlugin) -> str:
    """The role a discovered plugin plays, from the source that found it.

    By its source, never by the manifest's own ``bundled`` flag: a file can
    claim anything about itself. Nor by where its path resolves: a plugin in
    ``./.raven/plugins`` that is a symlink or a junction to a directory
    elsewhere still arrived with the checkout, and its code is imported from
    there all the same.
    """
    if discovered.source == ManifestOrigin.BUNDLED:
        return "builtin"
    if discovered.source == ManifestOrigin.CHECKOUT:
        return "project"
    return "installed"


_DIGEST_SKIPPED_DIRS = frozenset({"__pycache__", ".git"})


def plugin_location(location: Path | None) -> str:
    """The manifest a grant names, as one string per file wherever it is reached from."""
    return "" if location is None else os.path.normcase(str(location.resolve()))


def plugin_digest(location: Path | None) -> str:
    """A digest of the manifest and every file beside it, or ``""`` for a packaged plugin.

    A plugin's files are its code, its prompts and its manifest alike, so all
    of them count; caches and a nested repository do not.
    """
    if location is None:
        return ""
    root = location.resolve().parent
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not _DIGEST_SKIPPED_DIRS.intersection(path.relative_to(root).parts)
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"\0unreadable")
        digest.update(b"\0")
    return digest.hexdigest()


def role_permissions(overrides: Mapping[str, Iterable[str]] | None = None) -> dict[str, frozenset[str]]:
    """The roles' permission sets, with ``plugins.roles`` replacing the defaults it names."""
    roles = dict(DEFAULT_ROLE_PERMISSIONS)
    for role, perms in (overrides or {}).items():
        if role not in ROLES:
            raise GrantError(f"plugins.roles names unknown role {role!r}; roles are {', '.join(ROLES)}")
        unknown = sorted(set(perms) - set(PERMISSIONS))
        if unknown:
            raise GrantError(f"plugins.roles.{role} names unknown permission(s) {unknown}")
        roles[role] = frozenset(perms)
    return roles


# -- grants -------------------------------------------------------------------------


@dataclass
class Grant:
    plugin_id: str
    version: str
    permissions: list[str]
    granted_by: str = ""
    granted_at: float = 0.0
    location: str = ""
    digest: str = ""


def default_grants_path() -> Path:
    from raven.home import raven_home

    return raven_home() / "plugin_grants.json"


class GrantStore:
    """The operator's per-plugin consents, one per plugin id."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_grants_path()

    def all(self) -> dict[str, Grant]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out: dict[str, Grant] = {}
        for pid, row in (raw.get("grants") or {}).items():
            try:
                out[pid] = Grant(**row)
            except TypeError:
                continue
        return out

    def get(self, plugin_id: str) -> Grant | None:
        return self.all().get(plugin_id)

    def _save(self, grants: dict[str, Grant]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"grants": {pid: asdict(g) for pid, g in sorted(grants.items())}}
        atomic_replace(self.path, json.dumps(payload, ensure_ascii=False, indent=2))

    def grant(
        self,
        manifest: PluginManifest,
        permissions: Iterable[str],
        *,
        by: str = "",
        location: Path | None = None,
    ) -> Grant:
        """Consent to exactly what ``manifest`` needs, at its version, in its files.

        Partial consent leaves a plugin broken in a way nobody sees; consent to
        something it does not ask for grants a power nobody was shown a reason
        for. So the set must be the manifest's own, item for item. ``location``
        is where the manifest was found (``None`` for a packaged plugin), and the
        grant holds only for that manifest with the files it has now.
        """
        given = sorted(set(permissions))
        needed = sorted(required_permissions(manifest))
        if given != needed:
            extra, missing = sorted(set(given) - set(needed)), sorted(set(needed) - set(given))
            parts = []
            if missing:
                parts.append(f"missing {', '.join(missing)}")
            if extra:
                parts.append(f"not requested: {', '.join(extra)}")
            raise GrantError(
                f"a grant must match what {manifest.id} {manifest.version} needs exactly "
                f"({', '.join(needed) or 'nothing'}); {'; '.join(parts)}"
            )
        record = Grant(
            plugin_id=manifest.id,
            version=manifest.version,
            permissions=given,
            granted_by=by or _operator(),
            granted_at=time.time(),
            location=plugin_location(location),
            digest=plugin_digest(location),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with write_transaction(self.path):
            grants = self.all()
            grants[manifest.id] = record
            self._save(grants)
        return record

    def revoke(self, plugin_id: str) -> bool:
        if not self.path.exists():
            return False
        with write_transaction(self.path):
            grants = self.all()
            if grants.pop(plugin_id, None) is None:
                return False
            self._save(grants)
        return True


def _operator() -> str:
    try:
        return f"user:{getpass.getuser()}"
    except (OSError, KeyError):
        return "user:operator"


# -- the decision ---------------------------------------------------------------------


@dataclass(frozen=True)
class AccessDecision:
    plugin_id: str
    version: str
    role: str
    required: frozenset[str]
    from_role: frozenset[str]
    granted: frozenset[str]
    missing: frozenset[str]
    grant_note: str = ""

    @property
    def allowed(self) -> bool:
        return not self.missing

    def reason(self) -> str:
        if self.allowed:
            return f"allowed as {self.role}" + (" with a grant" if self.granted else "")
        note = f" ({self.grant_note})" if self.grant_note else ""
        return (
            f"plugin {self.plugin_id!r} needs {', '.join(sorted(self.missing))}, which its role "
            f"{self.role!r} does not give and no grant covers{note}; "
            f"`raven plugin grant {self.plugin_id}` consents to it"
        )


@dataclass
class AccessPolicy:
    """Decides, per discovered plugin, whether its role and grant cover what it needs."""

    roles: dict[str, frozenset[str]] = field(default_factory=lambda: dict(DEFAULT_ROLE_PERMISSIONS))
    grants: GrantStore = field(default_factory=GrantStore)

    def decide(self, discovered: DiscoveredPlugin) -> AccessDecision:
        mf = discovered.manifest
        role = role_of(discovered)
        required = required_permissions(mf)
        from_role = required & self.roles.get(role, frozenset())
        grant = self.grants.get(mf.id) if required - from_role else None
        granted: frozenset[str] = frozenset()
        note = ""
        if grant is not None:
            if grant.version != mf.version:
                note = f"the grant is for version {grant.version}, this is {mf.version}"
            elif grant.location != plugin_location(discovered.location):
                note = f"the grant is for the plugin at {grant.location or 'a package'}, not this one"
            elif grant.digest != plugin_digest(discovered.location):
                note = "the plugin's files changed after the grant"
            else:
                granted = required & frozenset(grant.permissions)
                if required - from_role - granted:
                    note = "the grant was for less than this version needs"
        missing = required - from_role - granted
        return AccessDecision(
            plugin_id=mf.id,
            version=mf.version,
            role=role,
            required=required,
            from_role=from_role,
            granted=granted,
            missing=missing,
            grant_note=note,
        )

    def check(self, discovered: DiscoveredPlugin) -> None:
        decision = self.decide(discovered)
        if not decision.allowed:
            raise PluginAccessDeniedError(decision.reason())


def policy_from_config(config: object) -> AccessPolicy:
    """The policy a host config asks for: its ``plugins.roles`` over the defaults."""
    overrides = getattr(getattr(config, "plugins", None), "roles", None) or {}
    try:
        roles = role_permissions(overrides)
    except GrantError as exc:
        # A typo in the override must not open anything up, nor stop the host:
        # the defaults stand, and the log says why the override did not.
        logger.warning("plugins.roles ignored: %s", exc)
        roles = dict(DEFAULT_ROLE_PERMISSIONS)
    return AccessPolicy(roles=roles, grants=GrantStore())


__all__ = [
    "CONTRIBUTION_PERMISSIONS",
    "DEFAULT_ROLE_PERMISSIONS",
    "PERMISSIONS",
    "ROLES",
    "AccessDecision",
    "AccessPolicy",
    "Grant",
    "GrantError",
    "GrantStore",
    "PluginAccessDeniedError",
    "policy_from_config",
    "required_permissions",
    "role_of",
    "role_permissions",
]
