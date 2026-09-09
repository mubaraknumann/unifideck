"""Finding a wrapper store's vendor clients across every prefix.

py_modules/unifideck/launcher/proton/handlers/wrapper_clients.py

The ``/proc`` primitives here were Battle.net's, scoped to one prefix at a
time: "is the client up in *this* prefix". That is the wrong shape for the
question that actually causes session loss, which is the **inverse** — "is a
client up in some *other* prefix right now".

It matters because these clients are not per-prefix as far as the vendor is
concerned. Every prefix is a clone, so every client presents the same
client-instance id and the same token; two running at once both refresh that
token and one of them loses. On Battle.net the user hits it by opening the
Sign-In tile and then launching a game, and the symptom is
``BLZBNTBGS80000023`` on whichever client was second to look.

Everything runs on the **Linux side**, reading ``/proc``. That is an
anti-cheat hygiene rule, not incidental: Warden scans the game process's
memory, its loaded code, the Windows process list and its handle table.
Reading ``/proc/<pid>/cmdline`` and ``/environ`` touches none of those — it
never enters the prefix, never opens a handle to the game, and never appears
in the Windows process list.

Stdlib-only; runs under the SYSTEM python (3.10-3.14).
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

_WINEPREFIX = "WINEPREFIX="

# The vendor client's own Windows images, per store. Battle.net's CEF children
# are *all* named ``battle.net.exe`` and distinguished only by ``--type=``;
# there is no ``Battle.net Helper.exe`` process, despite that string appearing
# in command lines.
CLIENT_IMAGES: dict[str, frozenset[str]] = {
    "battlenet": frozenset({"battle.net.exe", "battle.net launcher.exe"}),
    "ubisoft": frozenset({"upc.exe", "ubisoftconnect.exe"}),
}

# Images that mean "this store's install is still making progress", beyond the
# client itself. Battle.net downloads through a separate ``Agent.exe`` that
# keeps going after the user closes the client window — so a liveness question
# asked only about ``CLIENT_IMAGES`` would call a live 12 GB download abandoned
# and cancel it. Ubisoft Connect has no such helper; its entry is empty rather
# than absent so the shape of the table says "asked and answered".
INSTALL_WORKER_IMAGES: dict[str, frozenset[str]] = {
    "battlenet": frozenset({"agent.exe"}),
    "ubisoft": frozenset(),
}

# What a teardown may actually SIGNAL — a SUBSET of CLIENT_IMAGES, and the
# difference is load-bearing.
#
# ``Battle.net Launcher.exe`` is excluded because it owns the prefix's
# wineserver. Signalling it alongside the client tears the Wine session down
# before ``battle.net.exe`` can flush the token it rotated during the run — and
# that token is a *registry* key, written on shutdown. Measured: including it
# stopped every post-play capture happening (``capture`` then silently declines
# at its "no newer session" branch), the auth prefix froze on a token Blizzard
# eventually invalidated, and the next launch opened on a sign-in prompt.
#
# Liveness must still count the Launcher — see ``client_running_in``, where
# over-reporting only costs a short wait. "Is it up" and "may I kill it" are
# different questions and this table is why they can no longer be confused.
CLIENT_TEARDOWN_IMAGES: dict[str, frozenset[str]] = {
    "battlenet": frozenset({"battle.net.exe"}),
    "ubisoft": frozenset({"upc.exe", "ubisoftconnect.exe"}),
}


def normalise_prefix(prefix: str | Path) -> str:
    """Canonical form for comparing WINEPREFIX values.

    umu rewrites the value to ``<prefix>/pfx/`` and ``pfx`` is a symlink to
    the prefix itself, so both spellings must compare equal.
    """
    try:
        return str(Path(prefix).resolve()).rstrip("/")
    except OSError:
        return str(prefix).rstrip("/")


def proc_field(pid: str, field: str) -> str:
    """One ``/proc/<pid>`` field as text, or empty when unreadable."""
    try:
        with Path(f"/proc/{pid}/{field}").open("rb") as handle:
            return handle.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""


def image_name(cmdline: str) -> str:
    """Windows image name from a Wine process command line, lowercased."""
    first = cmdline.split("\x00", 1)[0]
    if not first:
        return ""
    return first.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()


def pids() -> list[str]:
    """Every pid on the system."""
    try:
        return [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []


def wineprefix_of(pid: str) -> str | None:
    """Normalised ``WINEPREFIX`` of ``pid``, or None if it has none.

    Read as an exact ``WINEPREFIX=`` entry, never as a substring of the whole
    environ blob: ``STEAM_COMPAT_DATA_PATH`` and ``PROTONPATH`` carry the same
    path and would match a naive ``in environ`` test.
    """
    environ = proc_field(pid, "environ")
    if _WINEPREFIX not in environ:
        return None
    for entry in environ.split("\x00"):
        if entry.startswith(_WINEPREFIX):
            return normalise_prefix(entry.partition("=")[2])
    return None


def scan_prefix(prefix: str | Path) -> list[tuple[str, str, str]]:
    """``(pid, image, cmdline)`` for every **Windows** process in ``prefix``.

    Scoped by ``WINEPREFIX`` so a client running for another of the store's
    games is never mistaken for this one's.

    Restricted to ``.exe`` images on purpose. ``WINEPREFIX`` is inherited by
    the whole Linux-side umu chain — ``srt-bwrap``, ``pv-adverb``,
    ``umu-run``, the Proton ``python3`` — so those wrappers used to read as
    game processes. Measured on-device: a phase C ``srt-bwrap`` was reported
    as "game process appeared after 0s", defeating the silent-failure
    detector and leaving the watcher following a pid that is not the game.
    """
    target = normalise_prefix(prefix)
    found: list[tuple[str, str, str]] = []
    for pid in pids():
        if wineprefix_of(pid) != target:
            continue
        cmdline = proc_field(pid, "cmdline")
        image = image_name(cmdline)
        if image.endswith(".exe"):
            found.append((pid, image, cmdline))
    return found


def client_running_in(store: str, prefix: str | Path) -> bool:
    """True while any of ``store``'s client processes is alive in ``prefix``.

    Deliberately a superset of Battle.net's own ``client_running``, which
    counts only ``battle.net.exe``: this one also counts the launcher
    executable. Callers use it to decide when a client has *fully* exited and
    its rotated token is safe to read, and there over-reporting liveness only
    costs a short wait, while under-reporting it reads a torn vault.
    """
    images = CLIENT_IMAGES.get(store)
    if not images:
        return False
    return any(image in images for _, image, _ in scan_prefix(prefix))


def install_active_in(store: str, prefix: str | Path) -> bool:
    """True while ``store`` is still installing into ``prefix``.

    Broader than :func:`client_running_in` by exactly the store's downloader
    images. Feeds the install watchdogs, which can only ever *end* an install,
    so the question has to be "is anything still working" rather than "is the
    window up": Battle.net's ``Agent.exe`` finishes a download long after the
    user has closed the client, and counting only the client would abandon it.
    """
    images = CLIENT_IMAGES.get(store, frozenset())
    images |= INSTALL_WORKER_IMAGES.get(store, frozenset())
    if not images:
        return False
    return any(image in images for _, image, _ in scan_prefix(prefix))


def teardown_pids_in(store: str, prefix: str | Path) -> list[str]:
    """PIDs a teardown of ``store`` in ``prefix`` may signal.

    Keyed on :data:`CLIENT_TEARDOWN_IMAGES`, deliberately narrower than the
    liveness question. Within that set every process counts whatever its
    ``--type=``: Battle.net measured a case where signalling only the main
    process left the surviving ``--type=gpu-process`` and ``--type=utility``
    children behind, so the dead session stayed in the prefix and the next
    launch stacked a second client on top of it.
    """
    images = CLIENT_TEARDOWN_IMAGES.get(store)
    if not images:
        return []
    return [pid for pid, image, _ in scan_prefix(prefix) if image in images]


def signal_all(pids: list[str], sig: int) -> None:
    """Signal each pid individually. Never ``killpg``.

    The process group here contains our own launcher (and, when Steam wraps
    us, more besides), and group-killing a store's processes is exactly how
    the legendary cancel path once took down its own subprocess tree.
    """
    for pid in pids:
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(pid), sig)


def terminate(
    pids: list[str],
    survivors_of: Callable[[], list[str]],
    timeout: float,
    *,
    label: str = "wrapper",
) -> int:
    """SIGTERM ``pids``, then SIGKILL whatever ``survivors_of`` still reports.

    SIGTERM first so the client can flush its session — the token it rotated
    during this run lives in the vendor's own vault and a SIGKILL can lose it.
    SIGKILL only for what is still alive at the deadline.
    """
    if not pids:
        return 0
    signal_all(pids, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not survivors_of():
            logger.info("[%s] %d process(es) stopped cleanly", label, len(pids))
            return len(pids)
        time.sleep(0.5)

    survivors = survivors_of()
    for pid in survivors:
        logger.warning("[%s] pid %s ignored SIGTERM — killing", label, pid)
    signal_all(survivors, signal.SIGKILL)
    return len(pids)


def kill_client(
    store: str, prefix: str | Path, *, timeout: float = 15.0,
) -> int:
    """Close ``store``'s vendor client in ``prefix``. Returns how many were signalled.

    Scoped by ``WINEPREFIX`` and to :data:`CLIENT_TEARDOWN_IMAGES`, so it never
    touches the game, Wine's infrastructure, the store's downloader, or the
    process that owns the prefix's wineserver. Reading ``/proc`` works across
    gamescope sessions, which a window-manager approach does not — the reason
    Ubisoft previously reached for a global ``pkill -f upc.exe``. That global
    form also closed a client belonging to a *different* game; this one cannot.
    """
    pids = teardown_pids_in(store, prefix)
    if not pids:
        return 0
    logger.info(
        "[%s] stopping %d client process(es) in %s", store, len(pids), prefix,
    )
    return terminate(
        pids, lambda: teardown_pids_in(store, prefix), timeout, label=store,
    )


def live_client_prefixes(
    store: str, *, exclude: tuple[str | Path, ...] = (),
) -> list[Path]:
    """Prefixes currently running ``store``'s vendor client.

    One pass over ``/proc``, grouping by ``WINEPREFIX`` — the inverse of
    :func:`scan_prefix`, and the only way to answer "is a client already up
    somewhere else" without knowing every prefix path in advance (games
    installed to removable media live outside our directory).

    Returned paths are the normalised ``WINEPREFIX`` values, which for umu is
    ``<prefix>/pfx`` — a self-symlink to the prefix, so they resolve to the
    same directory the caller passed in.
    """
    images = CLIENT_IMAGES.get(store)
    if not images:
        return []
    skip = {normalise_prefix(p) for p in exclude}
    found: dict[str, Path] = {}
    for pid in pids():
        prefix = wineprefix_of(pid)
        if prefix is None or prefix in skip or prefix in found:
            continue
        if image_name(proc_field(pid, "cmdline")) in images:
            found[prefix] = Path(prefix)
    return list(found.values())


# ── Opening the client: one toast, one wording ──────────────────────

# The client the user actually sees, per store. Interpolated into the
# shared toast so the two stores differ only by name.
_CLIENT_NAMES = {"ubisoft": "Ubisoft Connect", "battlenet": "Battle.net"}


def announce_client_open(store: str) -> None:
    """Toast that ``store``'s client is opening. Nothing more.

    One line, one variable. It used to vary by store *and* editorialise:
    Ubisoft said "Signing in to Ubisoft Connect / Sign in there, then
    return." while Battle.net said "Battle.net Sign-In / Opening the
    Battle.net client so you can sign in…" — two different voices for
    the same event, both telling the user what to do next as though
    that needed explaining, and both still saying "sign in" when the
    user had pressed the cart.

    No title key, so this renders as a title with no body (see
    ``showLauncherToast``). Kept here rather than in either handler so
    a third wrapper store cannot reintroduce the divergence.
    """
    from unifideck.launcher.frontend_bridge import launcher_toast

    launcher_toast(
        i18n_key="toasts.launcher.wrapperOpening",
        i18n_params={"client": _CLIENT_NAMES.get(store, store)},
    )
