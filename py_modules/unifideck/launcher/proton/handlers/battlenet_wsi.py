"""The gamescope-WSI workaround: measured per host, never assumed.

py_modules/unifideck/launcher/proton/handlers/battlenet_wsi.py

The Battle.net client's CEF renderer uses **ANGLE**, and on some hosts
ANGLE's device creation trips an assertion inside gamescope's Vulkan WSI
layer::

    [Gamescope WSI] Application info:
      pApplicationName: Battle.net.exe
      pEngineName: ANGLE
    [Gamescope WSI] Forcing on VK_EXT_swapchain_maintenance1.
    vkroots.h:129: … insert(Object, DispatchPtr) [with Object = VkQueue_T*]:
    Assertion `obj' failed.

A null ``VkQueue`` reaching the layer's dispatch table. The Wine session
dies and umu still returns 0, so nothing upstream looks like an error.

**It is host-specific, and that is the whole design constraint here.**
Measured 2026-08-19 on two machines running the *same* SteamOS 3.8.25
(build 20260807.2), the *same* kernel 6.18.42-valve2 and the *same*
gamescope 3.16.23.5:

===============  ==================  ==========
Device           GPU                 Client
===============  ==================  ==========
Steam Deck       Van Gogh 1002:163f  works
ROG Ally X       Phoenix 1002:15BF   aborts
===============  ==================  ==========

So the fix cannot be an unconditional ``DISABLE_GAMESCOPE_WSI=1``.
Disabling the layer costs the XWayland-bypass path — direct scanout, HDR —
and not only for the client: the game is started *by* the client and
inherits its environment, so every Blizzard title on every healthy host
would pay for a bug it does not have.

Hence: **react, never predict.** The first launch on an affected host runs
normally, fails in ~30 s (the readiness wait notices the client is gone),
and only then is the game log consulted for the signature above. If it is
there, the workaround is recorded for this host and the launch is retried
immediately. Every later launch reads the marker and applies it up front.

Deliberately *not* keyed on the GPU id. A hardware allowlist is the same
mistake as the 32-bit-Vulkan filename probe that refused a user their
client on a machine that had the driver: the failure mode of a guess is a
false negative, and the failure mode of measuring is one slow launch, once.

The marker is **host-scoped**, not per-prefix: this is a property of the
graphics stack, and a per-prefix marker would re-pay the 30 s discovery for
every game the user installs.

Stdlib-only; runs under the SYSTEM python (3.10-3.14).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: The layer's own documented off-switch, read from its manifest at
#: ``/usr/share/vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.*.json``:
#: ``disable_environment: {"DISABLE_GAMESCOPE_WSI": "1"}``. NOT
#: ``ENABLE_GAMESCOPE_WSI=0`` — the Vulkan loader tests for the *presence*
#: of the disable key, and the gamescope session sets the enable key itself.
DISABLE_VAR = "DISABLE_GAMESCOPE_WSI"

MARKER_FILENAME = "battlenet_gamescope_wsi.json"

# Both must appear for the crash to count as this one. The assertion alone
# could in principle come from another layer in the chain, and the WSI
# banner alone is on every healthy launch too — it is the pair that is
# diagnostic.
_ASSERT_SIGNATURE = "vkroots"
_ASSERT_DETAIL = "Assertion `obj' failed"
_LAYER_SIGNATURE = "[Gamescope WSI]"

# The signature sits at the very end of a crashed log; reading the tail
# keeps this cheap even when a long session wrote megabytes first.
_TAIL_BYTES = 64 * 1024


def _data_dir() -> Path:
    """Resolved per call — a module constant outlives a redirected HOME."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "unifideck"


def marker_path() -> Path:
    """Where this host's verdict is recorded."""
    return _data_dir() / MARKER_FILENAME


def workaround_recorded() -> bool:
    """Whether this host has already been measured to need the workaround."""
    try:
        data = json.loads(marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(isinstance(data, dict) and data.get("disable_gamescope_wsi"))


def record_workaround(reason: str) -> None:
    """Remember that this host needs the layer off. Best effort.

    A failed write costs one repeated slow launch, never a failed one, so
    this must not raise into the launch path.
    """
    payload = {
        "disable_gamescope_wsi": True,
        "recorded_at": time.time(),
        "reason": reason,
    }
    try:
        path = marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("[battlenet] could not record the WSI workaround: %s", exc)
        return
    logger.info("[battlenet] recorded the gamescope-WSI workaround for this host")


def apply_if_recorded(env: dict[str, str]) -> bool:
    """Set the disable var when this host has been measured to need it."""
    if not workaround_recorded():
        return False
    env[DISABLE_VAR] = "1"
    logger.info(
        "[battlenet] gamescope WSI layer disabled for this host "
        "(recorded after a previous ANGLE abort)",
    )
    return True


def apply_now(env: dict[str, str]) -> None:
    """Turn the layer off for this run, without consulting the marker."""
    env[DISABLE_VAR] = "1"


def _tail(path: Path) -> str:
    """The last :data:`_TAIL_BYTES` of ``path``, or empty when unreadable."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _TAIL_BYTES:
                handle.seek(size - _TAIL_BYTES)
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def crashed_in_gamescope_wsi(log_path: Path | None) -> bool:
    """Whether ``log_path`` ends in the ANGLE-in-WSI abort.

    Reading the client's own output is what makes this a *measurement*
    rather than a guess, and it is only possible because phase A writes to
    the per-launch game log instead of ``DEVNULL``.
    """
    if log_path is None:
        return False
    text = _tail(Path(log_path))
    if not text:
        return False
    return (
        _LAYER_SIGNATURE in text
        and _ASSERT_SIGNATURE in text
        and _ASSERT_DETAIL in text
    )


async def adopt_workaround(plan: object, game_title: str) -> bool:
    """Turn the layer off for this run, if that is provably why we died.

    Returns True when the caller should retry. Consults the client's own
    output rather than assuming: the abort is host-specific, so disabling
    the layer up front would cost every healthy host the XWayland-bypass
    path — and the *game* too, since it inherits the client's environment —
    to fix a minority. Measuring costs one slow launch, once per host.

    Takes ``plan`` as ``object`` rather than importing ``ProtonLaunchPlan``:
    that type lives in ``proton.infrastructure``, which imports this module
    for :func:`apply_if_recorded`, and naming it here would close the loop.
    """
    import asyncio

    from unifideck.launcher.frontend_bridge import launcher_toast
    from unifideck.launcher.proton.infrastructure.game_log import game_log_path

    env: dict[str, str] = plan.env  # type: ignore[attr-defined]
    if env.get(DISABLE_VAR):
        # Already off and it still died, so the layer was never the problem.
        return False
    if not await asyncio.to_thread(crashed_in_gamescope_wsi, game_log_path()):
        return False
    logger.warning(
        "[battlenet] the client aborted inside gamescope's Vulkan WSI layer "
        "(ANGLE); retrying with the layer disabled",
    )
    apply_now(env)
    await asyncio.to_thread(
        record_workaround,
        "ANGLE abort in VK_LAYER_FROG_gamescope_wsi (vkroots null VkQueue)",
    )
    launcher_toast(
        "toasts.launcher.battlenetRetryingWithoutWsiMessage",
        i18n_title_key="toasts.header.battlenet",
        game_title=game_title,
    )
    return True
