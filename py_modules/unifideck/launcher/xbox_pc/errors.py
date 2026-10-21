"""launcher/xbox_pc/errors.py — why an Xbox PC launch refused or failed.

Every refusal is an :class:`XboxPcLaunchError` with a short ``reason``. The
launcher service turns the reason into a toast through
:data:`REASON_TOAST_KEYS`, so a user always sees why nothing started
(CLAUDE.md rule 8). The result's ``error_code`` is ``xbox_pc_<reason>``.

Every toast key here declares ``{{game_key}}``, except the anti-cheat and
restart-loop ones: those stay within 15 characters so the toast does not cut
them off. A missing runtime's toast is tappable and opens Steam's install
dialog (:func:`toast_action`), so it need not spell out what to install. ``xboxPcLaunchFailed`` also declares ``{{reason}}``, because it
covers the reasons that have no string of their own.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

from typing import Any

from unifideck.launcher.types.errors import LauncherError
from unifideck.launcher.types.exit_codes import ExitCode

from .prefix_checks import BATTLEYE_RUNTIME_APPID, EAC_RUNTIME_APPID

_PREFIX = "toasts.launcher."

#: The fallback toast. It shows the reason code itself.
LAUNCH_FAILED_KEY = _PREFIX + "xboxPcLaunchFailed"

#: ``reason -> toast key``. A reason missing here gets
#: :data:`LAUNCH_FAILED_KEY`. A test checks every value against the locales.
REASON_TOAST_KEYS: dict[str, str] = {
    "compat_tool_wrapped": _PREFIX + "xboxPcCompatToolSet",
    "runtime_missing": _PREFIX + "xboxPcRuntimeMissing",
    "runtime_outdated": _PREFIX + "xboxPcRuntimeOutdated",
    "components_busy": _PREFIX + "xboxPcComponentsBusy",
    "backend_missing": _PREFIX + "xboxPcRuntimeMissing",
    "backend_tampered": _PREFIX + "xboxPcRuntimeMissing",
    "cloud_save_unavailable": _PREFIX + "xboxPcCloudSaveUnavailable",
    "steam_root_missing": _PREFIX + "xboxPcSteamRootMissing",
    "sign_in_required": _PREFIX + "xboxPcSignInRequired",
    "not_downloaded": _PREFIX + "xboxPcNotDownloaded",
    "market_unknown": LAUNCH_FAILED_KEY,
    "already_running": _PREFIX + "xboxPcAlreadyRunning",
    "prefix_failed": _PREFIX + "xboxPcPrefixFailed",
    "vcredist_missing": _PREFIX + "xboxPcVcRedistMissing",
    "eac_runtime_missing": _PREFIX + "xboxPcEacRuntimeMissing",
    "battleye_runtime_missing": _PREFIX + "xboxPcBattlEyeRuntimeMissing",
    "anticheat_unsupported": _PREFIX + "xboxPcAntiCheatUnsupported",
    "restart_loop": _PREFIX + "xboxPcRestartLoop",
    "license_denied": _PREFIX + "xboxPcLicenseDenied",
    "launch_failed": LAUNCH_FAILED_KEY,
    "game_crashed": _PREFIX + "xboxPcGameCrashed",
    "disabled": _PREFIX + "xboxPcDisabled",
    "not_installed": _PREFIX + "xboxPcNotInstalled",
}

#: Non-error notices shown while a launch goes on.
STARTING_KEY = _PREFIX + "startingXboxPcGame"
LANGUAGE_UNAVAILABLE_KEY = _PREFIX + "xboxPcLanguageUnavailable"
GAME_ARGS_IGNORED_KEY = _PREFIX + "xboxPcGameArgsIgnored"


#: The label of a toast that installs a Steam tool when tapped.
INSTALL_FROM_STEAM_KEY = "toasts.actions.installFromSteam"

#: ``reason -> Steam app id`` of the tool that fixes it. Its toast opens
#: Steam's install dialog for that tool (the frontend's ``open-url`` verb).
_REASON_STEAM_TOOLS: dict[str, int] = {
    "eac_runtime_missing": EAC_RUNTIME_APPID,
    "battleye_runtime_missing": BATTLEYE_RUNTIME_APPID,
}


def toast_key(reason: str) -> str:
    """The toast key of ``reason``, or the generic launch-failed key."""
    return REASON_TOAST_KEYS.get(reason, LAUNCH_FAILED_KEY)


def toast_action(reason: str) -> dict[str, Any] | None:
    """The one-tap fix offered with ``reason``'s toast, or None."""
    appid = _REASON_STEAM_TOOLS.get(reason)
    if appid is None:
        return None
    return {
        "verb": "open-url",
        "args": [f"steam://install/{appid}"],
        "i18n_label_key": INSTALL_FROM_STEAM_KEY,
    }


class XboxPcLaunchError(LauncherError):
    """An Xbox PC launch refused or failed for ``reason``.

    ``detail`` is for the log only. It is never shown, because it can hold
    paths. ``code`` is what ``handle_launcher_error`` reads, should this
    error ever escape the Xbox PC runner.
    """

    exit_code = ExitCode.GAME_FAILED

    def __init__(self, reason: str, detail: str = "") -> None:
        """Record the reason, and the detail for the log."""
        message = f"xbox_pc_{reason}: {detail}" if detail else f"xbox_pc_{reason}"
        super().__init__(message, context={"reason": reason})
        self.reason = reason
        self.detail = detail
        self.code = f"xbox_pc_{reason}"
