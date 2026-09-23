"""launcher/types/context.py — Immutable launch request context."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unifideck.launcher.wrapper_stores import is_wrapper_store

KNOWN_STORES: tuple[str, ...] = (
    "epic",
    "gog",
    "amazon",
    "microsoft",
    "ubisoft",
    "battlenet",
)


#: File types the launcher hands to Proton/umu. Anything else is exec'd as a
#: native Linux target (ELF, shell script, AppImage).
WINDOWS_EXE_SUFFIXES: tuple[str, ...] = (".exe", ".cmd", ".bat")


def is_windows_executable(path: str | Path) -> bool:
    """Whether ``path`` names a Windows launch target (by extension).

    The one definition of the native/Windows split: the launcher routes on
    it, and the install-time prefix warmup reads it so a native install does
    not get a Proton prefix it will never use.
    """
    return str(path).lower().endswith(WINDOWS_EXE_SUFFIXES)


@dataclass(frozen=True)
class LaunchContext:
    """Immutable description of a single launch request.
    Built once by the dispatcher from argv + games.map + env.
    Passed by value to every downstream module. Never mutated.

    Path fields (``exe_path``, ``work_dir``, ``plugin_dir``) are
    strict ``Path`` (not ``Path | str``) — the dispatcher wraps
    incoming strings exactly once at construction time, and
    every consumer downstream relies on Path-only methods
    (``.is_file()``, ``.parent``, ``.chmod``, ``.name``). Keeping
    the union here used to force ``str`` checks in every
    consumer; tightening to ``Path`` is the cleaner contract.
    """
    store: str
    game_id: str
    exe_path: Path
    work_dir: Path
    plugin_dir: Path
    raw_options: str = ""
    env_overrides: dict[str, str] = field(default_factory=dict)
    is_launch_action: bool = True
    auth_store: str | None = None
    # Non-launch action kind, when ``is_launch_action`` is False.
    # ``None``/"auth" → store sign-in; "install" → Ubisoft opens UPC to
    # install a game via RunGame (gamescope session). The launcher service
    # branches on this for the non-launch path.
    action: str | None = None
    bypass_circuit_breaker: bool = False
    steam_app_id: str | None = None
    # Browser games (``launcher/browser_games``): the URL to open, and
    # ``"stream"`` (xCloud) or ``"web"`` (an HTML5 game). Their own fields
    # because ``work_dir`` is a ``Path``, which collapses ``https://``.
    browser_url: str | None = None
    browser_kind: str = "web"
    @property
    def is_browser_game(self) -> bool:
        """Played in an Edge window at ``browser_url``; nothing installed."""
        return self.browser_url is not None
    @property
    def is_windows_game(self) -> bool:
        """Check whether windows game."""
        # Wrapper stores launch through a vendor client; their games
        # legitimately carry no exe_path, so the extension sniff below
        # would misroute them to the native path.
        if is_wrapper_store(self.store):
            return True
        return is_windows_executable(self.exe_path)
    @property
    def is_native_linux(self) -> bool:
        """Check whether native linux."""
        return not self.is_browser_game and not self.is_windows_game
    @property
    def game_key(self) -> str:
        """Game key."""
        return f"{self.store}:{self.game_id}"
    def to_log_dict(self) -> dict[str, Any]:
        """To log dict."""
        return {
            "store": self.store,
            "game_id": self.game_id,
            "exe_path": str(self.exe_path),
            "work_dir": str(self.work_dir),
            "is_browser_game": self.is_browser_game,
            "is_windows_game": self.is_windows_game,
            "is_launch_action": self.is_launch_action,
            "auth_store": self.auth_store,
            "action": self.action,
            "bypass_circuit_breaker": self.bypass_circuit_breaker,
        }

@dataclass
class RuntimeState:
    """Mutable companion to LaunchContext.
    Collects everything the launcher **derives**.
    """
    proton_path: Path | None = None
    proton_tool_id: str | None = None
    prefix_path: Path | None = None
    umu_store_code: str | None = None
    umu_id: str | None = None
    umu_wrapper: Path | None = None
    python_bin: Path | None = None
    # No ``wrappers`` field. Steam applies wrapper words itself, BEFORE our
    # launcher is exec'd — measured on this Deck (audit §2.9): with
    # ``env %command% epic:Salt`` Steam ran the launcher *under* ``env`` and
    # still delivered ``argv[1] = epic:Salt``. So ``mangohud %command% <id>``
    # already works today with no plugin code, and by the time we hold an
    # argv there is nothing left to wrap. Six argv builders used to prepend
    # this list; it could only ever be empty. Audit register item 23b.
    game_args: list[str] = field(default_factory=list)
    lsfg_requested: bool = False
    game_exit_code: int | None = None
    terminated_by_signal: bool = False
    def to_log_dict(self) -> dict[str, Any]:
        """To log dict."""
        return {
            "proton_path": str(self.proton_path) if self.proton_path else None,
            "proton_tool_id": self.proton_tool_id,
            "prefix_path": str(self.prefix_path) if self.prefix_path else None,
            "umu_store_code": self.umu_store_code,
            "umu_id": self.umu_id,
            "lsfg_requested": self.lsfg_requested,
            "game_exit_code": self.game_exit_code,
            "terminated_by_signal": self.terminated_by_signal,
            "game_args_count": len(self.game_args),
        }

    # Compat field used by LauncherService
    rc: int = 1
    started_at: float = 0.0
