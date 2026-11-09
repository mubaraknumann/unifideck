"""A fake installed Xodus backend for the plugin-side Xbox PC tests.

tests/unit/_xodus_backend_fakes.py

``install_backend(script)`` writes an ``xodus-cli`` that is a small Python
program, lists it in a ``manifest.json``, creates a runtime folder and
writes ``active.json``, exactly as ``runtime.promote`` would. The fake is
then found and verified by the real ``check_backend``, so the tests run
the real ``XodusCli`` against it. Callers must point ``XDG_DATA_HOME`` at a
temporary folder first.

Leading underscore so pytest does not collect it.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import textwrap
from pathlib import Path
from typing import Any

from unifideck.launcher.xbox_pc import backend, layout
from unifideck.launcher.xbox_pc.layout import ActiveComponents

FEATURES = ["cloud-saves-pr28", "xodus-tcp-bridge-v1", "package-locale"]


def add_member(tf: tarfile.TarFile, name: str, data: bytes = b"", *, mode: int = 0o644,
               kind: bytes = tarfile.REGTYPE, link: str = "") -> None:
    """Add one member to an archive being written."""
    info = tarfile.TarInfo(name)
    info.type, info.mode, info.linkname = kind, mode, link
    if kind == tarfile.DIRTYPE:
        info.mode = 0o755
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    tf.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)


def runtime_archive(version: str, *, features: list[str] | None = None, extra: Any = None,
                    requires: str = "0.2.1") -> bytes:
    """A tiny runtime tar.xz with every piece ``validate_runtime`` checks."""
    top = f"unifideck-xodus-runtime-{version}"
    meta = {"schema": 1, "requiresBackendVersion": requires,
            "features": FEATURES if features is None else features}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for d in (top, f"{top}/files", f"{top}/files/bin"):
            add_member(tf, d, kind=tarfile.DIRTYPE)
        add_member(tf, f"{top}/proton", b"#!/bin/sh\n", mode=0o755)
        add_member(tf, f"{top}/version", b"1 xodus\n")
        add_member(tf, f"{top}/toolmanifest.vdf", b'"manifest"\n{\n}\n')
        add_member(tf, f"{top}/XODUS_NEXUS_PATCH.json", json.dumps(meta).encode())
        add_member(tf, f"{top}/files/bin/wine", b"wine", mode=0o755)
        add_member(tf, f"{top}/files/bin/wineserver", b"ws", mode=0o755)
        add_member(tf, f"{top}/files/bin/msidb", kind=tarfile.SYMTYPE, link="wine")
        add_member(tf, f"{top}/files/blob", b"\0" * 300_000)
        if extra is not None:
            extra(tf, top)
    return buf.getvalue()


def make_source(tmp: Path, *, version: str = "0.1.0", archive: bytes | None = None,
                cli_body: bytes = b"cli-binary", bad_sum: bool = False) -> Path:
    """A build repo ``out/`` folder with the real names and the decoys."""
    out = tmp / "out"
    bdir, rdir = out / "backend", out / "runtime"
    bdir.mkdir(parents=True, exist_ok=True)
    rdir.mkdir(parents=True, exist_ok=True)
    bodies = {"xodus-cli": cli_body, "xodus-service": b"service-binary", "LICENSE": b"MIT"}
    for name, body in bodies.items():
        (bdir / name).write_bytes(body)
    files = {n: hashlib.sha256(b).hexdigest() for n, b in bodies.items()}
    manifest = {"schema": 1, "version": "0.1.0", "imported": {"backendVersion": "0.2.8"}, "files": files}
    (bdir / "manifest.json").write_text(json.dumps(manifest))
    (bdir / "unifideck-xodus-backend-0.1.0-linux-x86_64-source.tar.xz").write_bytes(b"decoy")
    for stale in rdir.glob("unifideck-xodus-runtime-*"):
        stale.unlink()
    data = archive if archive is not None else runtime_archive(version)
    name = f"unifideck-xodus-runtime-{version}-x86_64.tar.xz"
    (rdir / name).write_bytes(data)
    digest = "0" * 128 if bad_sum else hashlib.sha512(data).hexdigest()
    (rdir / f"unifideck-xodus-runtime-{version}-x86_64.sha512sum").write_text(f"{digest}  {name}\n")
    (rdir / f"unifideck-xodus-runtime-{version}-source.tar.xz").write_bytes(b"decoy")
    (rdir / f"unifideck-xodus-runtime-{version}-source.sha512sum").write_text(f"{'1' * 128}  x\n")
    return out


def install_backend(script: str, *, name: str = "0.1.0-testtest") -> ActiveComponents:
    """Install a fake backend whose ``xodus-cli`` runs ``script``."""
    root = layout.data_root()
    layout.ensure_private_dir(root)
    bdir = layout.backend_root() / name
    bdir.mkdir(parents=True)
    bodies = {
        "xodus-cli": f"#!{sys.executable}\n" + textwrap.dedent(script),
        "xodus-service": f"#!{sys.executable}\nimport sys\nsys.exit(0)\n",
        "LICENSE": "MIT\n",
    }
    files = {}
    for fname, body in bodies.items():
        (bdir / fname).write_text(body)
        files[fname] = hashlib.sha256(body.encode()).hexdigest()
    for exe in ("xodus-cli", "xodus-service"):
        (bdir / exe).chmod(0o755)
    raw = json.dumps({"schema": 1, "version": "0.1.0", "files": files}).encode()
    (bdir / "manifest.json").write_bytes(raw)
    rdir = layout.runtime_root() / "0.1.0"
    rdir.mkdir(parents=True)
    active = ActiveComponents(
        backend_version="0.1.0", backend_dir=bdir,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        runtime_version="0.1.0", runtime_dir=rdir, archive_sha512="0" * 128,
    )
    layout.write_active(active)
    backend._DIGEST_CACHE.clear()
    return active


def cli_path(active: ActiveComponents) -> Path:
    """The fake ``xodus-cli`` of ``active``."""
    return active.backend_dir / "xodus-cli"
