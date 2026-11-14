#!/usr/bin/env python3
"""scripts/vendor_backend_abis.py: native backend modules for every Decky Python.

Why
---
The plugin backend runs on the Python that Decky Loader bundles, and that
changed under us: Decky up to v3.2 ships 3.11, Decky v3.3 ships 3.14
(measured on two devices, 2026-11-13). ``vendor_deps`` installs wheels for
one Python only, so on Decky 3.3 every compiled module failed to import.
``rpds`` has no pure-Python fallback, which broke ``jsonschema`` and with it
config validation on every boot. The others (aiohttp, yarl, multidict,
frozenlist, propcache, websockets) silently fell back to slow pure Python.

Extension modules carry their ABI in the file name
(``*.cpython-311-*.so``, ``*.cpython-314-*.so``), so both live side by side
in one ``py_modules/``. This script finds each native module installed for
the primary Python, installs the SAME package version for every extra
Python, and copies only that ABI's ``.so`` files next to the primary ones.

Usage
-----
    python3 -I scripts/vendor_backend_abis.py PY_MODULES --primary 3.11 \\
        --extra 3.14 [--platform manylinux2014_x86_64] [--cache-dir DIR]
    python3 -I scripts/vendor_backend_abis.py PY_MODULES --primary 3.11 \\
        --extra 3.14 --check

``--check`` installs nothing and exits 1 when a native module lacks a
sibling for any extra Python. Exit codes: 0 ok, 1 findings, 2 could not run.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: Not handled here: our own code, and cffi's backend, which
#: ``vendor_launcher_cffi`` already ships for every launcher Python
#: (a superset of the Decky ones).
SKIP_TOP_LEVEL = {"unifideck", "_cffi_backend"}


def abi_tag(version: str) -> str:
    major, minor = version.split(".")
    return f"cpython-{major}{minor}"


def native_modules(py_modules: Path, primary: str) -> list[Path]:
    """Every primary-ABI extension module, relative to ``py_modules``."""
    tag = abi_tag(primary)
    found = []
    for so in sorted(py_modules.rglob(f"*.{tag}-*.so")):
        rel = so.relative_to(py_modules)
        top = rel.parts[0].split(".", 1)[0]
        if top not in SKIP_TOP_LEVEL:
            found.append(rel)
    return found


def sibling(rel: Path, version: str, primary: str) -> Path:
    return rel.with_name(rel.name.replace(abi_tag(primary), abi_tag(version), 1))


def owners(py_modules: Path, modules: list[Path]) -> dict[str, list[Path]]:
    """``name==version`` → the modules its dist-info RECORD lists."""
    wanted = {m.as_posix(): m for m in modules}
    out: dict[str, list[Path]] = {}
    for dist in sorted(py_modules.glob("*.dist-info")):
        record = dist / "RECORD"
        if not record.is_file():
            continue
        name, _, version = dist.name.removesuffix(".dist-info").partition("-")
        with record.open(newline="") as f:
            for row in csv.reader(f):
                if row and row[0] in wanted:
                    out.setdefault(f"{name}=={version}", []).append(wanted[row[0]])
    return out


def missing(py_modules: Path, primary: str, extras: list[str]) -> list[str]:
    problems = []
    for rel in native_modules(py_modules, primary):
        for ver in extras:
            if not (py_modules / sibling(rel, ver, primary)).is_file():
                problems.append(f"{rel} has no Python {ver} build")
    return problems


def vendor(args: argparse.Namespace) -> int:
    py_modules = args.py_modules
    modules = native_modules(py_modules, args.primary)
    pins = owners(py_modules, modules)
    unowned = set(modules) - {m for ms in pins.values() for m in ms}
    if unowned:
        print(f"[vendor_backend_abis] no dist-info owns: {sorted(map(str, unowned))}")
        return 1
    for ver in args.extra:
        if all((py_modules / sibling(rel, ver, args.primary)).is_file() for rel in modules):
            print(f"[vendor_backend_abis] Python {ver}: already vendored")
            continue
        if not args.pip_python:
            print(
                f"[vendor_backend_abis] Python {ver} builds are missing and no "
                "Python with pip was found (SteamOS's python3 has none; create "
                "the repo .venv)",
            )
            return 1
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [
                args.pip_python, "-m", "pip", "install", "--quiet",
                "--target", tmp, "--platform", args.platform,
                "--python-version", ver, "--only-binary", ":all:", "--no-deps",
            ]
            if args.cache_dir:
                cmd += ["--cache-dir", str(args.cache_dir)]
            result = subprocess.run([*cmd, *sorted(pins)], check=False)
            if result.returncode != 0:
                print(f"[vendor_backend_abis] pip failed for Python {ver}")
                return 1
            copied = 0
            for rel in modules:
                built = Path(tmp) / sibling(rel, ver, args.primary)
                if built.is_file():
                    dest = py_modules / sibling(rel, ver, args.primary)
                    shutil.copy2(built, dest)
                    copied += 1
            print(f"[vendor_backend_abis] Python {ver}: {copied}/{len(modules)} native modules")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("py_modules", type=Path)
    parser.add_argument("--primary", required=True)
    parser.add_argument("--extra", action="append", default=[])
    parser.add_argument("--platform", default="manylinux2014_x86_64")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument(
        "--pip-python", default=None,
        help="interpreter that has pip (only needed when something is missing)",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.py_modules.is_dir():
        print(f"[vendor_backend_abis] not a directory: {args.py_modules}")
        return 2
    if not args.check:
        rc = vendor(args)
        if rc:
            return rc
    problems = missing(args.py_modules, args.primary, args.extra)
    for p in problems:
        print(f"[vendor_backend_abis] {p}")
    if problems:
        return 1
    print(f"[vendor_backend_abis] OK — every native module has a build for {', '.join(args.extra) or 'no extra Python'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
