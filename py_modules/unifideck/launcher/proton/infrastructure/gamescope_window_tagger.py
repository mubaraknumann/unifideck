"""Tag game windows with STEAM_GAME so gamescope brings them to the foreground.

umu's own ``monitor_windows`` does this, but only when ``is_steammode`` is
True — which requires ``container=flatpak``.  We run inside
``container=pressure-vessel`` so umu skips that path.

The fix: game windows (WM_CLASS = "steam_app_<appid>") appear on display ``:0``
(the gamescope compositor), not ``:1``.  Setting STEAM_GAME=<appid> on them
causes gamescope to add the app to FOCUSABLE_APPS and switch focus immediately.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DISPLAY = ":0"
_POLL_INTERVAL = 0.3
_MAX_RUNTIME = 300

# How a COMPANION (trainer / cheat tool) window is tagged once identified.
# The main game window is always tagged with its real appid regardless.
#
# All three values have now been measured on-device with a trainer running
# alongside Ghost of Tsushima in Gaming Mode:
#
#   "fake"  — tag with the companion's own dedicated fake appid. gamescope
#             does not composite the window AT ALL: the trainer is simply
#             not on screen. An appid Steam does not know never enters the
#             focusable-app list gamescope receives FROM Steam
#             (``ctxFocusControlAppIDs`` in ``pick_primary_focus_and_override``),
#             so no fake VALUE can ever work — the earlier suspicion that
#             this was a signed-vs-unsigned range problem was wrong, a
#             value safely inside the positive int32 range behaved
#             identically.
#   "game"  — tag with the main game's real appid. Both windows exist, but
#             gamescope's focus picker paints only one window per appid, so
#             the trainer covers the game (or vice versa) with no way to
#             switch between them; closing the front one reveals the other.
#   "skip"  — leave the companion window WITHOUT a STEAM_GAME property.
#             This reproduces what the committed pre-companion tagger did
#             implicitly: it only ever matched on WM_CLASS, so a companion
#             window that did not carry the game's ``steam_app_<appid>``
#             WM_CLASS was ignored entirely.
#
# CRUCIAL CONTEXT for the table above: all three of those results were
# measured while the launcher had ESCAPED Steam's pressure-vessel container
# (``bin/unifideck-launcher``'s ``_escape_pressure_vessel``, which fires
# whenever Force-Compat is still set on the shortcut at launch time). The
# escape re-parents the whole real process tree — launcher, umu, game and
# companion — out from under Steam's ``reaper`` and onto
# ``steam-runtime-launcher-service``. Steam then no longer recognises those
# windows as belonging to the app it launched, and offers no way to switch
# between them, which is what made this look like an appid problem for so
# long.
#
# Launched WITHOUT the container (Force-Compat cleared, so Steam runs the
# launcher natively — the path ``selector.py`` documents as the intended
# one), Steam offers its OWN "switch window" control for the app and both
# the game and an entirely UNTAGGED companion window are reachable through
# it. That is why "skip" is the default: with a correct, non-escaped launch
# no tagging of the companion is needed at all, and adding any makes things
# worse rather than better.
#
# Note that "skip" is an EXPLICIT decision, not a fall-through: without an
# explicit companion branch, a companion window drops to the WM_CLASS rule
# and — since companions inherit the game's ``SteamAppId`` and therefore its
# WM_CLASS — gets tagged "game" anyway. That silent fall-through is what
# made every earlier fake-appid attempt a no-op, so the branch stays even
# where its outcome would coincide.
_COMPANION_TAG_MODE = "skip"


# PIDs of companion-executable process TREES (trainers, cheat tools) for
# THIS launch, mapped to the DEDICATED fake Steam appid computed for that
# specific companion (see ``launcher.proton.infrastructure.companions``). A
# companion's window has no ``steam_app_<appid>`` WM_CLASS of its own — it's
# a separate exe with its own arbitrary window class — so matching by
# PROCESS ANCESTRY (see :func:`_pid_descends_from`) is the only way to find
# its window at all.
#
# An earlier revision tagged a matched companion window with the MAIN
# GAME's own appid, on the theory that gamescope would then treat it as
# "the same app" and let the Steam button cycle to it. On-device testing
# showed that theory wrong: gamescope's focus picker
# (``pick_primary_focus_and_override`` in steamcompmgr) selects exactly ONE
# window as ``focusWindow`` for a given appid — a second top-level window
# sharing that appid is never painted at all (not merely hidden) until the
# first one unmaps. That is exactly the "trainer only appears once the game
# closes" symptom this section fixes. Tagging each companion with its OWN
# dedicated appid instead makes gamescope treat it as a genuinely separate
# focusable app — reachable independently via the Steam button's app
# switcher — while it still runs in the identical Proton prefix/env as the
# main game (only the focus-identity env vars differ; see
# ``companions._run_one_companion``).
#
# One process per launcher process (``unifideck-launcher`` is a fresh
# process per launch), so a plain module-level dict + lock is sufficient.
_companion_pids_lock = threading.Lock()
_companion_pid_appids: dict[int, int] = {}

# Companion EXECUTABLE BASENAMES (lower-cased, e.g. ``trainer.exe``) mapped
# to the same dedicated fake appid as ``_companion_pid_appids``.
#
# This exists because ancestry matching alone DOES NOT WORK, as proven by an
# on-device process tree: we register the PID of the ``proton runinprefix``
# process we spawn, but the Wine process that actually OWNS the trainer's
# window is REPARENTED OUT of that subtree — its PPID is 1 / systemd, not
# our proton process. Concretely, with the trainer's window owned by PID
# 7098 and our registered companion PID being 7095, ``_ancestor_chain(7098)``
# yielded ``[7098, 1398]`` and terminated: 7095 never appeared, so
# :func:`_companion_appid_for_pid` always returned ``None``.
#
# The consequence was silent and exactly matched the long-standing bug
# report: rule 1 in :func:`_try_tag` never fired for a companion, the
# window fell through to the WM_CLASS rule, and — because a companion
# inherits the main game's ``SteamAppId`` and therefore gets the identical
# ``steam_app_<appid>`` WM_CLASS — it was tagged with the MAIN GAME's
# appid. Two top-level windows then shared one ``STEAM_GAME`` value, and
# gamescope's focus picker paints only one window per value, so the trainer
# stayed invisible until the game's window unmapped. No fake-appid VALUE
# could ever fix that, because the fake appid was never applied at all.
#
# Matching on the executable basename found in ``/proc/<pid>/cmdline``
# survives the reparent because it is a property of the process itself
# rather than of its (mutable) place in the process tree.
_companion_exe_appids: dict[str, int] = {}


def register_companion_exe(exe_name: str, appid: int) -> None:
    """Register a companion executable's basename + its dedicated fake appid.

    Primary companion-window match; see ``_companion_exe_appids``'s comment
    for why the PID/ancestry route (:func:`register_companion_pid`) cannot
    be relied on. Both are registered — ancestry stays as a fallback for
    companions whose window process happens NOT to be reparented.
    """
    with _companion_pids_lock:
        _companion_exe_appids[exe_name.lower()] = appid


def _companion_exe_appids_snapshot() -> dict[str, int]:
    with _companion_pids_lock:
        return dict(_companion_exe_appids)


def register_companion_pid(pid: int, appid: int) -> None:
    """Register a companion process's PID + its dedicated fake appid.

    Safe to call before or after :func:`start_window_tagger` — the tagger
    thread reads this map on every poll iteration, so a companion spawned
    seconds into the launch (its whole point — see
    ``_MIN_COMPANION_DELAY_SECONDS``) is picked up on the very next poll.
    """
    with _companion_pids_lock:
        _companion_pid_appids[pid] = appid


def _companion_pid_appids_snapshot() -> dict[int, int]:
    with _companion_pids_lock:
        return dict(_companion_pid_appids)


def _parent_pid(pid: int) -> int | None:
    """Return ``pid``'s PPID from ``/proc/<pid>/stat``, or ``None``."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        # Field 4 is ppid; field 2 (comm) may itself contain spaces/parens,
        # so split on the LAST ')' rather than naive whitespace splitting.
        after_comm = raw.rsplit(")", 1)[-1].split()
        return int(after_comm[1])
    except (OSError, ValueError, IndexError):
        return None


def _ancestor_chain(pid: int) -> Iterator[int]:
    """Yield ``pid`` then each ancestor PID up the ``/proc`` parent chain.

    Bounded to a sane depth so a ``/proc`` read error or PID-reuse cycle can
    never spin forever. Shared by :func:`_pid_descends_from` and
    :func:`_companion_appid_for_pid` so both walk identically.
    """
    current = pid
    for _ in range(64):
        yield current
        parent = _parent_pid(current)
        if parent is None or parent == current or parent <= 1:
            return
        current = parent


def _pid_descends_from(pid: int, roots: set[int]) -> bool:
    """Whether ``pid`` IS one of ``roots`` or a descendant of one of them."""
    if not roots:
        return False
    return any(p in roots for p in _ancestor_chain(pid))


def _companion_appid_for_pid(pid: int, appid_map: dict[int, int]) -> int | None:
    """Return the dedicated appid of the registered companion ``pid`` descends from.

    Walks the ``/proc`` parent chain upward via :func:`_ancestor_chain`, same
    as :func:`_pid_descends_from`, but returns the MATCHED companion's own
    appid rather than a bool — each companion in a multi-companion launch is
    tagged with its own distinct appid, never the main game's (see the
    module-level ``_companion_pid_appids`` comment for why).
    """
    if not appid_map:
        return None
    for p in _ancestor_chain(pid):
        if p in appid_map:
            return appid_map[p]
    return None


def _process_cmdline(pid: int) -> str:
    """Return ``pid``'s full command line as one string, or ``""`` on error.

    ``/proc/<pid>/cmdline`` is NUL-separated; joined with spaces here since
    callers only ever substring-search it. Deliberately preferred over
    ``/proc/<pid>/comm``, which the kernel truncates to 15 characters — many
    trainer executables have names far longer than that (e.g. ``Ghost of
    Tsushima v1.0 Plus 33 Trainer.exe``), so ``comm`` alone would silently
    fail to match them.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")


def _process_comm(pid: int) -> str:
    """Return ``pid``'s ``comm`` (kernel-truncated to 15 chars), or ``""``."""
    try:
        return Path(f"/proc/{pid}/comm").read_text(
            encoding="utf-8", errors="replace",
        ).strip()
    except OSError:
        return ""


def _companion_appid_for_exe(pid: int, exe_map: dict[str, int]) -> int | None:
    """Return the dedicated appid if ``pid``'s executable is a known companion.

    Matches a registered companion basename against ``pid``'s command line.
    A Wine process's cmdline holds the WINDOWS-style path it was started
    with (``X:\\Games\\...\\Trainer.exe``), so a plain case-insensitive
    substring test on the basename is enough — no path normalisation needed.

    ``comm`` is consulted only as a fallback for an unreadable cmdline, and
    then only when it is NOT truncated (shorter than the kernel's 15-char
    limit), so a truncated name can never partially match — and in
    particular can never match a companion whose name merely SHARES A
    PREFIX with the main game's executable, which is a realistic case:
    trainers are commonly named after the game they patch.
    """
    if not exe_map:
        return None
    cmdline = _process_cmdline(pid).lower()
    if cmdline:
        for name, appid in exe_map.items():
            if name in cmdline:
                return appid
        return None
    comm = _process_comm(pid).lower()
    if comm and len(comm) < 15:
        return exe_map.get(comm)
    return None


def _find_umu_zipapp() -> Path | None:
    """Return path to the umu zipapp bundled with this plugin."""
    # infrastructure/ → proton/ → launcher/ → unifideck/ → py_modules/ → plugin_root/
    here = Path(__file__).resolve().parent
    plugin_root = here.parents[4]
    candidate = plugin_root / "bin" / "umu" / "umu" / "umu_run.py"
    if candidate.is_file():
        return candidate
    return None


def _tag_windows(appid: int, stop_event: threading.Event) -> None:
    opened = _open_tagger_display()
    if opened is None:
        return
    d, xlib_x, atom_steam_game, atom_wm_pid = opened

    root = d.screen().root
    # Listen for new windows on :0 (gamescope compositor)
    root.change_attributes(event_mask=xlib_x.SubstructureNotifyMask)
    d.flush()

    wm_class_str = f"steam_app_{appid}"
    tagged: set[int] = set()
    deadline = time.monotonic() + _MAX_RUNTIME
    logger.info("[gamescope_tagger] watching :0 for WM_CLASS=%s", wm_class_str)

    # Also tag any windows that already exist on :0 before we started listening
    try:
        for child in root.query_tree().children:
            _try_tag(d, child, atom_steam_game, atom_wm_pid, wm_class_str, appid, tagged)
    except Exception as e:
        logger.debug("[gamescope_tagger] initial scan error: %s", e)

    _watch_loop(
        d, xlib_x, stop_event, deadline, atom_steam_game, atom_wm_pid,
        wm_class_str, appid, tagged,
    )

    d.close()
    logger.info("[gamescope_tagger] done, tagged %d window(s)", len(tagged))


def _open_tagger_display() -> tuple[Any, Any, int, int] | None:
    """Open the gamescope X display and intern the atoms the tagger needs.

    Returns ``(display, X, atom_steam_game, atom_wm_pid)`` or ``None`` when
    the umu zipapp (which carries Xlib) is missing or the display cannot be
    opened. Xlib rides along inside the umu zipapp — see :func:`_find_umu_zipapp`.
    """
    umu_zip = _find_umu_zipapp()
    if umu_zip is None:
        logger.warning("[gamescope_tagger] umu zipapp not found, cannot tag")
        return None
    zip_str = str(umu_zip)
    if zip_str not in sys.path:
        sys.path.insert(0, zip_str)
    try:
        from Xlib import X
        from Xlib.display import Display
    except ImportError as e:
        logger.warning("[gamescope_tagger] Xlib import failed: %s", e)
        return None
    try:
        d = Display(_DISPLAY)
    except Exception as e:
        logger.warning("[gamescope_tagger] cannot open display %s: %s", _DISPLAY, e)
        return None
    atom_steam_game = d.intern_atom("STEAM_GAME", only_if_exists=False)
    atom_wm_pid = d.intern_atom("_NET_WM_PID", only_if_exists=True)
    return d, X, atom_steam_game, atom_wm_pid


def _watch_loop(
    d: Any, xlib_x: Any, stop_event: threading.Event, deadline: float,
    atom_steam_game: int, atom_wm_pid: int,
    wm_class_str: str, appid: int, tagged: set[int],
) -> None:
    """Tag matching windows as they appear, until stopped or timed out."""
    while not stop_event.is_set() and time.monotonic() < deadline:
        while d.pending_events():
            ev = d.next_event()
            if ev.type != xlib_x.CreateNotify:
                continue
            try:
                _try_tag(
                    d, ev.window, atom_steam_game, atom_wm_pid,
                    wm_class_str, appid, tagged,
                )
            except Exception as e:
                logger.debug("[gamescope_tagger] event error: %s", e)
        time.sleep(_POLL_INTERVAL)


def _window_pid(window: Any, atom_wm_pid: int) -> int | None:
    """Read a window's ``_NET_WM_PID`` property, or ``None`` if unset."""
    if not atom_wm_pid:
        return None
    try:
        prop = window.get_full_property(atom_wm_pid, 0)
        if prop and prop.value:
            return int(prop.value[0])
    except Exception as e:
        logger.debug("[gamescope_tagger] _NET_WM_PID read failed: %s", e)
    return None


def _matches_main_game(window: Any, wm_class_str: str) -> bool:
    """Whether ``window``'s WM_CLASS marks it as the main game window."""
    cls = window.get_wm_class()
    return bool(cls) and wm_class_str in cls


def _companion_match(win_pid: int | None) -> tuple[int | None, str]:
    """Return ``(companion_appid, rule)`` for a window's owning process.

    Rule 1: the process runs a registered companion exe
    (:func:`register_companion_exe`). Rule 2 (fallback): it descends from a
    registered companion PID (:func:`register_companion_pid`). ``(None, "")``
    when neither matches or the PID is unknown.
    """
    if win_pid is None:
        return None, ""
    exe_appids = _companion_exe_appids_snapshot()
    appid = _companion_appid_for_exe(win_pid, exe_appids)
    if appid is not None:
        return appid, "companion-exe"
    companion_appids = _companion_pid_appids_snapshot()
    appid = _companion_appid_for_pid(win_pid, companion_appids)
    if appid is not None:
        return appid, "companion-ancestry"
    return None, ""


def _resolve_tag_appid(
    window: Any, win_pid: int | None, wm_class_str: str, appid: int,
) -> tuple[int | None, str, bool]:
    """Decide which ``STEAM_GAME`` appid (if any) ``window`` should get.

    Returns ``(tag_appid, match_rule, skip)``. ``skip`` is True when a
    companion window must be left UNTAGGED (``_COMPANION_TAG_MODE == "skip"``).
    Match rules, in order — see :func:`_try_tag` for the full rationale:
      1./2. registered companion exe or PID ancestry (:func:`_companion_match`);
      3. last resort: WM_CLASS matches the main game.
    """
    tag_appid, match_rule = _companion_match(win_pid)
    if tag_appid is not None:
        if _COMPANION_TAG_MODE == "skip":
            return tag_appid, match_rule, True
        if _COMPANION_TAG_MODE == "game":
            # Discard the companion's dedicated fake appid and use the main
            # game's real one — the only appid gamescope will composite at
            # all, because it is the only one Steam knows.
            return appid, match_rule + "+game-appid", False
        return tag_appid, match_rule, False
    if _matches_main_game(window, wm_class_str):
        return appid, "main-game-wm-class", False
    return None, "", False


def _try_tag(
    d: Any, window: Any, atom_steam_game: int, atom_wm_pid: int,
    wm_class_str: str, appid: int, tagged: set[int],
) -> None:
    """Tag ``window`` with ``STEAM_GAME=<its appid>`` if it's a registered
    companion (trainer/cheat tool) window OR the main game.

    Three match rules, checked in this specific order:
      1. FIRST: the window's owning process runs an executable registered
         via :func:`register_companion_exe` — tagged with THAT companion's
         own dedicated appid (never the main game's — see the module-level
         ``_companion_pid_appids`` comment for why sharing the game's appid
         breaks gamescope's focus picker).
      2. Fallback: that process descends from a PID registered via
         :func:`register_companion_pid`. Kept only for companions whose
         window process is NOT reparented away from us; it cannot be the
         primary rule, since a reparent is the normal Wine behaviour (see
         ``_companion_exe_appids``'s comment for the measured evidence).
      3. Only if neither companion rule matched: WM_CLASS matches the main
         game (the original behaviour) — tagged with the main game's
         ``appid`` param.

    ORDER MATTERS: a companion process inherits the main game's own
    ``SteamAppId``/``SteamGameId`` env vars (deliberately — see
    ``companions.start_companions``'s comment for why popping or
    overriding them regressed gamescope refusing to composite the window
    at all), so Wine gives its window the SAME ``steam_app_<main game>``
    WM_CLASS as the real game window. WM_CLASS therefore CANNOT tell the
    two apart and must stay the last resort: whenever it ran first, a
    companion window was claimed as the main game's own window and got the
    game's appid, leaving gamescope with two identically-tagged windows and
    painting only one — the trainer stayed hidden until the game's window
    closed. Both companion rules run before it so that only a window from
    a process that is NOT a registered companion can reach rule 3.

    The per-window result is logged with the rule that matched, so a future
    "trainer window not focusable" report can be diagnosed from the launch
    log alone instead of needing a live process tree off the device.
    """
    wid = window.id
    if wid in tagged:
        return
    win_pid = _window_pid(window, atom_wm_pid)
    tag_appid, match_rule, skip = _resolve_tag_appid(
        window, win_pid, wm_class_str, appid,
    )
    if skip:
        tagged.add(wid)
        logger.info(
            "[gamescope_tagger] companion window left UNTAGGED on "
            "wid=0x%x (rule=%s, pid=%s, cmd=%r)",
            wid, match_rule, win_pid,
            _process_cmdline(win_pid)[:160] if win_pid is not None else "",
        )
        return
    if tag_appid is None:
        return
    window.change_property(atom_steam_game, d.get_atom("CARDINAL"), 32, [tag_appid])
    d.flush()
    tagged.add(wid)
    logger.info(
        "[gamescope_tagger] STEAM_GAME=%d set on wid=0x%x (rule=%s, pid=%s, cmd=%r)",
        tag_appid, wid, match_rule, win_pid,
        _process_cmdline(win_pid)[:160] if win_pid is not None else "",
    )


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
