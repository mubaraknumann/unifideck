"""launcher/xbox_pc/errors.py — why an Xbox PC launch refused or failed.

Every refusal is an :class:`XboxPcLaunchError` with a short ``reason``. The
launcher service turns the reason into a toast through
:data:`REASON_TOAST_KEYS`, so a user always sees why nothing started
(CLAUDE.md rule 8). The result's ``error_code`` is ``xbox_pc_<reason>``.

Every toast key here declares ``{{game_key}}``. ``xboxPcLaunchFailed`` also
declares ``{{reason}}``, because it covers the reasons that have no string
of their own.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

from unifideck.launcher.types.errors import LauncherError
from unifideck.launcher.types.exit_codes import ExitCode

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


def toast_key(reason: str) -> str:
    """The toast key of ``reason``, or the generic launch-failed key."""
    return REASON_TOAST_KEYS.get(reason, LAUNCH_FAILED_KEY)


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
