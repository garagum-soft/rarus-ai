"""A repository's plugin cannot pass for an installed one, nor borrow another plugin's grant.

The role was read from where a plugin's path resolved, while its code was
imported from where it was found: a ``./.raven/plugins/<id>`` that is a
symlink or a junction to a directory elsewhere played ``installed`` and ran
without ``raven plugin grant``. And a grant named only an id and a version,
which any manifest can claim, so a second checkout with the same two words
inherited the first one's consent.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from raven.config.raven import RavenConfig
from raven.core import plugin_stack
from raven.plugins.access import GrantStore
from raven.plugins.discover import DiscoveredPlugin, ManifestOrigin, PluginDiscovery
from raven.plugins.manifest import PluginManifest

TOOL_MODULE = """
import pathlib

pathlib.Path(__file__).with_name("RAN.txt").write_text("ran", encoding="utf-8")

def make_tool(ctx):
    return None
"""


def _tool_plugin(root: Path, pid: str, *, version: str = "1.0.0") -> Path:
    mod = f"{pid.replace('-', '_')}_mod"
    plug = root / pid
    plug.mkdir(parents=True, exist_ok=True)
    (plug / "raven-plugin.toml").write_text(
        f'[plugin]\nid = "{pid}"\nversion = "{version}"\n'
        f'[[plugin.contributes.tools]]\nname = "{pid}-tool"\nfactory = "{mod}:make_tool"\n',
        encoding="utf-8",
    )
    (plug / f"{mod}.py").write_text(TOOL_MODULE, encoding="utf-8")
    return plug


def _link_dir(link: Path, target: Path) -> None:
    """A directory symlink, or on Windows without the privilege for one, a junction."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            pytest.skip("cannot create a directory symlink here")
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")


@pytest.fixture
def sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    found = {
        "bundled_dir": tmp_path / "none",
        "user_dir": tmp_path / "user_plugins",
        "project_dir": tmp_path / "repo" / ".raven" / "plugins",
        "entry_points_group": None,
    }
    monkeypatch.setattr(plugin_stack, "plugin_discovery_sources", lambda: dict(found))
    yield found
    for name in [n for n in sys.modules if n.endswith("_mod")]:
        sys.modules.pop(name, None)


def _registry():
    return plugin_stack.build_plugin_registry(RavenConfig.model_validate({}), notify=lambda _m: None)


def test_a_linked_checkout_plugin_still_plays_project_and_runs_nothing(sources, tmp_path: Path) -> None:
    elsewhere = _tool_plugin(tmp_path / "repo" / "vendor", "evil")
    _link_dir(sources["project_dir"] / "evil", elsewhere)

    registry = _registry()

    assert registry.activated_ids() == []
    reason = {f.plugin_id: f.reason for f in registry.activation_failures()}["evil"]
    assert "role 'project'" in reason
    assert not (elsewhere / "RAN.txt").exists(), "nothing of the plugin was imported"


def test_a_grant_does_not_follow_the_id_to_another_checkout(sources, tmp_path: Path) -> None:
    granted_dir = _tool_plugin(tmp_path / "repo_a" / ".raven" / "plugins", "helper")
    granted = DiscoveredPlugin(
        manifest=PluginManifest.from_toml_path(granted_dir / "raven-plugin.toml"),
        source=ManifestOrigin.CHECKOUT,
        location=granted_dir / "raven-plugin.toml",
    )
    GrantStore().grant(granted.manifest, ["tools"], location=granted.location)

    impostor = _tool_plugin(sources["project_dir"], "helper")
    (impostor / "helper_mod.py").write_text(TOOL_MODULE + "\n# other code\n", encoding="utf-8")

    registry = _registry()

    assert registry.activated_ids() == []
    assert "not this one" in {f.plugin_id: f.reason for f in registry.activation_failures()}["helper"]


def test_a_grant_ends_when_the_plugin_s_files_change(sources) -> None:
    plug = _tool_plugin(sources["project_dir"], "helper")
    found = {d.manifest.id: d for d in plugin_stack.discover_plugins(None)}["helper"]
    GrantStore().grant(found.manifest, ["tools"], location=found.location)
    assert _registry().activated_ids() == ["helper"]

    (plug / "helper_mod.py").write_text(TOOL_MODULE + "\nimport os  # pulled in later\n", encoding="utf-8")
    sys.modules.pop("helper_mod", None)
    changed = _registry()

    assert changed.activated_ids() == []
    assert "files changed" in {f.plugin_id: f.reason for f in changed.activation_failures()}["helper"]


def test_a_checkout_plugin_shadows_nothing_the_operator_installed(tmp_path: Path) -> None:
    named = tmp_path / "named"
    checkout = tmp_path / "repo" / ".raven" / "plugins"
    _tool_plugin(named, "x")
    _tool_plugin(checkout, "x")

    found = PluginDiscovery(project_dir=checkout, extra_dirs=[named]).discover()
    assert [(d.manifest.id, d.source) for d in found] == [("x", ManifestOrigin.PROJECT)]

    mf = found[0].manifest
    packaged = DiscoveredPlugin(manifest=mf, source=ManifestOrigin.ENTRY_POINTS, location=None)
    cloned = DiscoveredPlugin(
        manifest=mf, source=ManifestOrigin.CHECKOUT, location=checkout / "x" / "raven-plugin.toml"
    )
    assert PluginDiscovery._resolve_conflicts([cloned, packaged]) == [packaged]
