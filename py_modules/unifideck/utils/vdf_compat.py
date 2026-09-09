"""utils/vdf_compat.py — Steam config.vdf + compatibilitytools.d parsing.

Shared and dependency-light (stdlib + the vendored ``vdf`` KeyValues
parser only) so BOTH the Decky backend (bundled Python) and the
out-of-process game launcher (system ``/usr/bin/python3``) can import
it — ``utils/`` is already on the launcher's import path. Nothing here
may pull in ``aiohttp`` or the ``compatibility`` package; that would
break the launcher process, which imports these helpers directly.

Two cross-distro concerns live here:

1. **Steam root / config.vdf discovery.** SteamOS keeps Steam at
   ``~/.steam/steam``; on Bazzite/CachyOS (native Steam) that symlink
   usually exists too, but ``~/.local/share/Steam`` and the Flatpak
   path are probed as well so resolution never hard-codes one layout.

2. **Compat-tool enumeration** that understands ``compatibilitytool.vdf``
   manifests and the system-wide ``/usr/share/steam/compatibilitytools.d``
   directory where CachyOS's ``proton-cachyos`` package installs. Steam
   itself only lists the user dirs, so distro packaging drops a loose
   ``.vdf`` there whose ``install_path`` points at the system dir. A
   plain directory scan (the pre-0.7.1 behaviour) misses both, so a
   force-selected Proton-CachyOS/GE silently fell through to GE-latest.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

# Candidate Steam roots, most-specific first. ``~/.steam/steam`` and
# ``~/.steam/root`` are the symlinks Steam maintains; the share dir is
# the real target on most distros; the last is Flatpak Steam. Kept as a
# single source of truth so ``steam.library.find_steam_path`` and the
# launcher agree (and match ``defaults/config.json``'s advertised list).
STEAM_ROOT_CANDIDATES: tuple[str, ...] = (
    "~/.steam/steam",
    "~/.steam/root",
    "~/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/.steam/steam",
)

# System-wide compat-tool dirs populated by distro packages (CachyOS
# ``proton-cachyos``, Arch ``proton-ge-custom``). Steam does not scan
# these, so tooling that resolves Proton independently must.
SYSTEM_COMPAT_DIRS: tuple[str, ...] = (
    "/usr/share/steam/compatibilitytools.d",
    "/usr/local/share/steam/compatibilitytools.d",
)

# ``~/.steam/root`` is a symlink Steam creates to the active install; on most
# distros it points at ``~/.local/share/Steam``. ``~/.steam/steam`` is listed
# explicitly so Proton still resolves if that symlink is absent (fresh
# install, unusual setup) — it costs nothing when the dirs don't exist.
STEAM_COMPAT_ROOTS: tuple[str, ...] = (
    "~/.steam/root/compatibilitytools.d",
    "~/.steam/steam/compatibilitytools.d",
    "~/.local/share/Steam/compatibilitytools.d",
)

# Unifideck's own compat-tool dir. Not "external" by definition, so external
# detection excludes it while name resolution includes it.
UNIFIDECK_COMPAT_DIR = "~/.local/share/unifideck/compat-tools"


def compat_tool_roots(
    *, include_unifideck: bool = True, include_system: bool = True,
) -> list[Path]:
    """Every ``compatibilitytools.d`` root to search, in priority order.

    The single definition of "where compat tools live". Four copies of this
    list existed before 2026-09-07 (``selector.STEAM_COMPAT_ROOTS``,
    ``ge_installer._SCAN_ROOTS``, ``selector._compat_tool_roots`` and
    ``external_ge.EXTERNAL_COMPAT_ROOTS``); two of them had already drifted
    apart on whether the unifideck dir counts. Callers now say what they
    mean with the two flags instead of maintaining a fifth list.

    Order is unifideck dir, then the user Steam dirs, then the system-wide
    dirs distro packages install into but Steam never lists. The pre-0.7.1
    resolver scanned only the three user dirs, so a system-wide /
    manifest-registered tool the user force-selected was unresolvable and
    silently fell back to GE-latest.

    **Deduplicated by real path.** ``~/.steam/root``, ``~/.steam/steam`` and
    ``~/.local/share/Steam`` are normally the same directory, so a naive list
    made ``iter_compat_tools`` enumerate and parse it three times: measured
    3.0 ms per call on a Steam Deck against 0.98 ms deduplicated, on a path
    that now runs at every game launch. A broken symlink cannot be resolved,
    so those fall back to the unresolved path rather than being dropped.
    """
    raw: list[str] = []
    if include_unifideck:
        raw.append(UNIFIDECK_COMPAT_DIR)
    raw.extend(STEAM_COMPAT_ROOTS)
    if include_system:
        raw.extend(SYSTEM_COMPAT_DIRS)

    roots: list[Path] = []
    seen: set[Path] = set()
    for entry in raw:
        path = Path(entry).expanduser()
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        seen.add(key)
        roots.append(path)
    return roots


# loginusers.vdf user blocks are flat KeyValues (no nested braces), so a
# ``[^{}]*`` body capture is safe and keeps this launcher-safe — no need to
# import the ``vdf`` lib in the root-resolution hot path.
_LOGINUSERS_USER_RE = re.compile(r'"(\d{6,})"\s*\{([^{}]*)\}', re.DOTALL)
_MOST_RECENT_RE = re.compile(r'"MostRecent"\s+"1"')
_TIMESTAMP_RE = re.compile(r'"Timestamp"\s+"(\d+)"')


def _most_recent_login(loginusers: Path) -> tuple[int | None, str | None]:
    """``(Timestamp, userdata-id)`` for the ``MostRecent`` account, else ``(None, None)``.

    Steam stamps the last-logged-in account's ``Timestamp`` here; it's the
    sharpest "which install is live" signal when several Steam roots coexist.
    Regex-parsed (not the ``vdf`` lib) so the launcher process can call it
    without extra deps.
    """
    try:
        text = loginusers.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, None
    best_ts: int | None = None
    best_user: str | None = None
    for steam64, body in _LOGINUSERS_USER_RE.findall(text):
        if not _MOST_RECENT_RE.search(body):
            continue
        m = _TIMESTAMP_RE.search(body)
        try:
            ts = int(m.group(1)) if m else 0
            acct = str(int(steam64) & 0xFFFFFFFF)
        except (TypeError, ValueError):
            continue
        if best_ts is None or ts > best_ts:
            best_ts, best_user = ts, acct
    return best_ts, best_user


def _steam_root_liveness(root: Path) -> tuple[float, str | None]:
    """``(recency_score, most_recent_user)`` — higher score = more recently active.

    Combines the ``MostRecent`` login ``Timestamp`` with the mtimes of the
    files Steam rewrites while running (``loginusers.vdf`` and each user's
    ``localconfig.vdf``). The install the user is actually running always has
    the freshest of these, so it outranks a stale sibling root.
    """
    login = root / "config" / "loginusers.vdf"
    ts, user = _most_recent_login(login)
    score = float(ts) if ts else 0.0
    for path in (login, *root.glob("userdata/*/config/localconfig.vdf")):
        try:
            score = max(score, path.stat().st_mtime)
        except OSError:
            continue
    return score, user


def resolve_live_steam_root(
    candidates: Iterable[str] = STEAM_ROOT_CANDIDATES,
) -> Path | None:
    """The Steam root the user is *running*, not merely the first that exists.

    A candidate qualifies only if it has a ``steamapps/`` dir. Symlinked
    duplicates (``~/.steam/steam`` → ``~/.local/share/Steam``) collapse to one
    real install. When two *distinct* installs both qualify, the freshest
    (see ``_steam_root_liveness``) wins — so a stale ``~/.steam/steam``
    skeleton can't shadow a running Flatpak Steam (the "synced but nothing
    shows" bug), and a WARNING names both so support can spot the mismatch.
    """
    rooted: list[Path] = []
    seen: set[Path] = set()
    for cand in candidates:
        root = Path(cand).expanduser()
        if not (root / "steamapps").is_dir():
            continue
        try:
            key = root.resolve()
        except OSError:
            key = root
        if key in seen:
            continue
        seen.add(key)
        rooted.append(root)
    if not rooted:
        return None
    if len(rooted) == 1:
        return rooted[0]
    scored = sorted(
        ((_steam_root_liveness(r)[0], r) for r in rooted),
        key=lambda t: t[0],
        reverse=True,
    )
    chosen, runner_up = scored[0][1], scored[1][1]
    logger.warning(
        "[vdf_compat] %d distinct Steam installs have steamapps/; writing to "
        "the most recently active: %s (runner-up: %s). If shortcuts never "
        "appear, set paths.steam_root to the Steam you actually run.",
        len(scored), chosen, runner_up,
    )
    return chosen


def find_steam_root() -> Path | None:
    """The live Steam root, or ``None``.

    Launcher-safe twin of ``steam.library.find_steam_path`` (which pulls in
    ``aiohttp`` at import and so cannot run in the launcher process, and which
    additionally honours the Decky-only ``paths.steam_root`` /
    ``paths.steam_candidates`` config overrides).
    """
    return resolve_live_steam_root()


def find_steam_config_vdf() -> Path | None:
    """Global ``config/config.vdf`` under the resolved Steam root, or ``None``.

    ``CompatToolMapping`` (both per-app and the ``"0"`` global default)
    lives in this file — NOT in the per-user ``localconfig.vdf``.
    """
    root = find_steam_root()
    if root is None:
        return None
    cfg = root / "config" / "config.vdf"
    return cfg if cfg.is_file() else None


def steam_library_dirs() -> list[Path]:
    """Every Steam library root, the main install first.

    Parsed from ``steamapps/libraryfolders.vdf``'s ``"path"`` entries.
    Proton is installed into whichever library Steam picked, which on a Deck
    is routinely an SD card or a second drive — searching only the main
    install means a perfectly valid Proton the user selected is simply not
    found, and the launcher silently falls back to a different one.

    Launcher-safe: regex over the file, no ``vdf`` import and no
    ``steam.library.find_steam_path`` (which pulls aiohttp and cannot load
    in the launcher's slim Python). Mirrors
    ``steam.owned_games._list_library_roots``, which is unavailable here for
    that reason. Returns ``[]`` when no Steam root resolves; missing or
    unreadable ``libraryfolders.vdf`` degrades to just the main root.
    """
    root = find_steam_root()
    if root is None:
        return []
    roots = [root]
    libfolders = root / "steamapps" / "libraryfolders.vdf"
    try:
        content = libfolders.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return roots
    for match in re.finditer(r'"path"\s*"([^"]+)"', content):
        path = Path(match.group(1))
        if path not in roots and path.is_dir():
            roots.append(path)
    return roots


def _extract_kv_block(content: str, start: int) -> str:
    """Return the balanced ``{ … }`` block beginning at/after *start*.

    Respects nested per-appid blocks (a ``[^}]*`` regex would stop at
    the first inner ``}``); ``""`` when no balanced block is found.
    """
    open_brace = content.find("{", start)
    if open_brace < 0:
        return ""
    depth = 0
    for i in range(open_brace, len(content)):
        ch = content[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return content[open_brace:i + 1]
    return ""


def parse_compat_tool(content: str, appid: int) -> str:
    """Return the per-app ``CompatToolMapping[appid]`` tool name, or ``""``."""
    if not content:
        return ""
    appid_str = str(appid)
    if f'"{appid_str}"' not in content:
        return ""
    marker = '"CompatToolMapping"'
    marker_pos = content.find(marker)
    if marker_pos < 0:
        return ""
    pattern = re.compile(rf'"{appid_str}"\s*\{{([^}}]*)\}}', re.DOTALL)
    m = pattern.search(content, marker_pos)
    if not m:
        return ""
    name_match = re.search(r'"name"\s+"([^"]*)"', m.group(1))
    return name_match.group(1) if name_match else ""


def parse_global_default_compat_tool(content: str) -> str:
    """Return the global-default tool (``CompatToolMapping["0"]``), or ``""``.

    Bazzite/CachyOS ship this pre-set (e.g. ``Proton-CachyOS``). Bounded
    to the ``CompatToolMapping`` block via ``_extract_kv_block`` so an
    unrelated ``"0" { … }`` elsewhere in ``config.vdf`` can't false-match.
    """
    if not content:
        return ""
    marker = '"CompatToolMapping"'
    marker_pos = content.find(marker)
    if marker_pos < 0:
        return ""
    block = _extract_kv_block(content, marker_pos)
    if not block:
        return ""
    m = re.search(r'"0"\s*\{([^}]*)\}', block, re.DOTALL)
    if not m:
        return ""
    name_match = re.search(r'"name"\s+"([^"]*)"', m.group(1))
    return name_match.group(1) if name_match else ""


def _manifest_tools(manifest: Path, base_dir: Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(name, proton_path)`` for each tool in one ``.vdf`` manifest.

    Maps BOTH the internal name (the ``compat_tools`` key) and the
    ``display_name`` to the resolved ``proton`` script, following
    ``install_path`` (``"."``/relative → *base_dir*; absolute → verbatim).
    Only yields tools whose ``proton`` script actually exists. Never raises.
    """
    try:
        import vdf
        with manifest.open(encoding="utf-8", errors="ignore") as f:
            data = vdf.load(f)  # type: ignore[no-untyped-call]  # vendored vdf is untyped
    except Exception as e:
        logger.debug("[vdf_compat] manifest %s parse failed: %s", manifest, e)
        return
    root = data.get("compatibilitytools", {}) if isinstance(data, dict) else {}
    tools = root.get("compat_tools", {}) if isinstance(root, dict) else {}
    if not isinstance(tools, dict):
        return
    for internal_name, spec in tools.items():
        yield from _resolve_manifest_tool(internal_name, spec, base_dir)


def _resolve_manifest_tool(
    internal_name: str, spec: object, base_dir: Path,
) -> Iterator[tuple[str, Path]]:
    """Yield ``(name, proton_path)`` for one ``compat_tools`` entry.

    Extracted from :func:`_manifest_tools` to keep that function under the
    cognitive-complexity cap; behaviour is identical. Yields nothing when
    *spec* is malformed or the ``proton`` script does not exist.
    """
    if not isinstance(spec, dict):
        return
    install_path = str(spec.get("install_path", ".") or ".")
    tool_dir = (
        Path(install_path)
        if Path(install_path).is_absolute()
        else base_dir / install_path
    )
    proton = (tool_dir / "proton").expanduser()
    if not proton.is_file():
        return
    for name in (internal_name, str(spec.get("display_name", ""))):
        if name:
            yield name, proton


def official_proton_alias(dir_name: str) -> str | None:
    """Steam's internal ``CompatToolMapping`` name for an official Proton dir.

    Valve's own Protons ship **no** ``compatibilitytool.vdf`` (only a
    ``toolmanifest.vdf``), so nothing supplies their internal name and
    :func:`_entry_tools` can only key them by directory name — yet
    ``CompatToolMapping`` records the user's choice under the internal name::

        "Proton - Experimental"  ->  proton_experimental
        "Proton 9.0 (Beta)"      ->  proton_9
        "Proton 10.0"            ->  proton_10
        "Proton 11.0"            ->  proton_11
        "Proton Hotfix"          ->  proton_hotfix

    Without this alias EVERY official Proton the user selects in Steam's own
    Properties > Compatibility dialog fails to resolve, so
    ``select_proton_version`` falls through its tiers to the latest
    GE-Proton and the user's choice is silently ignored — the game keeps
    launching under the wrong Proton no matter what they pick. Field report:
    "no other game is loading with a different Proton", with every launch
    logging ``selected via saved tool: GE-Proton11-3`` while ``config.vdf``
    held ``proton_11`` / ``proton_experimental``.

    Third-party tools are unaffected: their real internal name comes from
    their manifest and is yielded first, and ``iter_compat_tools`` uses
    ``setdefault``, so this only ever *adds* a fallback key. GE/UMU builds
    do not start with "Proton" so they get no alias at all.

    Returns ``None`` when *dir_name* is not an official-Proton-shaped name.
    """
    if not dir_name.lower().startswith("proton"):
        return None
    rest = dir_name[len("proton"):].strip(" -_")
    if not rest:
        return None
    # "9.0 (Beta)" -> "9"; "Experimental" -> "experimental"
    token = rest.split()[0].split(".")[0].strip("()").lower()
    return f"proton_{token}" if token else None


def _entry_tools(entry: Path, root: Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(name, proton)`` for one directory entry under a compat root.

    A tool dir with a ``compatibilitytool.vdf`` (manifest names first, then
    the dir name as a bare fallback), or a loose top-level ``*.vdf`` manifest.
    Bare dirs additionally yield their official-Proton internal alias — see
    :func:`official_proton_alias`.
    """
    if entry.is_dir():
        manifest = entry / "compatibilitytool.vdf"
        if manifest.is_file():
            yield from _manifest_tools(manifest, entry)
        bare = entry / "proton"
        if bare.is_file():
            yield entry.name, bare
            alias = official_proton_alias(entry.name)
            if alias:
                yield alias, bare
    elif entry.suffix == ".vdf":
        yield from _manifest_tools(entry, root)


def iter_compat_tools(roots: list[Path] | tuple[Path, ...]) -> dict[str, Path]:
    """Map every compat-tool name found under *roots* to its ``proton`` script.

    Handles three shapes seen across distros: a tool directory holding a
    ``compatibilitytool.vdf`` (per-dir manifest), a loose top-level
    ``*.vdf`` manifest whose ``install_path`` points elsewhere (how
    CachyOS's ``proton-cachyos`` is registered into the user dir), and a
    bare directory with a ``proton`` script and no manifest. Keys include
    internal names, display names, and directory names so a
    ``CompatToolMapping`` value resolves however Steam wrote it. Earlier
    roots win on name collisions (user dir before system dir).
    """
    result: dict[str, Path] = {}
    for raw_root in roots:
        root = Path(raw_root).expanduser()
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            for name, proton in _entry_tools(entry, root):
                result.setdefault(name, proton)
    return result


def resolve_compat_tool(
    tool_id: str, roots: list[Path] | tuple[Path, ...],
) -> Path | None:
    """Resolve *tool_id* to its ``proton`` script under *roots*.

    Exact match first, then case-insensitive (Steam's stored name and a
    tool's directory/display name occasionally differ only in case).
    """
    if not tool_id:
        return None
    tools = iter_compat_tools(roots)
    exact = tools.get(tool_id)
    if exact is not None:
        return exact
    lowered = tool_id.lower()
    for name, proton in tools.items():
        if name.lower() == lowered:
            return proton
    return None
