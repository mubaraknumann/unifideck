"""launcher/xbox_pc/title_env.py — env a known title needs from the runtime.

Some runtime features change behaviour every title would otherwise share,
so the runtime keeps them off unless the game's env turns them on. This
table turns one on for the titles verified to need it, keyed by Store
product id (the ``StoreId`` of ``MicrosoftGame.config``).

Applied before the user's launch options, so a user can still override an
entry (``XODUS_GAMEINPUT_V3=0 %command%``).

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

from .package_identity import PackageNames

#: GameInput v3 gamepads over XInput (runtime 0.1.15+, ISSUES.md I-059). A
#: title that asks for IGameInput v3 and finds none takes a fallback that
#: crashes at startup. Off by default: a title that works through its own
#: XInput fallback could lose keyboard and mouse on a gamepad-only GameInput.
_GAMEINPUT_V3 = {"XODUS_GAMEINPUT_V3": "1"}

#: What Xbox back-compat packages need from the runtime (I-081, runtime 0.1.20+):
#: GameInput v3, special user APCs delivered to running threads (the emulator's
#: interrupts between guest processors), and SetFileInformationByHandle
#: (FileAllocationInfo) succeeding. The runtime keeps the last two off for every
#: other process, so a game that never asked for them behaves as on stock Wine.
_BACKCOMPAT = {
    **_GAMEINPUT_V3,
    "XODUS_SPECIAL_USER_APC": "1",
    "XODUS_FILE_ALLOCATION_INFO": "1",
}

TITLE_ENV: dict[str, dict[str, str]] = {
    "C261457LCNMJ": _GAMEINPUT_V3,
}

#: Titles that branch on their Windows package identity (runtime 0.1.21+,
#: ISSUES.md I-085). Without one, Tell Me Why runs as its Steam edition and
#: quits when Steam refuses it. Off for every other title: a game that finds
#: a package may take Store code paths the runtime does not serve.
PACKAGE_IDENTITY_TITLES = frozenset({"9NBL0XKVCN5L"})


def title_env(
    store_id: str | None, *, backcompat: bool = False, package: PackageNames | None = None,
) -> dict[str, str]:
    """The extra env for ``store_id``; empty for any other title.

    Every Xbox back-compat package runs the same emulator, so ``backcompat``
    turns on what it needs (:data:`_BACKCOMPAT`) whatever the product id.
    ``package`` is the game's own package names, passed to the runtime for
    the titles in :data:`PACKAGE_IDENTITY_TITLES`.
    """
    key = (store_id or "").upper()
    env = dict(_BACKCOMPAT) if backcompat else {}
    env.update(TITLE_ENV.get(key, {}))
    if package is not None and key in PACKAGE_IDENTITY_TITLES:
        env["XODUS_PACKAGE_FAMILY_NAME"] = package.family
        env["XODUS_PACKAGE_FULL_NAME"] = package.full
    return env
