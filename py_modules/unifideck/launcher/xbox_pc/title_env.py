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


def title_env(store_id: str | None, *, backcompat: bool = False) -> dict[str, str]:
    """The extra env for ``store_id``; empty for any other title.

    Every Xbox back-compat package runs the same emulator, so ``backcompat``
    turns on what it needs (:data:`_BACKCOMPAT`) whatever the product id.
    """
    env = dict(_BACKCOMPAT) if backcompat else {}
    env.update(TITLE_ENV.get((store_id or "").upper(), {}))
    return env
