"""Cloud-sync failure classification and user-facing toasts.

Called from ``services/launcher/helpers.cloud_sync_phase`` when a sync
raises. Classifies the exception into a stable code, honours
``cloud.failure_behavior.<store>``, and emits a ``LAUNCHER_STAGE`` toast —
the one channel that reaches the UI from the out-of-process launcher.

**This module was unimported until 2026-08-26**, and the history matters
because two decisions were taken on the assumption it ran:

* ``cloud_sync_phase`` caught every sync exception with a bare
  ``logger.warning(... ignoring ...)``. A failed **upload** was completely
  silent: the user quit the game believing their progress had reached the
  cloud, and found out otherwise on another device.
* Audit §1.2 read ``get_failure_behavior`` as "read live at launch" and, on
  that basis, deleted the two RPCs that would have let a user silence the
  toast — reasoning that the current behaviour was acceptable. There was no
  behaviour to accept. The config key survives and is still honoured here;
  it is hand-editable in ``~/.config/unifideck/config.json``.

Surfacing upload failures is a product decision taken 2026-08-26 (audit
register item 37).

Two contracts to keep in mind when changing the payload:

* the message strings interpolate ``{{error}}``, while this module sends a
  machine ``error_code`` plus ``error_i18n_key`` so the reason stays
  translatable. ``src/services/toast-params.ts`` resolves one into the
  other; sending English from here would be untranslatable.
* ``_TOAST_ACTIONS`` still uses an ``{i18n_label_key, target_url}`` shape,
  while both frontend renderers parse ``{verb, args}``. Those actions are
  therefore **not** rendered yet — audit register item 4b.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.types.events import Events

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.event_bus.event_bus import EventBus
logger = logging.getLogger(__name__)
def _classify_aiohttp_error(err: BaseException) -> str | None:
    """Classify a network error from the ``aiohttp`` family.

    Returns one of the canonical codes (``network_unreachable``,
    ``auth_expired``, ``forbidden``, ``quota_exceeded``,
    ``server_error``, ``timed_out``) or ``None`` if the error is
    not from ``aiohttp`` — the caller should then try the next
    classifier.

    The ``aiohttp`` import is lazy so the module loads even on
    minimal installs where ``aiohttp`` is unavailable; in that
    case we always return ``None`` and let other classifiers
    handle the error.
    """
    try:
        import aiohttp
    except ImportError:
        return None

    if isinstance(err, aiohttp.ClientConnectionError):
        return "network_unreachable"

    if isinstance(err, aiohttp.ClientResponseError):
        status = getattr(err, "status", 0)
        if status == 401:
            return "auth_expired"
        if status == 403:
            return "forbidden"
        if status == 413:
            return "quota_exceeded"
        if 500 <= status < 600:
            return "server_error"

    # ``aiohttp.ClientTimeout`` is a configuration dataclass, NOT
    # an exception type — an earlier version of this function
    # checked ``isinstance(err, aiohttp.ClientTimeout)`` which
    # was always False, so every timed-out sync silently fell
    # through to "unknown". The actual exceptions raised on
    # timeout are ``aiohttp.ServerTimeoutError`` (which inherits
    # from ``asyncio.TimeoutError``) and the bare
    # ``asyncio.TimeoutError`` raised from ``async with timeout``.
    if isinstance(err, (aiohttp.ServerTimeoutError, TimeoutError)):
        return "timed_out"

    return None


def _classify_os_error(err: BaseException) -> str | None:
    """Classify a filesystem ``OSError`` by its ``errno``.

    Returns ``disk_full`` for ``ENOSPC`` and ``permission_denied``
    for ``EACCES``/``EPERM``. Other ``OSError`` codes fall through
    to ``None`` so the caller can try the next classifier (e.g.
    the ``LowDiskSpaceError`` heuristic).
    """
    if not isinstance(err, OSError):
        return None
    import errno
    if err.errno == errno.ENOSPC:
        return "disk_full"
    if err.errno in (errno.EACCES, errno.EPERM):
        return "permission_denied"
    return None


def _classify_disk_space_error(err: BaseException) -> str | None:
    """Classify our own ``LowDiskSpaceError`` if it's importable.

    This is split out so unit tests can swap in their own error
    types without monkey-patching the main classifier. The import
    is wrapped in ``try``/``except ImportError`` because the
    ``disk_space`` module may not be available in stripped builds.
    """
    try:
        from .disk_space import LowDiskSpaceError
    except ImportError:
        return None
    if isinstance(err, LowDiskSpaceError):
        return "disk_space_low"
    return None


def _classify_cancellation(err: BaseException) -> str | None:
    """Classify ``asyncio.CancelledError`` as ``cancelled``.

    Always-available; ``asyncio`` is part of stdlib. Wrapped in
    the same shape as the other classifiers for consistency.
    """
    import asyncio
    if isinstance(err, asyncio.CancelledError):
        return "cancelled"
    return None


# Ordered chain of classifiers. The first non-``None`` result wins;
# if none match, the caller returns ``"unknown"``. Order matters —
# network errors must be tried first because OSError is a parent of
# ``aiohttp.ClientOSError`` and would shadow it otherwise.
_CLASSIFIERS = (
    _classify_aiohttp_error,
    _classify_os_error,
    _classify_disk_space_error,
    _classify_cancellation,
)


def classify_cloud_error(err: BaseException) -> str:
    """Map a raised exception to a stable cloud-failure code.

    Walks ``_CLASSIFIERS`` in order and returns the first match.
    Falls back to ``"unknown"`` so callers can always rely on a
    string code (no None handling at the call site).
    """
    for classifier in _CLASSIFIERS:
        code = classifier(err)
        if code is not None:
            return code
    return "unknown"

_DEFAULT_BEHAVIOR = "toast"
_VALID_BEHAVIORS = frozenset({"silent", "toast"})
def get_failure_behavior(config: ConfigManager | None, store: str) -> str:
    """Get failure behavior."""
    if config is None or not hasattr(config, "get_str"):
        return _DEFAULT_BEHAVIOR
    key = f"cloud.failure_behavior.{store}"
    raw = config.get_str(key, "")
    if not raw:
        raw = config.get_str(
            "cloud.failure_behavior.default", _DEFAULT_BEHAVIOR,
        )
    if raw not in _VALID_BEHAVIORS:
        logger.warning(
            "[cloud_failure] invalid behavior %r for store %s, "
            "falling back to %r", raw, store, _DEFAULT_BEHAVIOR,
        )
        return _DEFAULT_BEHAVIOR
    return raw
async def handle_cloud_sync_failure(
    bus: EventBus,
    config: ConfigManager | None,
    *,
    phase: str,
    store: str,
    game_id: str,
    error: BaseException,
) -> None:
    """Handle cloud sync failure."""
    code = classify_cloud_error(error)
    behavior = get_failure_behavior(config, store)
    logger.error(
        "[cloud_failure] phase=%s store=%s game_id=%s "
        "error_code=%s behavior=%s error=%s",
        phase, store, game_id, code, behavior, error,
        exc_info=error,
    )
    if behavior == "silent":
        return
    await _emit_toast(bus, phase=phase, store=store, game_id=game_id, code=code)
async def _emit_toast(
    bus: EventBus, *,
    phase: str, store: str, game_id: str, code: str,
) -> None:
    """Emit toast."""
    if bus is None:
        return  # type: ignore[unreachable]  # fallback for unexpected error shape
    i18n_key = (
        "toasts.launcher.cloudSyncDownFailed"
        if phase == "sync_down"
        else "toasts.launcher.cloudSyncUpFailed"
    )
    payload: dict[str, Any] = {
        "severity": "warning",
        "i18n_key": i18n_key,
        "i18n_params": {
            "store": store,
            "error_code": code,
            "error_i18n_key": f"cloudSync.error.{code}",
        },
        "duration_ms": 6000,
        "game_id": game_id,
        "store": store,
        "phase": phase,
    }
    resolved_action = _resolve_toast_action(
        code, store=store, game_id=game_id, phase=phase,
    )
    if resolved_action is not None:
        payload["action"] = resolved_action
    try:
        await bus.emit(Events.LAUNCHER_STAGE, **payload)
    except Exception:
        logger.exception("[cloud_failure] toast emit failed")

def _resolve_toast_action(
    code: str, *, store: str, game_id: str, phase: str,
) -> dict[str, Any] | None:
    """The one-click remediation for *code*, in the frontend's shape.

    Emits ``{verb, args, i18n_label_key}`` — the same shape
    ``CloudSaveService._emit_save_conflict`` already sends and both
    renderers already parse. It used to emit
    ``{i18n_label_key, target_url}`` instead, a **second** action shape for
    the same field, so a renderer written for one would read ``undefined``
    from the other. That is the ``app_id``/``game_id`` split of audit §1.1.1,
    between two producers rather than a producer and a consumer, and it was
    invisible because this module had no caller (item 37).

    A Steam deep link is expressed as the ``open-url`` verb with the target
    and an optional fallback as args, so one shape covers both a backend verb
    and an external URL without a second field to branch on.
    """
    action = _TOAST_ACTIONS.get(code)
    if action is None:
        return None
    ctx_vars = {"store": store, "game_id": game_id, "phase": phase}
    try:
        args = [arg.format(**ctx_vars) for arg in action["args"]]
    except (KeyError, IndexError) as err:
        logger.warning(
            "[cloud_failure] action template error for code=%s: %s — "
            "dropping action", code, err,
        )
        return None
    return {
        "verb": action["verb"],
        "args": args,
        "i18n_label_key": action["i18n_label_key"],
    }


#: code → the remediation offered with its toast.
#:
#: ``verb`` is a ``unifideck://`` action verb, except ``open-url`` which the
#: frontend opens directly (args: target, then optional fallback).
_TOAST_ACTIONS: dict[str, dict[str, Any]] = {
    "disk_space_low": {
        "i18n_label_key": "toasts.actions.openStorageManager",
        "verb": "open-url",
        "args": ["steam://settings/storage", "steam://settings"],
    },
    "disk_full": {
        "i18n_label_key": "toasts.actions.openStorageManager",
        "verb": "open-url",
        "args": ["steam://settings/storage", "steam://settings"],
    },
    "auth_expired": {
        "i18n_label_key": "toasts.actions.signInToStore",
        "verb": "auth",
        "args": ["{store}"],
    },
    "network_unreachable": {
        "i18n_label_key": "toasts.actions.retrySync",
        "verb": "retry-sync",
        "args": ["{store}", "{game_id}", "{phase}"],
    },
    "timed_out": {
        "i18n_label_key": "toasts.actions.retrySync",
        "verb": "retry-sync",
        "args": ["{store}", "{game_id}", "{phase}"],
    },
    "server_error": {
        "i18n_label_key": "toasts.actions.retrySync",
        "verb": "retry-sync",
        "args": ["{store}", "{game_id}", "{phase}"],
    },
}
