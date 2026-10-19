"""Build the components Rarus AI downloads after it is installed, and the catalogue it ships with.

    build\\venv\\Scripts\\python.exe packaging\\components\\build.py --dist build\\dist\\RarusAI [--only browser]

Run after PyInstaller (packaging/scripts/build.ps1 step 5), inside the build venv. For every entry
of packaging/components/catalog.json:

1. ``uv pip install --target`` its packages, pinned to the versions the program was built with
   (the build venv's own freeze), the engine resolved from the wheel this build made;
2. drop every distribution the program already carries: what is left is only what the program
   lacks, so a component never brings a second, different copy of anything;
3. fetch its data (the word dictionary, NLTK corpora, the deepdoc models, Chromium);
4. pack the folder into a reproducible zip named by its own SHA-256, so an unchanged component
   keeps its name from build to build and is never downloaded twice;
5. unpack it into a scratch home and run its ``check`` inside the frozen program itself.

The catalogue (``components.json``: ids, groups, names, sizes, SHA-256) is written into the
frozen program (``_internal/rarus_app``) and next to the zips in ``build/components/out``, which
is what goes onto the server under ``/api/mirror/rarus/components/``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Path(__file__).with_name("catalog.json")
GROUPS = ("hidden", "locked", "optional", "off")
# Fixed so the same files always make the same zip (and the same SHA-256).
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def log(text: str) -> None:
    print(text, flush=True)


def run(cmd: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None) -> None:
    log("  $ " + " ".join(str(c) for c in cmd))
    done = subprocess.run([str(c) for c in cmd], env=env, cwd=cwd)
    if done.returncode != 0:
        raise SystemExit(f"failed ({done.returncode}): {cmd[0]}")


# -- what the program already carries ------------------------------------------------------------


class Bundle:
    """The modules and files of the frozen program, to tell what a component must still bring."""

    def __init__(self, dist: Path, work: Path) -> None:
        from PyInstaller.archive.readers import ZlibArchiveReader

        pyz = sorted(work.glob("PYZ-*.pyz"))
        if not pyz:
            raise SystemExit(f"no PYZ archive under {work}: run PyInstaller first")
        self.modules = set(ZlibArchiveReader(str(pyz[0])).toc)
        internal = dist / "_internal"
        self.files = {p.relative_to(internal).as_posix().lower() for p in internal.rglob("*") if p.is_file()}

    def carries(self, record: list[str]) -> bool:
        """Whether any module or extension of a distribution (its RECORD paths) is in the program."""
        for path in record:
            if path.endswith(".py"):
                name = path[:-3].replace("/", ".")
                if name.endswith(".__init__"):
                    name = name[: -len(".__init__")]
                if name in self.modules:
                    return True
            elif path.endswith(".pyd") and path.lower() in self.files:
                return True
        return False


def installed_distributions(site: Path) -> list[tuple[str, Path, list[str]]]:
    """``(name, dist-info folder, RECORD paths)`` for every distribution in a target folder."""
    found = []
    for info in sorted(site.glob("*.dist-info")):
        record = info / "RECORD"
        paths = []
        if record.is_file():
            for line in record.read_text(encoding="utf-8").splitlines():
                path = line.split(",")[0].strip()
                if path:
                    paths.append(path.replace("\\", "/"))
        meta = info / "METADATA"
        name = info.name.split("-")[0]
        if meta.is_file():
            for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Name:"):
                    name = line.split(":", 1)[1].strip()
                    break
        found.append((name, info, paths))
    return found


def drop_distribution(site: Path, info: Path, paths: list[str]) -> None:
    for path in paths:
        target = (site / path).resolve()
        if site.resolve() in target.parents and target.is_file():
            target.unlink()
    shutil.rmtree(info, ignore_errors=True)
    # Folders the removal emptied, deepest first.
    for folder in sorted({(site / p).parent for p in paths}, key=lambda p: len(p.parts), reverse=True):
        try:
            if folder.resolve() != site.resolve() and folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass


# -- building one component ----------------------------------------------------------------------


def constraints(python: Path, out: Path, engine_version: str, locked: Path | None = None) -> Path:
    """The versions a component may resolve to: the lock's (what the program was built with), else the venv's."""
    if locked is not None:
        lines = locked.read_text(encoding="utf-8").splitlines()
    else:
        lines = subprocess.run(
            ["uv", "pip", "freeze", "--python", str(python)], capture_output=True, text=True, check=True
        ).stdout.splitlines()
    pins = [
        line for line in lines
        if line and not line.lstrip().startswith("#") and " @ " not in line and not line.startswith(("raven", "rarus-app"))
    ]
    pins.append(f"raven=={engine_version}")
    out.write_text("\n".join(pins) + "\n", encoding="utf-8")
    return out


# Packages that look for their data next to the program when they see a PyInstaller build. Inside a
# component they are not part of the program -- they sit in its own folder -- so that branch must not
# run. Each old text must still be found, or the build stops: a new upstream release is re-checked.
FIXUPS = {
    "cairosvg/__init__.py": [("if hasattr(sys, 'frozen'):", "if False:  # Rarus AI component: data beside this file")],
}


def apply_fixups(site: Path) -> None:
    for rel, edits in FIXUPS.items():
        path = site / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for old, new in edits:
            if old not in text:
                raise SystemExit(f"{rel}: the text to fix is gone ({old!r}); look at the new release")
            text = text.replace(old, new, 1)
        path.write_text(text, encoding="utf-8")
        log(f"  fixed {rel}")


def spec_of(package: str) -> str:
    """A catalogue package: a path under the repository (``engine/plugins-dist/x[extra]``) or a requirement."""
    name, bracket, extra = package.partition("[")
    if name.startswith("engine/"):
        return str(ROOT / name) + (bracket + extra if bracket else "")
    return package


def install_packages(entry: dict, site: Path, *, python: Path, wheels: Path, pins: Path, bundle: Bundle) -> list[str]:
    if site.exists():
        shutil.rmtree(site)
    packages = entry.get("packages") or []
    if not packages:
        return []
    site.mkdir(parents=True)
    run(
        [
            "uv", "pip", "install", "--quiet", "--target", site, "--python", python,
            "--find-links", wheels, "--constraint", pins, "--link-mode", "copy",
            *[spec_of(p) for p in packages],
        ]
    )
    # Distributions the program carries only in part (PyInstaller takes the modules the engine imports)
    # but the component needs more of: kept whole, and the component's copy -- the same version -- wins.
    whole = {w.lower() for w in entry.get("whole") or []}
    kept, dropped = [], []
    for name, info, paths in installed_distributions(site):
        if name.lower() in whole:
            kept.append(name)
            continue
        if name.lower() in ("raven", "rarus-app") or bundle.carries(paths):
            drop_distribution(site, info, paths)
            dropped.append(name)
        else:
            kept.append(name)
    # Console-script launchers point at an interpreter the program does not have.
    for scripts in ("bin", "Scripts"):
        shutil.rmtree(site / scripts, ignore_errors=True)
    apply_fixups(site)
    log(f"  kept {len(kept)}: {', '.join(sorted(kept))}")
    log(f"  already in the program, dropped {len(dropped)}")
    return sorted(kept)


def fetch(entry: dict, stage: Path, *, python: Path, cache: Path) -> None:
    site = stage / "site"
    env = {**os.environ, "PYTHONUTF8": "1"}
    pythonpath = [str(site)] if site.is_dir() else []
    for kind in entry.get("fetch") or []:
        log(f"  fetch {kind}")
        if kind in ("dictionary", "nltk"):
            env_k = {**env, "RAVEN_CORE_RES": str(stage / "core-res"), "PYTHONPATH": os.pathsep.join(pythonpath)}
            call = "resources.fetch_dictionary(resources.DICTIONARY, force=False)" if kind == "dictionary" else (
                "resources.fetch_nltk(force=False)"
            )
            run([python, "-c", f"from raven import resources; {call}"], env=env_k)
        elif kind == "deepdoc":
            tools = cache / "tools"
            if not (tools / "huggingface_hub").is_dir():
                run(["uv", "pip", "install", "--quiet", "--target", tools, "--python", python, "huggingface-hub>=1.11"])
            env_k = {
                **env,
                "RAVEN_KNOWLEDGE_RES": str(stage / "knowledge-res"),
                "HF_HOME": str(cache / "hf"),
                "PYTHONPATH": os.pathsep.join([*pythonpath, str(tools)]),
            }
            run([python, "-c", "from raven import resources; resources.fetch_deepdoc(resources.DEEPDOC, force=False)"], env=env_k)
        elif kind == "chromium":
            browsers = stage / "browsers"
            env_k = {**env, "PLAYWRIGHT_BROWSERS_PATH": str(browsers), "PYTHONPATH": os.pathsep.join(pythonpath)}
            # The full Chromium only: with channel "chromium" it also runs headless, so the separate
            # headless shell (another ~100 MB) is not shipped (raven/browser/driver.py).
            run([python, "-m", "playwright", "install", "--no-shell", "chromium"], env=env_k)
            for leftover in browsers.glob(".links"):
                shutil.rmtree(leftover, ignore_errors=True)
        else:
            raise SystemExit(f"unknown fetch kind {kind!r} in {entry['id']}")


def pack(stage: Path, target_dir: Path, cid: str) -> dict:
    """A reproducible zip of the stage, named by its SHA-256; answers the catalogue's file fields."""
    target_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in stage.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    unpacked = 0
    with tempfile.NamedTemporaryFile(dir=target_dir, suffix=".zip", delete=False) as handle:
        temp = Path(handle.name)
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(stage).as_posix(), ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            data = path.read_bytes()
            unpacked += len(data)
            archive.writestr(info, data, compresslevel=6)
    digest = hashlib.sha256(temp.read_bytes()).hexdigest()
    name = f"{cid}-{digest[:12]}.zip"
    final = target_dir / name
    if final.exists():
        temp.unlink()
    else:
        temp.replace(final)
    return {"file": name, "sha256": digest, "size": final.stat().st_size, "unpacked": unpacked, "count": len(files)}


# Helpers every check may call. ``walk`` imports each module of a package, so a module of a library the
# program carries only in part shows up here rather than on someone's computer; ``everos_up`` starts the
# memory server the way the engine does (``RarusAI.exe --everos``) and waits for its health answer.
CHECK_PRELUDE = r"""
def walk(name, skip=()):
    import importlib, pkgutil
    module = importlib.import_module(name)
    bad = []
    for info in pkgutil.walk_packages(getattr(module, '__path__', []), name + '.'):
        parts = info.name.split('.')
        if any(p in ('tests', 'test', 'testing', '__main__') for p in parts) or info.name.startswith(tuple(skip)):
            continue
        try:
            importlib.import_module(info.name)
        except ImportError as exc:
            bad.append(info.name + ': ' + str(exc))
    assert not bad, bad


def everos_up():
    import os, socket, subprocess, sys, tempfile, time, urllib.request
    from raven_everos.config import configure_everos_env, ensure_everos_home, set_everos_api
    root = tempfile.mkdtemp(prefix='everos-check-')
    configure_everos_env(root)
    ensure_everos_home(root)
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    set_everos_api(host='127.0.0.1', port=port)
    # The address and the model travel the way the engine sends the roles (EVEROS_<SECTION>__<KEY>).
    env = dict(os.environ, EVEROS_API__HOST='127.0.0.1', EVEROS_API__PORT=str(port), EVEROS_LLM__MODEL='check',
               EVEROS_LLM__BASE_URL='http://127.0.0.1:9/v1', EVEROS_LLM__API_KEY='placeholder-not-a-key',
               PYTHONIOENCODING='utf-8')
    log = open(os.path.join(root, 'server.log'), 'w')
    server = subprocess.Popen([sys.executable, '--everos', 'server', 'start', '--root', root], stdout=log, stderr=log, env=env)
    try:
        for _ in range(120):
            try:
                if urllib.request.urlopen('http://127.0.0.1:%d/health' % port, timeout=2).status == 200:
                    return
            except OSError:
                pass
            if server.poll() is not None:
                break
            time.sleep(1)
        log.close()
        tail = open(os.path.join(root, 'server.log'), encoding='utf-8', errors='replace').read()[-3000:]
        raise AssertionError('the memory server did not answer: ' + tail)
    finally:
        server.kill()


"""


def smoke(entries: list[dict], dist: Path, zips: Path, scratch: Path) -> None:
    """Unpack every built component into a scratch home and run each ``check`` in the frozen program."""
    if scratch.exists():
        shutil.rmtree(scratch)
    root = scratch / "components"
    state: dict = {"components": {}}
    for entry in entries:
        folder = root / entry["id"] / entry["sha256"][:12]
        with zipfile.ZipFile(zips / entry["file"]) as archive:
            archive.extractall(folder)
        state["components"][entry["id"]] = {"dir": f"{entry['id']}/{entry['sha256'][:12]}", "enabled": True}
    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
    exe = dist / "RarusAI.exe"
    failed = []
    for entry in entries:
        code = entry.get("check")
        if not code:
            continue
        out = scratch / f"check-{entry['id']}.txt"
        wrapped = (
            CHECK_PRELUDE +
            "import traceback\n"
            f"try:\n" + "".join(f"    {line}\n" for line in code.splitlines()) +
            f"    open({str(out)!r}, 'w', encoding='utf-8').write('ok')\n"
            "except BaseException:\n"
            f"    open({str(out)!r}, 'w', encoding='utf-8').write(traceback.format_exc())\n"
        )
        env = {**os.environ, "RARUS_HOME": str(scratch)}
        subprocess.run([str(exe), "-c", wrapped], env=env, timeout=600)
        said = out.read_text(encoding="utf-8") if out.is_file() else "the check wrote nothing"
        log(f"  check {entry['id']}: {'ok' if said == 'ok' else 'FAILED'}")
        if said != "ok":
            log(said)
            failed.append(entry["id"])
    if failed:
        raise SystemExit(f"component checks failed: {', '.join(failed)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dist", type=Path, default=ROOT / "build" / "dist" / "RarusAI")
    parser.add_argument("--work", type=Path, default=ROOT / "build" / "work" / "rarus")
    parser.add_argument("--wheels", type=Path, default=ROOT / "build" / "wheels")
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "components")
    parser.add_argument("--constraints", type=Path, help="the lock's versions (build.ps1 exports them from engine/uv.lock)")
    parser.add_argument("--only", action="append", default=[], help="rebuild just this id (repeatable); others keep their last build")
    parser.add_argument("--no-check", action="store_true", help="skip running the checks in the frozen program")
    args = parser.parse_args(argv)

    python = Path(sys.executable)
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    out, stages, zips, cache = args.out, args.out / "stage", args.out / "out", args.out / "cache"
    for folder in (stages, zips, cache):
        folder.mkdir(parents=True, exist_ok=True)
    wheel = sorted(args.wheels.glob("raven-*.whl"))
    if not wheel:
        raise SystemExit(f"no engine wheel in {args.wheels}")
    engine_version = wheel[-1].name.split("-")[1]
    pins = constraints(python, cache / "constraints.txt", engine_version, args.constraints)
    bundle = Bundle(args.dist, args.work)

    previous = {}
    last = zips / "components.json"
    if last.is_file():
        previous = {c["id"]: c for c in json.loads(last.read_text(encoding="utf-8")).get("components", [])}

    built = []
    for entry in catalog["components"]:
        cid = entry["id"]
        if entry.get("setup") not in GROUPS:
            raise SystemExit(f"{cid}: setup must be one of {GROUPS}")
        if args.only and cid not in args.only and cid in previous:
            built.append(previous[cid])
            log(f"[{cid}] kept from the last build: {previous[cid]['file']}")
            continue
        log(f"[{cid}] building")
        stage = stages / cid
        stage.mkdir(parents=True, exist_ok=True)
        kept = install_packages(entry, stage / "site", python=python, wheels=args.wheels, pins=pins, bundle=bundle)
        fetch(entry, stage, python=python, cache=cache)
        mark = {"id": cid, "env": entry.get("env") or {}, "packages": kept}
        (stage / "component.json").write_text(json.dumps(mark, ensure_ascii=False, indent=1), encoding="utf-8")
        fields = pack(stage, zips, cid)
        log(f"  {fields['file']}: {fields['size'] / 1e6:.1f} MB packed, {fields['unpacked'] / 1e6:.1f} MB unpacked")
        shipped = {k: v for k, v in entry.items() if k not in ("packages", "fetch", "_comment")}
        built.append({**shipped, **fields})

    if not args.no_check:
        log("checks in the frozen program")
        smoke(built, args.dist, zips, out / "smoke-home")

    shipped_catalog = {"schema": 1, "base_url": catalog["base_url"], "components": built}
    text = json.dumps(shipped_catalog, ensure_ascii=False, indent=1)
    last.write_text(text, encoding="utf-8")
    target = args.dist / "_internal" / "rarus_app" / "components.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    total = sum(c["size"] for c in built)
    log(f"catalogue: {len(built)} components, {total / 1e6:.0f} MB to host; written to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
