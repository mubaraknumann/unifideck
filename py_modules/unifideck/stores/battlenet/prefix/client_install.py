"""Download and silently install the Battle.net client into a prefix.

py_modules/unifideck/stores/battlenet/prefix/client_install.py

Nothing is bundled. The client is fetched at runtime from Blizzard's own
installer URL and cached, exactly as Ubisoft fetches
``UbisoftConnectInstaller.exe`` — shipping a vendor installer in the plugin
would be both large and stale.

Proven on-device 2026-07-03: the official 4.9 MB stub, run under ``umu-run``
with ``WINEPREFIX`` pointing at a fresh prefix, downloaded and installed the
client and Agent unattended and exited 0.

Two things this must get right or the install hangs rather than fails:

* **Display environment.** The plugin runs headless under
  ``plugin_loader``; a Wine process with no ``DISPLAY`` /
  ``XDG_RUNTIME_DIR`` / DBus hangs. ``WineEnvResolver`` borrows them from
  the live Steam process.
* **The client is 32-bit** (PE32 i386, confirmed on-device). Without a
  32-bit Vulkan driver on the host the installer freezes around 25% with no
  error at all.

That second one used to be a **gate**: a filename guess at the host's Vulkan
ICDs, refusing the install when it came up empty. It came up empty on a
CachyOS machine whose driver Steam had enumerated 42 seconds earlier, and
that user was left with no client, no sign-in and no library — for a
capability they had. So the shape is now:

* :mod:`unifideck.utils.vulkan` answers the question properly, by ELF class
  rather than by filename, and is allowed to answer "I cannot tell";
* **no verdict blocks the install.** Not even ``ABSENT`` — it buys a stall
  watchdog and a warning, not a refusal;
* the hardware-acceleration tweak is written *before* the installer runs
  rather than after, so the stub itself has the best chance of not needing
  the GPU in the first place.

The rule this encodes: never refuse a user the client over a preflight
whose failure mode is a false negative.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import ssl
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from unifideck.launcher.proton.infrastructure.proton_health import check_after_failure
from unifideck.launcher.wrapper_locale import bootstrapper_locale
from unifideck.stores.battlenet import paths
from unifideck.stores.shared.wine_env import WineEnvResolver
from unifideck.utils.vulkan import Vulkan32, detect_32bit_vulkan

from . import tweaks
from .stall_watchdog import stall_watchdog

logger = logging.getLogger(__name__)

# The stub is ~4.9 MB; anything wildly off means we cached an error page.
MIN_INSTALLER_BYTES = 1_000_000
DOWNLOAD_TIMEOUT_SECONDS = 300
# The installer downloads the real client, so it needs a generous budget.
INSTALL_TIMEOUT_SECONDS = 1800
# Applied only when the host is *proven* to lack a 32-bit Vulkan driver: how
# long the installer may write nothing at all before we call it wedged.
# Measured against progress, never the clock — see
# :func:`~.stall_watchdog.stall_watchdog`.
NO_VULKAN_STALL_SECONDS = 300.0

# Pre-answer the two bootstrapper screens that otherwise wait for a click.
# Without them the wizard stops on the language screen forever — see
# :func:`run_silent_install`. Verified on-device: with these it reaches
# STATE_UPDATE_BOOTSTRAPPER and starts downloading; without, it never
# leaves STATE_SELECT_LANGUAGE.
INSTALLER_ARGS = (
    "--lang=enUS",
    "--installpath=C:\\Program Files (x86)\\Battle.net",
)


def installer_args() -> tuple[str, ...]:
    """:data:`INSTALLER_ARGS` with the language taken from the plugin locale.

    The install path stays pinned: ``wrapper_session_specs`` hashes it to find
    the client's section in ``Battle.net.config``, so it must be the same
    string in every prefix.

    The language does not have to be, and pinning it had a cost beyond an
    English wizard. The bootstrapper derives its **region** from the locale
    (``Configuration: locale=enUS region=US`` in its own log) and warms the
    Agent's content store for that region *before* login. The account's real
    region then arrives at login and invalidates the warm-up, which is a
    45-minute re-download the first time. Guaranteed for every non-US user
    while this said ``enUS`` unconditionally.

    ``bootstrapper_locale`` falls back to ``enUS`` for any tag Battle.net does
    not ship, because the failure mode of a bad value here is the wizard
    stalling on its language screen with no visible window; see its docstring.
    """
    locale = bootstrapper_locale("battlenet")
    return (f"--lang={locale}", *INSTALLER_ARGS[1:])

GAMEID = "umu-battlenet"


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Outcome of preparing a prefix with the client in it."""

    success: bool
    error: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class InstallOutcome:
    """Outcome of one installer run.

    ``stalled`` is carried separately so the caller can report *why* rather
    than a generic failure: an installer killed for writing nothing on a
    host with no 32-bit Vulkan driver is a different message from one that
    exited on its own.
    """

    installed: bool
    stalled: bool = False


def _ssl_context() -> ssl.SSLContext:
    """Permissive TLS.

    An outdated CA bundle on SteamOS breaks otherwise-fine downloads, and
    the plugin disables verification everywhere except the updater for
    exactly this reason.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _download_sync(url: str, destination: Path) -> bool:
    tmp = destination.with_suffix(destination.suffix + ".part")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": "Unifideck"})
        with urllib.request.urlopen(
            request, timeout=DOWNLOAD_TIMEOUT_SECONDS, context=_ssl_context(),
        ) as response, tmp.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except (OSError, ValueError) as exc:
        logger.warning("[Battlenet] installer download failed: %s", exc)
        tmp.unlink(missing_ok=True)
        return False
    if tmp.stat().st_size < MIN_INSTALLER_BYTES:
        logger.warning(
            "[Battlenet] downloaded installer is only %d bytes — discarding",
            tmp.stat().st_size,
        )
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(destination)
    return True


def _cached_installer_is_usable(path: Path) -> bool:
    """A cached file only counts if it is plausibly the real stub.

    Guards against an error page or a truncated download being reused
    forever as though it were the installer.
    """
    try:
        return path.is_file() and path.stat().st_size >= MIN_INSTALLER_BYTES
    except OSError:
        return False


async def ensure_installer(url: str, cache_path: Path) -> Path | None:
    """Return a cached installer, downloading it if absent. Never raises."""
    path = Path(cache_path)
    if await asyncio.to_thread(_cached_installer_is_usable, path):
        return path
    logger.info("[Battlenet] downloading client installer from %s", url)
    ok = await asyncio.to_thread(_download_sync, url, path)
    return path if ok else None


def has_32bit_vulkan() -> bool:
    """Whether a 32-bit Vulkan driver is installed on the host.

    Kept as the boolean face of :func:`~unifideck.utils.vulkan.detect_32bit_vulkan`
    for callers that only want a yes. ``UNKNOWN`` reads as ``False`` here,
    which is why :func:`bootstrap_client` uses the three-way verdict instead:
    the distinction between "no" and "cannot tell" is the whole point.
    """
    return detect_32bit_vulkan().verdict is Vulkan32.PRESENT


async def run_silent_install(
    installer: Path,
    prefix: Path,
    resolver: WineEnvResolver,
    *,
    stall_timeout: float | None = None,
    proton_path: str | None = None,
) -> InstallOutcome:
    """Run the installer inside ``prefix`` under umu. Never raises.

    ``proton_path`` is the Proton the *launcher* will later run this
    client with, passed down so the prefix is built and driven by one
    Proton rather than two. Without it the resolver picks its own, and on
    a host where those differ the prefix is created by one Wine build and
    every subsequent client start happens under another. ``None`` leaves
    the resolver to choose, which is still the right answer for a caller
    that has no launch plan to speak from.

    ``stall_timeout`` arms :func:`stall_watchdog` for hosts proven to lack
    a 32-bit Vulkan driver. ``None`` — every other path — keeps the plain
    30-minute budget and no polling.

    The arguments are load-bearing, not cosmetic. Launched bare, the
    bootstrapper opens its wizard and *waits on the language screen* — in
    Gaming Mode, behind everything, where nobody clicks it. Blizzard's own
    log recorded the dead end three times in a row::

        Bootstrapper State: STATE_SELECT_LANGUAGE
        Active screen changed: language
        <nothing, ever>

    The user saw a Sign In button that did nothing while an invisible
    wizard blocked for the full 30-minute timeout. With the arguments,
    ``locale`` is pre-set and it goes straight through::

        Configuration: locale=enUS region=US
        Bootstrapper State: STATE_CHECK_ENVIRONMENT
        Bootstrapper State: STATE_UPDATE_BOOTSTRAPPER
        Downloading from version service: …/bts/versions

    ``--installpath`` is passed for the same reason — it pre-answers the
    other screen that can stall waiting for input.
    """
    umu_run = resolver.find_umu_run()
    if not umu_run:
        logger.error("[Battlenet] umu-run not found — cannot install the client")
        return InstallOutcome(installed=False)

    env = _installer_env(resolver, prefix, proton_path)
    if env is None:
        return InstallOutcome(installed=False)

    await asyncio.to_thread(Path(prefix).mkdir, parents=True, exist_ok=True)
    logger.info("[Battlenet] installing client into %s", prefix)
    try:
        proc = await asyncio.create_subprocess_exec(
            umu_run,
            str(installer),
            *installer_args(),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        logger.exception("[Battlenet] could not spawn the installer")
        return InstallOutcome(installed=False)

    outcome = await _await_installer(proc, prefix, stall_timeout)
    if not outcome.installed:  # a damaged Proton kills the installer in seconds
        await asyncio.to_thread(check_after_failure, env)
    return outcome


def _installer_env(
    resolver: WineEnvResolver, prefix: Path, proton_path: str | None,
) -> dict[str, str] | None:
    """The installer's umu environment, or None when there is no display."""
    env = resolver.build_env(prefix, GAMEID, proton_path=proton_path)
    logger.info(
        "[Battlenet] installer PROTONPATH=%s (%s)",
        env.get("PROTONPATH"),
        "from the launch plan" if proton_path else "resolver's choice",
    )
    if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        # Headless Decky env: a Wine process with no display hangs instead
        # of failing, so refuse rather than burn the timeout.
        logger.error(
            "[Battlenet] no DISPLAY/WAYLAND_DISPLAY available — refusing to "
            "run the installer (it would hang rather than fail)",
        )
        return None
    return env


async def _await_installer(
    proc: asyncio.subprocess.Process,
    prefix: Path,
    stall_timeout: float | None,
) -> InstallOutcome:
    """Wait out the installer, with the stall watchdog when one is armed."""
    watchdog: asyncio.Task[None] | None = None
    if stall_timeout:
        watchdog = asyncio.ensure_future(stall_watchdog(Path(prefix), proc, stall_timeout))
    try:
        completed, err = await _run_installer_to_exit(proc)
    finally:
        await _stop_watchdog(watchdog)
    if not completed:
        return InstallOutcome(installed=False)
    # The watchdog only ever finishes by killing the process, so a completed
    # task means the run it was watching was the stalled one.
    stalled = watchdog is not None and not watchdog.cancelled()
    return await _installer_outcome(proc, prefix, err, stalled=stalled)


async def _run_installer_to_exit(
    proc: asyncio.subprocess.Process,
) -> tuple[bool, bytes]:
    """``(exited on its own, stderr)`` — killing it at the hard timeout."""
    try:
        _out, err = await asyncio.wait_for(
            proc.communicate(), timeout=INSTALL_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.exception("[Battlenet] installer timed out — killing")
        proc.kill()
        await proc.wait()
        return False, b""
    return True, err


async def _stop_watchdog(watchdog: asyncio.Task[None] | None) -> None:
    """Cancel the stall watchdog and wait for it to unwind. Safe on ``None``."""
    if watchdog is None:
        return
    watchdog.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await watchdog


async def _installer_outcome(
    proc: asyncio.subprocess.Process,
    prefix: Path,
    err: bytes,
    *,
    stalled: bool,
) -> InstallOutcome:
    """Read the verdict off the filesystem rather than off the exit code."""
    if proc.returncode != 0:
        logger.warning(
            "[Battlenet] installer exited %s: %s",
            proc.returncode,
            err.decode(errors="replace")[-400:],
        )
    # Trust the filesystem over the exit code: the stub has been observed
    # exiting non-zero after a successful install.
    installed = bool(await asyncio.to_thread(paths.client_installed, prefix))
    return InstallOutcome(installed=installed, stalled=stalled and not installed)


def apply_prefix_tweaks(prefix: Path) -> bool:
    """Write the settings the client needs before its first run."""
    drive_c = paths.drive_c(prefix)
    if drive_c is None:
        return False
    ok = tweaks.write_client_config(drive_c)
    if ok:
        tweaks.mark_applied(prefix)
    return ok


def preseed_client_config(prefix: Path) -> bool:
    """Write ``HardwareAcceleration=false`` *before* the installer runs.

    The bootstrapper stub is CEF too, and the config it reads is the same
    one the installed client reads. Seeding it first is the one thing here
    that actually shrinks the 32-bit-Vulkan dependency rather than
    detecting it, so it happens on every path regardless of verdict.

    Best-effort by design: a fresh prefix has no ``drive_c`` until umu
    creates one, in which case this no-ops and
    :func:`apply_prefix_tweaks` writes the same settings afterwards.
    ``write_client_config`` merges, so writing twice is harmless.
    """
    drive_c = paths.drive_c(prefix)
    if drive_c is None:
        return False
    return tweaks.write_client_config(drive_c)


def _stall_leash(on_warning: Callable[[], None] | None) -> float | None:
    """Arm the stall watchdog only when 32-bit Vulkan is *proven* missing.

    Never returns a refusal. ``UNKNOWN`` is treated exactly like
    ``PRESENT``: a probe that could not tell must not cost the user their
    client, which is the whole reason this stopped being a gate.
    """
    report = detect_32bit_vulkan()
    logger.info("[Battlenet] 32-bit Vulkan check: %s", report.summary())
    if report.verdict is not Vulkan32.ABSENT:
        if report.verdict is Vulkan32.UNKNOWN:
            logger.warning(
                "[Battlenet] could not determine 32-bit Vulkan support — "
                "installing anyway rather than blocking on it",
            )
        return None
    logger.warning(
        "[Battlenet] no 32-bit Vulkan driver found — installing anyway, but "
        "the installer will be killed after %.0fs of no progress",
        NO_VULKAN_STALL_SECONDS,
    )
    if on_warning is not None:
        on_warning()
    return NO_VULKAN_STALL_SECONDS


async def bootstrap_client(
    prefix: Path,
    *,
    installer_url: str,
    installer_cache: Path,
    resolver: WineEnvResolver,
    on_warning: Callable[[], None] | None = None,
    proton_path: str | None = None,
) -> BootstrapResult:
    """Ensure ``prefix`` contains a usable, tweaked Battle.net client.

    ``on_warning`` fires once, before the install, when the host is proven
    to lack a 32-bit Vulkan driver. It exists so the launcher can toast
    that warning without this module reaching into the frontend bridge.

    ``proton_path`` is the launcher's own Proton; see
    :func:`run_silent_install`.

    Reached for an *incomplete* client too, not only a missing one:
    ``client_installed`` requires the versioned payload, so a prefix
    holding only the bootstrapper's shim falls through to the installer
    and gets completed in place rather than passing as ready.
    """
    if paths.client_installed(prefix):
        if not tweaks.tweaks_applied(prefix):
            apply_prefix_tweaks(prefix)
        return BootstrapResult(success=True)

    stall_timeout = _stall_leash(on_warning)

    installer = await ensure_installer(installer_url, installer_cache)
    if installer is None:
        return BootstrapResult(
            success=False,
            error="Could not download the Battle.net installer",
            error_code="installer_download_failed",
        )

    preseed_client_config(prefix)
    outcome = await run_silent_install(
        installer, prefix, resolver,
        stall_timeout=stall_timeout, proton_path=proton_path,
    )
    if not outcome.installed:
        return _install_failure(outcome)

    apply_prefix_tweaks(prefix)
    logger.info("[Battlenet] client installed into %s", prefix)
    return BootstrapResult(success=True)


def _install_failure(outcome: InstallOutcome) -> BootstrapResult:
    """Name the failure. A stall on a driverless host is its own diagnosis."""
    if outcome.stalled:
        return BootstrapResult(
            success=False,
            error=(
                "The Battle.net installer stopped making progress. This host has "
                "no 32-bit Vulkan driver, and the client's installer is 32-bit."
            ),
            error_code="missing_32bit_vulkan",
        )
    return BootstrapResult(
        success=False,
        error="The Battle.net client installer did not complete",
        error_code="client_install_failed",
    )
