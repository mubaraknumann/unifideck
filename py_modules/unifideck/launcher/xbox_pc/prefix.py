"""launcher/xbox_pc/prefix.py — one Wine prefix per Xbox PC game.

The prefix is ``~/.local/share/unifideck/prefixes/<PID>`` with a
``pfx -> .`` self-link, the same layout umu uses (launcher invariant 2).
It is created by the runtime's own ``proton run cmd /c exit``, never by umu
or GE (I-017). A prefix is ready once Proton has put DXVK and vkd3d-proton
into ``system32``.

Proton needs ``STEAM_COMPAT_CLIENT_INSTALL_PATH`` to create a prefix on the
host. This is the one place that sets it (a scoped exception to launcher
invariant 6), from the Steam root the caller resolved. Nothing here creates
``~/.steam``: a real folder there would shadow Steam's own symlink.

Four steps run on every launch:

* :func:`rebase_runtime_symlinks`: Proton links ``system32`` DLLs into the
  runtime that made the prefix. After a runtime update those links point
  at an old release, so they are moved to the current one.
* :func:`install_runtime_components`: the runtime's patched DLLs are copied
  into ``system32`` after a SHA-256 check against its metadata. WinHTTP
  loads ``libnghttp2`` with ``LOAD_LIBRARY_SEARCH_SYSTEM32``, so
  ``WINEDLLPATH`` alone is not enough (Nexus ``proton.ts``).
* :func:`missing_activatable_classes`: Wine registers a DLL's WinRT classes
  only when it creates or upgrades a prefix, so a prefix made by an older
  runtime lacks the classes added since. Each missing one is added with
  ``proton run reg add``, once (I-031).
* :mod:`.prefix_webview2`: the prefix is linked to the runtime's WebView2
  and registered for the version it ships (I-041).

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from .environment import ZERO_STEAM_IDS, clean_env
from .errors import XboxPcLaunchError
from .output import OutputSink
from .prefix_webview2 import link_webview2, reg_file_text, shared_webview2, stale_values
from .runtime import Runtime
from .stop import kill_and_reap, wait_exit

logger = logging.getLogger(__name__)

#: Nexus's readiness check: Proton has installed DXVK and vkd3d-proton.
REQUIRED_PREFIX_FILES = (
    "drive_c/windows/system32/libvkd3d-utils-1.dll",
    "drive_c/windows/system32/dxgi.dll",
    "drive_c/windows/system32/d3d12.dll",
)
PREFIX_TIMEOUT_S = 600.0

#: ``feature -> DLL`` copied into ``system32`` when the runtime declares it.
_COMPONENT_FEATURES = (
    ("wine-winhttp-http2-v1", "libnghttp2.dll"),
    ("winrt-json-v1", "windows.web.dll"),
    ("hlsl-state-keywords-v1", "libvkd3d-shader-1.dll"),
    ("appstate-registration-v1", "twinapi.appcore.dll"),
    ("coremessaging-dispatcherqueue-v1", "coremessaging.dll"),
)

#: ``feature -> (WinRT class, DLL)`` the prefix must register when the runtime declares it.
_ACTIVATABLE_CLASSES = (
    ("coremessaging-dispatcherqueue-v1", "Windows.System.DispatcherQueue", "coremessaging.dll"),
)
_CLASS_KEY = ("Software", "Microsoft", "WindowsRuntime", "ActivatableClassId")


def missing_prefix_files(prefix: Path) -> list[str]:
    """The readiness files ``prefix`` lacks."""
    return [rel for rel in REQUIRED_PREFIX_FILES if not os.access(prefix / rel, os.R_OK)]


def _rebased_target(target: Path, releases: Path, current: Path) -> Path | None:
    """Where an absolute link into ``releases/<old>/files/...`` should point now."""
    try:
        parts = target.relative_to(releases).parts
    except ValueError:
        return None
    if len(parts) < 3 or parts[1] != "files":
        return None
    replacement = current.joinpath(*parts[2:])
    if replacement == target or not os.access(replacement, os.R_OK):
        return None
    return replacement


def rebase_runtime_symlinks(prefix: Path, runtime_root: Path) -> int:
    """Point links that target an older runtime release at this one. Returns the count."""
    releases = runtime_root.parent
    current = runtime_root / "files"
    updated = 0
    for dirpath, _dirs, names in os.walk(prefix):
        for name in names:
            entry = Path(dirpath) / name
            if not entry.is_symlink():
                continue
            target = Path(os.readlink(entry))
            replacement = _rebased_target(target, releases, current) if target.is_absolute() else None
            if replacement is None:
                continue
            temporary = entry.with_name(f"{name}.unifideck-runtime-{os.getpid()}-{updated}")
            temporary.symlink_to(replacement)
            temporary.replace(entry)
            updated += 1
    return updated


def _verified_components(runtime: Runtime) -> list[tuple[str, bytes]]:
    """Every wanted DLL with its bytes, checked. Raises ``runtime_missing``."""
    verified: list[tuple[str, bytes]] = []
    for feature, name in _COMPONENT_FEATURES:
        if feature not in runtime.features:
            continue
        library = "vkd3d" if name == "libvkd3d-shader-1.dll" else "wine"
        try:
            data = (runtime.files / "lib" / library / "x86_64-windows" / name).read_bytes()
        except OSError as err:
            raise XboxPcLaunchError("runtime_missing", f"{name}: {err}") from err
        if runtime.components.get(name) != hashlib.sha256(data).hexdigest():
            raise XboxPcLaunchError("runtime_missing", f"{name} checksum mismatch")
        verified.append((name, data))
    return verified


def install_runtime_components(runtime: Runtime, prefix: Path) -> None:
    """Copy the runtime's patched DLLs into ``system32``. Verifies all first."""
    verified = _verified_components(runtime)
    system32 = prefix / "drive_c/windows/system32"
    for name, data in verified:
        target = system32 / name
        try:
            if target.read_bytes() == data:
                continue
        except OSError:
            pass
        temporary = target.with_name(f"{name}.unifideck-{os.getpid()}")
        temporary.write_bytes(data)
        os.chmod(temporary, 0o644)
        temporary.replace(target)


def missing_activatable_classes(runtime: Runtime, prefix: Path) -> list[tuple[str, str]]:
    """``(class, DLL)`` for each wanted WinRT class ``system.reg`` does not register."""
    wanted = [(name, dll) for feature, name, dll in _ACTIVATABLE_CLASSES if feature in runtime.features]
    if not wanted:
        return []
    try:
        registry = (prefix / "system.reg").read_text(errors="replace")
    except OSError:
        registry = ""
    # system.reg doubles each backslash in a key name.
    return [(name, dll) for name, dll in wanted if "[" + "\\\\".join((*_CLASS_KEY, name)) + "]" not in registry]


async def _register_activatable_classes(runtime: Runtime, compat_root: Path, env: Mapping[str, str],
                                        timeout_s: float) -> None:
    """Add each missing WinRT class to the prefix. Raises ``prefix_failed``."""
    for name, dll in await asyncio.to_thread(missing_activatable_classes, runtime, compat_root):
        logger.info("[xbox_pc.prefix] registering %s in %s", name, compat_root)
        key = "HKLM\\" + "\\".join((*_CLASS_KEY, name))
        rc, tail = await run_proton(
            runtime, env, timeout_s,
            "reg", "add", key, "/v", "DllPath", "/t", "REG_SZ", "/d", f"C:\\windows\\system32\\{dll}", "/f",
        )
        if rc != 0:
            raise XboxPcLaunchError("prefix_failed", f"could not register {name}: reg exited {rc}: {tail[-600:]}")


async def _install_webview2(runtime: Runtime, compat_root: Path, env: Mapping[str, str], timeout_s: float) -> None:
    """Link the runtime's WebView2 into the prefix and register it. Raises ``prefix_failed``."""
    shared = await asyncio.to_thread(shared_webview2, runtime)
    if shared is None:
        return
    version, folder = shared
    if not await asyncio.to_thread(link_webview2, compat_root, folder):
        return
    stale = await asyncio.to_thread(stale_values, compat_root, version)
    if not stale:
        return
    logger.info("[xbox_pc.prefix] registering WebView2 %s in %s", version, compat_root)
    reg = compat_root / "drive_c/windows/temp/unifideck-webview2.reg"
    await asyncio.to_thread(reg.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(reg.write_text, reg_file_text(stale), encoding="utf-16")
    try:
        rc, tail = await run_proton(
            runtime, env, timeout_s, "regedit", "/S", "C:\\windows\\temp\\unifideck-webview2.reg",
        )
    finally:
        await asyncio.to_thread(reg.unlink, missing_ok=True)
    if rc != 0:
        raise XboxPcLaunchError("prefix_failed", f"could not register WebView2 {version}: regedit exited {rc}: {tail[-600:]}")


def _ensure_layout(compat_root: Path) -> None:
    """Create the prefix root and its ``pfx -> .`` link."""
    compat_root.mkdir(parents=True, exist_ok=True)
    pfx = compat_root / "pfx"
    if not pfx.exists() and not pfx.is_symlink():
        pfx.symlink_to(".")


def proton_env(base: Mapping[str, str], compat_root: Path, steam_root: Path) -> dict[str, str]:
    """The env of the prefix-creating ``proton run``: no game identity at all."""
    env = clean_env(base)
    env.update(ZERO_STEAM_IDS)
    env.update({
        "STEAM_COMPAT_DATA_PATH": str(compat_root),
        "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(steam_root),
        "UMU_ID": "unifideck-xbox",
        "UMU_USE_STEAM": "0",
    })
    return env


async def _spawn_proton(runtime: Runtime, env: Mapping[str, str], args: tuple[str, ...]) -> asyncio.subprocess.Process:
    """Start ``proton run <args>`` in its own session. Raises ``prefix_failed``."""
    try:
        return await asyncio.create_subprocess_exec(
            str(runtime.root / "proton"), "run", *args,
            env=dict(env), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as err:
        raise XboxPcLaunchError("prefix_failed", f"proton did not start: {err}") from err


async def run_proton(
    runtime: Runtime, env: Mapping[str, str], timeout_s: float, *args: str,
) -> tuple[int | None, str]:
    """Run ``proton run <args>``, by default ``cmd /c exit``. ``(rc or None on timeout, output tail)``."""
    sink = OutputSink(None, tail_lines=20)
    proc = await _spawn_proton(runtime, env, args or ("cmd", "/c", "exit"))
    pump = asyncio.ensure_future(sink.pump(proc.stdout, source="proton"))
    try:
        exited = await wait_exit(proc, timeout_s)
        if not exited:
            await kill_and_reap(proc)
        await asyncio.wait({pump}, timeout=5.0)
        return (proc.returncode if exited else None), sink.tail()
    except asyncio.CancelledError:
        await kill_and_reap(proc)
        raise
    finally:
        if not pump.done():
            pump.cancel()


async def prepare_prefix(
    runtime: Runtime,
    compat_root: Path,
    steam_root: Path,
    *,
    base_env: Mapping[str, str],
    on_create: Callable[[], Awaitable[None]] | None = None,
    timeout_s: float = PREFIX_TIMEOUT_S,
) -> bool:
    """Make ``compat_root`` a ready prefix for ``runtime``. True when it was created.

    Raises ``prefix_failed`` when Proton does not produce a ready prefix.
    Cancelling kills Proton's process group.
    """
    await asyncio.to_thread(_ensure_layout, compat_root)
    await asyncio.to_thread(rebase_runtime_symlinks, compat_root, runtime.root)
    created = False
    env = proton_env(base_env, compat_root, steam_root)
    if await asyncio.to_thread(missing_prefix_files, compat_root):
        logger.info("[xbox_pc.prefix] creating %s with the runtime's proton", compat_root)
        if on_create is not None:
            await on_create()
        rc, tail = await run_proton(runtime, env, timeout_s)
        missing = await asyncio.to_thread(missing_prefix_files, compat_root)
        if missing:
            raise XboxPcLaunchError(
                "prefix_failed",
                f"proton exited {rc}; missing {', '.join(missing)}: {tail[-600:]}",
            )
        created = True
        await asyncio.to_thread(rebase_runtime_symlinks, compat_root, runtime.root)
    await asyncio.to_thread(install_runtime_components, runtime, compat_root)
    await _register_activatable_classes(runtime, compat_root, env, timeout_s)
    await _install_webview2(runtime, compat_root, env, timeout_s)
    return created
