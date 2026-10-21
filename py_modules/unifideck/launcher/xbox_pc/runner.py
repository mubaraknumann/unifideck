"""launcher/xbox_pc/runner.py — the ``<wine>`` command ``xodus-cli run`` executes.

``xodus-cli run DIR RUNNER --exe EXE`` runs ``RUNNER <view>/<exe>`` with the
decrypted images handed down as inherited memfds. The runner is a small
shell script in the prefix (``unifideck-xbox-wine``, a port of Nexus's
``xodus-proton-wine``). It sets the prefix env and runs the runtime's own
``wine`` directly.

It cannot go through umu: umu starts Proton with ``close_fds=True`` and the
memfds are lost (I-017). That is also why this launch never uses the GE or
Proton chain or ``setup_prefix``.

After the game exits the runner waits for the prefix's ``wineserver``, so a
launcher exe that quits before its game keeps ``xodus-cli`` and its launch
view alive until the game ends. Two extra modes:

* :data:`STOP_FLAG` runs ``wineserver -k`` (the forced stop).
* :data:`NO_WAIT_FLAG` runs a helper such as ``taskkill`` without waiting
  for the game's ``wineserver``.

A 32-bit game runs with ``WINEARCH=wow64`` (I-038). The runtime ships
``i386-unix`` libraries, so under ``win64`` a 32-bit game gets the old WoW64:
its Unix side (the Vulkan driver, gamescope's WSI layer, winex11) is 32-bit
too and shares the game's 4 GB address space. A 32-bit ``strtol`` turns a
shortcut app id above 2147483647 into 2147483647, so in Gaming Mode
gamescope gave Alan Wake's American Nightmare's window to another app and
the game stayed behind the loading screen. With ``wow64`` the Unix side is
64-bit, and its memory stays out of the game's address space. The runtime's own ``proton`` script does the same
for ``PROTON_USE_WOW64=1``.

NVAPI is disabled unless the host runs an NVIDIA driver (I-035). The prefix
is made by the runtime's ``proton`` script, which installs dxvk-nvapi, but
the game runs without that script. On a Windows PC with an AMD GPU there is
no ``nvapi.dll``, and games expect that. Alan Wake's American Nightmare asks
NVAPI for the SLI state every frame. With no NVIDIA GPU each call opened a
new DXGI factory, so a new Vulkan instance: the game ran at about 17 frames
a second and then crashed. Proton disables NVAPI for that game by Steam app
id on the same ``/proc/modules`` check.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import os
from pathlib import Path

from .runtime import Runtime

RUNNER_NAME = "unifideck-xbox-wine"
STOP_FLAG = "--unifideck-stop-prefix"
NO_WAIT_FLAG = "--unifideck-no-wait"

_HTTP2_FEATURE = "wine-winhttp-http2-v1"
_HOST_LIBS_FEATURE = "wine-host-libraries-v1"
_NATIVE_OVERRIDES = "dxgi=n;d3d11=n;d3d10core=n;d3d9=n;d3d12=n;d3d12core=n"
_HTTP2_OVERRIDES = ";winhttp=b;libnghttp2=n"
_NO_NVAPI_OVERRIDES = ";nvapi=d;nvapi64=d;nvofapi64=d"
_NVIDIA_MODULES = frozenset({"nvidia", "nouveau", "nova"})
_LIB_ARCHES = ("x86_64-linux-gnu", "aarch64-linux-gnu", "i386-linux-gnu")


def shell_quote(value: str | Path) -> str:
    """``value`` as one single-quoted POSIX shell word."""
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def nvidia_driver_loaded(modules: Path = Path("/proc/modules")) -> bool:
    """Whether the kernel runs an NVIDIA driver; True when it cannot tell.

    The same check as the runtime's ``proton`` script. An unreadable
    ``modules`` keeps NVAPI, the script's default.
    """
    try:
        text = modules.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return any(line.partition(" ")[0] in _NVIDIA_MODULES for line in text.splitlines())


def _exports(
    runtime: Runtime, compat_root: Path, host_libraries: bool, wow64: bool, nvapi: bool,
) -> list[str]:
    """The ``export`` lines of the prefix env.

    ``WINEDLLOVERRIDES`` and ``LD_LIBRARY_PATH`` keep what the runner
    inherits, which is the user's own launch option: ``clean_env`` removes
    Steam's ``LD_LIBRARY_PATH`` before ``xodus-cli`` starts. The user's DLL
    overrides come after ours, so Wine applies theirs. Their library folders
    come first, so they are searched first. The GStreamer paths stay ours
    alone: host plugins break the runtime's media stack.
    """
    files = runtime.files
    lib_dirs = [str(files / "lib" / d) for d in _LIB_ARCHES]
    ld_path = ":".join((["/usr/lib"] if host_libraries else []) + lib_dirs)
    gst = (
        "/usr/lib/gstreamer-1.0" if host_libraries
        else ":".join(d + "/gstreamer-1.0" for d in lib_dirs)
    )
    http2 = _HTTP2_FEATURE in runtime.features
    overrides = (
        _NATIVE_OVERRIDES
        + (_HTTP2_OVERRIDES if http2 else "")
        + ("" if nvapi else _NO_NVAPI_OVERRIDES)
    )
    q = shell_quote
    return [
        f"export WINEPREFIX={q(compat_root)}",
        f"export WINEARCH={q('wow64' if wow64 else 'win64')}",
        f"export WINEDLLPATH={q(str(files / 'lib/vkd3d') + ':' + str(files / 'lib/wine'))}",
        f'export LD_LIBRARY_PATH="${{LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}}"{q(ld_path)}',
        f"export ESPEAK_DATA_PATH={q(files / 'share')}",
        f"export GST_PLUGIN_SYSTEM_PATH_1_0={q(gst)}",
        "export GST_PLUGIN_PATH_1_0=''",
        f"export WINE_GST_REGISTRY_DIR={q(compat_root / 'gstreamer-1.0')}",
        f"export MEDIACONV_BLANK_VIDEO_FILE={q(files / 'share/media/blank.mkv')}",
        f"export MEDIACONV_BLANK_AUDIO_FILE={q(files / 'share/media/blank.ptna')}",
        f"export PATH={q(files / 'bin')}:\"$PATH\"",
        f'export WINEDLLOVERRIDES={q(overrides)}"${{WINEDLLOVERRIDES:+;$WINEDLLOVERRIDES}}"',
        "export WINE_LARGE_ADDRESS_AWARE='1'",
    ]


def _modes(runtime: Runtime) -> list[str]:
    """The stop, no-wait and normal run modes."""
    q = shell_quote
    return [
        f'if [ "${{1:-}}" = {q(STOP_FLAG)} ]; then',
        f"  exec {q(runtime.wineserver)} -k",
        "fi",
        f'if [ "${{1:-}}" = {q(NO_WAIT_FLAG)} ]; then',
        "  shift",
        f'  exec {q(runtime.wine)} "$@"',
        "fi",
        "launch_status=0",
        f'{q(runtime.wine)} "$@" || launch_status=$?',
        "wait_status=0",
        f"{q(runtime.wineserver)} -w || wait_status=$?",
        'if [ "$launch_status" -ne 0 ]; then exit "$launch_status"; fi',
        'exit "$wait_status"',
        "",
    ]


def write_runner(
    runtime: Runtime,
    compat_root: Path,
    *,
    eac_runtime: Path | None = None,
    host_libraries: bool | None = None,
    wow64: bool = False,
    nvapi: bool | None = None,
) -> Path:
    """Write ``<compat_root>/unifideck-xbox-wine`` (0700) atomically and return it.

    ``eac_runtime`` is Steam's Proton EasyAntiCheat Runtime folder, when
    installed. A ``PROTON_EAC_RUNTIME`` the user set wins over it.
    ``wow64`` is for a 32-bit game and ``nvapi`` keeps NVAPI; None checks
    the host for an NVIDIA driver (see the module docstring for both). A
    user's ``nvapi=n`` launch option still wins.
    """
    if host_libraries is None:
        host_libraries = _HOST_LIBS_FEATURE in runtime.features
    if nvapi is None:
        nvapi = nvidia_driver_loaded()
    lines = ["#!/bin/sh", *_exports(runtime, compat_root, host_libraries, wow64, nvapi)]
    if eac_runtime is not None:
        lines += [
            'if [ -z "${PROTON_EAC_RUNTIME:-}" ]; then',
            f"  export PROTON_EAC_RUNTIME={shell_quote(eac_runtime)}",
            "fi",
        ]
    lines += _modes(runtime)
    runner = compat_root / RUNNER_NAME
    temporary = runner.with_name(f"{runner.name}.tmp-{os.getpid()}")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(temporary, 0o700)
    temporary.replace(runner)
    return runner
