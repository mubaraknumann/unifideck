"""Tag game windows with STEAM_GAME so gamescope brings them to the foreground.

umu's own ``monitor_windows`` does this, but only when ``is_steammode`` is
True — which requires ``container=flatpak``.  We run inside
``container=pressure-vessel`` so umu skips that path.

The fix: game windows (WM_CLASS = "steam_app_<appid>") appear on display ``:0``
(the gamescope compositor), not ``:1``.  Setting STEAM_GAME=<appid> on them
causes gamescope to add the app to FOCUSABLE_APPS and switch focus immediately.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The gamescope compositor's X display. Game windows appear here, not on
#: the nested ``:1`` the Steam client itself draws on. Overridable because
#: the number is not guaranteed — a second session, or a device that is not
#: a Deck, can land elsewhere.
_DISPLAY = os.environ.get("UNIFIDECK_GAMESCOPE_DISPLAY", ":0")
_POLL_INTERVAL = 0.3
_MAX_RUNTIME = 300


def _find_umu_zipapp() -> Path | None:
    """Return path to the umu zipapp bundled with this plugin."""
    # infrastructure/ → proton/ → launcher/ → unifideck/ → py_modules/ → plugin_root/
    here = Path(__file__).resolve().parent
    plugin_root = here.parents[4]
    candidate = plugin_root / "bin" / "umu" / "umu" / "umu_run.py"
    if candidate.is_file():
        return candidate
    return None


def _import_xlib() -> tuple[Any, Any] | None:
    """``(X, Display)`` from the bundled umu zipapp, or ``None``.

    Xlib is not a dependency of this plugin; it rides along inside the umu
    zipapp. That path goes on ``sys.path`` only for the duration of the
    import — leaving it there for the life of the launcher process would let
    the zipapp shadow later imports of any name it also happens to contain.
    """
    umu_zip = _find_umu_zipapp()
    if umu_zip is None:
        logger.warning("[gamescope_tagger] umu zipapp not found, cannot tag")
        return None
    zip_str = str(umu_zip)
    added = zip_str not in sys.path
    if added:
        sys.path.insert(0, zip_str)
    try:
        from Xlib import X
        from Xlib.display import Display
    except ImportError as e:
        logger.warning("[gamescope_tagger] Xlib import failed: %s", e)
        return None
    else:
        return X, Display
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(zip_str)


def _tag_windows(appid: int, stop_event: threading.Event) -> None:
    imported = _import_xlib()
    if imported is None:
        return
    xlib_x, xlib_display = imported

    try:
        d = xlib_display(_DISPLAY)
    except Exception as e:
        logger.warning(
            "[gamescope_tagger] cannot open display %s: %s", _DISPLAY, e,
        )
        return

    root = d.screen().root
    root.change_attributes(event_mask=xlib_x.SubstructureNotifyMask)
    d.flush()

    atom_steam_game = d.intern_atom("STEAM_GAME", only_if_exists=False)
    wm_class_str = f"steam_app_{appid}"
    tagged: set[int] = set()
    logger.info(
        "[gamescope_tagger] watching %s for WM_CLASS=%s", _DISPLAY, wm_class_str,
    )

    # Windows that already exist before the listener was attached.
    try:
        for child in root.query_tree().children:
            _try_tag(d, child, atom_steam_game, wm_class_str, appid, tagged)
    except Exception as e:
        logger.debug("[gamescope_tagger] initial scan error: %s", e)

    _watch_loop(
        d, xlib_x, stop_event, atom_steam_game, wm_class_str, appid, tagged,
    )

    d.close()
    logger.info("[gamescope_tagger] done, tagged %d window(s)", len(tagged))


def _watch_loop(
    d: Any,
    xlib_x: Any,
    stop_event: threading.Event,
    atom_steam_game: int,
    wm_class_str: str,
    appid: int,
    tagged: set[int],
) -> None:
    """Tag matching windows as they appear, until stopped or timed out."""
    deadline = time.monotonic() + _MAX_RUNTIME
    while not stop_event.is_set() and time.monotonic() < deadline:
        while d.pending_events():
            ev = d.next_event()
            if ev.type != xlib_x.CreateNotify:
                continue
            try:
                _try_tag(
                    d, ev.window, atom_steam_game, wm_class_str, appid, tagged,
                )
            except Exception as e:
                logger.debug("[gamescope_tagger] event error: %s", e)
        time.sleep(_POLL_INTERVAL)


def _try_tag(
    d: Any,
    window: Any,
    atom_steam_game: int,
    wm_class_str: str,
    appid: int,
    tagged: set[int],
) -> None:
    wid = window.id
    if wid in tagged:
        return
    cls = window.get_wm_class()
    if not cls:
        return
    # WM_CLASS is a tuple (instance, class); game windows have both set to steam_app_<appid>
    if wm_class_str not in cls:
        return
    window.change_property(atom_steam_game, d.get_atom("CARDINAL"), 32, [appid])
    d.flush()
    tagged.add(wid)
    logger.info("[gamescope_tagger] STEAM_GAME=%d set on wid=0x%x", appid, wid)


def start_window_tagger(appid: int) -> threading.Event:
    """Start a daemon thread that tags game windows on :0 with STEAM_GAME=appid."""
    stop = threading.Event()
    t = threading.Thread(
        target=_tag_windows,
        args=(appid, stop),
        daemon=True,
        name=f"gamescope-tagger-{appid}",
    )
    t.start()
    logger.info("[gamescope_tagger] started for appid=%d", appid)
    return stop
