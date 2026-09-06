"""steam/current_user.py — the single source of truth for the active Steam user.

Every module that needs "who is logged into Steam and where do their
per-user files live" resolves it HERE, so the whole plugin agrees on one
answer. Previously this was scattered across five+ call sites with four
independent ``loginusers.vdf`` parsers and an unvalidated directory-mtime
guess — the guess wrote ``shortcuts.vdf`` into the wrong ``userdata/<id>/``
folder ("synced N games, Steam shows 0"), and because our own writes bumped
that folder's mtime the wrong guess stuck on every boot.

Resolution order (best-available wins; every candidate is validated: the
``userdata/<id>`` dir must exist and not be a reserved dir):

1. **Frontend-authoritative** — the value the live Steam client pushed via
   ``set_authoritative`` (persisted in config as ``steam.active_user``). The
   frontend runs *inside* Steam and knows the logged-in user with certainty;
   this is the only 100%-correct source. Read config-free from disk when no
   ConfigManager is available (the out-of-process launcher / staticmethods).
2. **loginusers.vdf MostRecent** — correct when Steam wrote a clean record.
3. **registry.vdf AutoLoginUser** — Steam's live auto-login account *name*,
   mapped to an id via ``loginusers.vdf``'s ``AccountName``. Name-based, so
   immune to the mtime trap.
4. **localconfig.vdf mtime** — rank candidate userdata dirs by the mtime of
   ``config/localconfig.vdf`` (Steam rewrites it while running; our shortcut
   writes do NOT touch it — unlike the *directory* mtime). Kills the
   self-reinforcing trap.
5. **directory mtime** — last-ditch only; ranked below everything.

Returns ``None`` when no real user can be confirmed (only the guest ``"0"``
dir exists, or the userdata dir is missing). Callers MUST treat ``None`` as
"defer the write and tell the user" — never fall back to guest ``"0"`` for a
real ``shortcuts.vdf`` write, which Steam ignores.

Launcher-safe: no aiohttp / backend-only imports at module load, so the
out-of-process launcher (system Python) can import and use it. The ``vdf``
lib is imported lazily and only for the loginusers ``AccountName`` map;
everything on the hot path uses the regex primitives in ``utils.vdf_compat``.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

logger = logging.getLogger(__name__)

# Steam's special / non-real userdata directories — never a valid active user.
_RESERVED_USERDATA_DIRS = frozenset({"0", "anonymous", "ac"})

# Config key holding the frontend-confirmed account id (persisted, survives reboot).
CONFIG_ACTIVE_USER_KEY = "steam.active_user"

# registry.vdf lives under the ``~/.steam`` root (a symlink), NOT under the
# resolved steam_root, and moves under Flatpak. Probe a small known set.
_REGISTRY_CANDIDATES = (
    "~/.steam/registry.vdf",
    "~/.steam/steam/registry.vdf",
    "~/.var/app/com.valvesoftware.Steam/.steam/registry.vdf",
)
_AUTOLOGIN_RE = re.compile(r'"AutoLoginUser"\s+"([^"]+)"')
# loginusers user block: id + flat body (no nested braces), matching vdf_compat.
_LOGINUSERS_USER_RE = re.compile(r'"(\d{6,})"\s*\{([^{}]*)\}', re.DOTALL)
_ACCOUNT_NAME_RE = re.compile(r'"AccountName"\s+"([^"]+)"')


def account_id_from_steam64(steam64_id_str: str) -> str | None:
    """Convert a SteamID64 string to the 32-bit ``userdata/`` folder name."""
    try:
        return str(int(steam64_id_str) & 0xFFFFFFFF)
    except (TypeError, ValueError):
        return None


def _valid_user_dir(steam_root: Path, account_id: str | None) -> str | None:
    """Return ``account_id`` iff it is real and its ``userdata/<id>`` exists."""
    if not account_id or account_id in _RESERVED_USERDATA_DIRS:
        return None
    if not account_id.isdigit():
        return None
    if (steam_root / "userdata" / account_id).is_dir():
        return account_id
    return None


def _read_persisted_user(config: ConfigManager | None) -> str | None:
    """The frontend-confirmed id — from config if given, else read from disk.

    The out-of-process launcher and the ``auth_shortcuts`` staticmethod have
    no ConfigManager, so fall back to reading the user config layer directly.
    """
    if config is not None:
        val = config.get(CONFIG_ACTIVE_USER_KEY)
        return str(val) if val else None
    return _read_persisted_user_from_disk()


def _read_persisted_user_from_disk() -> str | None:
    """Config-free read of ``steam.active_user`` from the user config file."""
    import json
    cfg = Path("~/.config/unifideck/config.json").expanduser()
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # Support both nested ({"steam": {"active_user": ...}}) and flat keys.
    steam = data.get("steam") if isinstance(data, dict) else None
    if isinstance(steam, dict) and steam.get("active_user"):
        return str(steam["active_user"])
    flat = data.get(CONFIG_ACTIVE_USER_KEY) if isinstance(data, dict) else None
    return str(flat) if flat else None


def _from_loginusers(steam_root: Path) -> str | None:
    """MostRecent account id from ``loginusers.vdf`` (regex, launcher-safe)."""
    from unifideck.utils.vdf_compat import _most_recent_login
    _, user = _most_recent_login(steam_root / "config" / "loginusers.vdf")
    return _valid_user_dir(steam_root, user)


def _login_name_to_account_id(steam_root: Path, login_name: str) -> str | None:
    """Map a Steam login/account NAME to its account id via loginusers.vdf."""
    login = steam_root / "config" / "loginusers.vdf"
    try:
        text = login.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    target = login_name.strip().lower()
    for steam64, body in _LOGINUSERS_USER_RE.findall(text):
        m = _ACCOUNT_NAME_RE.search(body)
        if m and m.group(1).strip().lower() == target:
            return account_id_from_steam64(steam64)
    return None


def _from_registry_autologin(steam_root: Path) -> str | None:
    """Account id for ``registry.vdf``'s ``AutoLoginUser`` (Steam's live user)."""
    for cand in _REGISTRY_CANDIDATES:
        p = Path(cand).expanduser()
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = _AUTOLOGIN_RE.search(text)
        if not m:
            continue
        account_id = _login_name_to_account_id(steam_root, m.group(1))
        valid = _valid_user_dir(steam_root, account_id)
        if valid:
            return valid
    return None


def _candidate_user_dirs(steam_root: Path) -> list[Path]:
    """Real ``userdata/<digits>`` dirs (reserved dirs excluded)."""
    userdata = steam_root / "userdata"
    if not userdata.is_dir():
        return []
    return [
        e for e in userdata.iterdir()
        if e.is_dir() and e.name.isdigit()
        and e.name not in _RESERVED_USERDATA_DIRS
    ]


def _rank_by_mtime(dirs: list[Path], rel: str | None) -> str | None:
    """Highest-mtime dir name, ranking by ``dir/rel`` when ``rel`` is set."""
    best: tuple[float, str] | None = None
    for d in dirs:
        target = (d / rel) if rel else d
        try:
            mtime = target.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, d.name)
    return best[1] if best is not None else None


def _from_localconfig_mtime(steam_root: Path) -> str | None:
    """Rank userdata dirs by ``config/localconfig.vdf`` mtime (Steam-written).

    Unlike the directory mtime, ``localconfig.vdf`` is untouched by our
    shortcut writes, so this does not self-reinforce a wrong guess.
    """
    dirs = _candidate_user_dirs(steam_root)
    return _valid_user_dir(
        steam_root, _rank_by_mtime(dirs, "config/localconfig.vdf"),
    )


def _from_dir_mtime(steam_root: Path) -> str | None:
    """Last-ditch: newest userdata *directory* mtime (the legacy heuristic)."""
    dirs = _candidate_user_dirs(steam_root)
    return _valid_user_dir(steam_root, _rank_by_mtime(dirs, None))


def resolve(steam_root: Path, config: ConfigManager | None = None) -> str | None:
    """Resolve the active Steam account id, best-available signal wins.

    ``None`` when no real user can be confirmed — the caller must defer the
    write and surface a warning rather than write to the guest ``"0"`` dir.
    """
    persisted = _valid_user_dir(steam_root, _read_persisted_user(config))
    if persisted:
        logger.info("[CurrentUser] active user from frontend/config: %s", persisted)
        return persisted

    for label, fn in (
        ("loginusers.vdf", _from_loginusers),
        ("registry.vdf AutoLoginUser", _from_registry_autologin),
        ("localconfig.vdf mtime", _from_localconfig_mtime),
        ("directory mtime (last-ditch)", _from_dir_mtime),
    ):
        user = fn(steam_root)
        if user is not None:
            logger.info("[CurrentUser] active user from %s: %s", label, user)
            return user

    logger.warning(
        "[CurrentUser] could not confirm the active Steam user under %s — "
        "deferring shortcuts.vdf / artwork writes (they would be invisible to Steam)",
        steam_root,
    )
    return None


# ── Per-user path helpers ──────────────────────────────────────────

def user_config_dir(steam_root: Path, account_id: str) -> Path:
    """``<steam_root>/userdata/<id>/config`` — the per-user config dir."""
    return steam_root / "userdata" / account_id / "config"


def shortcuts_path(steam_root: Path, account_id: str) -> str:
    return str(user_config_dir(steam_root, account_id) / "shortcuts.vdf")


def grid_dir(steam_root: Path, account_id: str) -> str:
    return str(user_config_dir(steam_root, account_id) / "grid")


def localconfig_path(steam_root: Path, account_id: str) -> str:
    return str(user_config_dir(steam_root, account_id) / "localconfig.vdf")


# ── Runtime re-bind coordinator ────────────────────────────────────

def rebind_user_paths(services: Any, steam_root: Path, account_id: str) -> None:
    """Push the resolved user's per-user paths onto the live services.

    Called by BOTH the frontend push (``set_active_steam_user`` RPC) and the
    ``ACCOUNT_SWITCHED`` handler, so both funnel through identical logic. Each
    setter is best-effort — a missing service slot must not abort the rest.
    ``services`` is the ServiceContainer (attrs: ``shortcut``, ``artwork``);
    accessed via ``getattr`` so this stays import-cycle-free.

    ``proton`` used to be re-bound here too, pointing ProtonService at the
    user's ``localconfig.vdf``. That was doubly wrong — ``CompatToolMapping``
    lives in ``config/config.vdf``, not ``localconfig.vdf``, and the compat
    writer it fed could never run — so ProtonService no longer holds a path
    at all and there is nothing per-user to re-bind.
    """
    sc_path = shortcuts_path(steam_root, account_id)
    g_dir = grid_dir(steam_root, account_id)

    shortcut = getattr(services, "shortcut", None)
    if shortcut is not None and hasattr(shortcut, "set_shortcuts_path"):
        shortcut.set_shortcuts_path(sc_path)
    artwork = getattr(services, "artwork", None)
    if artwork is not None and hasattr(artwork, "set_grid_dir"):
        artwork.set_grid_dir(g_dir)
    logger.info(
        "[CurrentUser] re-bound per-user paths to account %s (shortcuts=%s)",
        account_id, sc_path,
    )


__all__ = [
    "CONFIG_ACTIVE_USER_KEY",
    "account_id_from_steam64",
    "grid_dir",
    "localconfig_path",
    "rebind_user_paths",
    "resolve",
    "shortcuts_path",
    "user_config_dir",
]
