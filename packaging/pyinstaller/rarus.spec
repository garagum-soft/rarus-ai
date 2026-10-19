# PyInstaller spec for Rarus AI: one folder (onedir), one program that is both the window and,
# with ``--engine``, the engine (rarus_app/__main__.py). Run from the repository root by
# packaging/scripts/build.ps1, inside the build venv that has the engine wheel, the shell and
# PyInstaller installed -- the wheel, not the checkout, so the page (raven/ui/dist), the templates
# and the data files the hatch hook maps in are the ones that ship.
import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]

datas, binaries, hiddenimports = [], [], []

# The engine imports most of its own modules by name at run time (plugins, RPC methods, channels),
# so every submodule rides along, with the data files the wheel carries beside them.
hiddenimports += collect_submodules("raven")
datas += collect_data_files("raven")
# The agents' own launchers and plugins (agents/*/run.py, install.py, plugins/**) and the scaffold a
# hired employee is made from are scripts the engine runs from disk, not modules it imports, so they
# ship as files: without them no agent could start in the frozen program.
for tree in ("agents", "templates"):
    datas += collect_data_files("raven", include_py_files=True, subdir=tree)
hiddenimports += collect_submodules("rarus_app")
datas += collect_data_files("rarus_app")

# The whole standard library, not only what the engine itself imports: the components, the agents and
# the scripts a person runs import whatever they need of it (the deck engine wanted ``filecmp``), and the
# frozen program is the only Python they have. Test suites and GUI toolkits stay out.
_STDLIB_OUT = ("test", "idlelib", "tkinter", "turtle", "turtledemo", "lib2to3", "ensurepip", "venv", "pydoc_data")
for _name in sorted(sys.stdlib_module_names):
    if _name.startswith("_") or _name in _STDLIB_OUT or _name in ("this", "antigravity"):
        continue
    _spec = importlib.util.find_spec(_name)
    if _spec is None:  # POSIX-only modules
        continue
    hiddenimports.append(_name)
    if _spec.submodule_search_locations:
        hiddenimports += collect_submodules(_name, filter=lambda n: ".test" not in n and "idle_test" not in n)

# Libraries that find their own data or native pieces at run time.
for package in ("litellm", "tiktoken", "tiktoken_ext", "lancedb", "onnxruntime", "certifi", "mcp"):
    try:
        d, b, h = collect_all(package)
    except Exception:  # a package that is not installed is not needed by this build
        continue
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(ROOT / "packaging" / "pyinstaller" / "entry.py")],
    pathex=[str(ROOT / "app")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "ruff", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RarusAI",
    # The dark yellow globe (tools/dev/make_icons.py): the exe, its window, the taskbar and the shortcuts.
    icon=str(ROOT / "packaging" / "assets" / "rarus.ico"),
    console=False,
    disable_windowed_traceback=False,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="RarusAI")
