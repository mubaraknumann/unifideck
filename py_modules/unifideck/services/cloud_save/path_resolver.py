import configparser
import logging
import os
import re

logger = logging.getLogger(__name__)

class WinePrefixResolver:
    """Helper to resolve Windows registry path variables in Wine prefixes."""

    @staticmethod
    def _ci_child(parent: str, name: str) -> str | None:
        """Unique case-insensitive child of ``parent`` named ``name``, else None.

        Ambiguous (two siblings differing only in case) returns None so the
        caller keeps the literal — never guess.
        """
        try:
            if not os.path.isdir(parent):
                return None
            low = name.lower()
            hits = [e for e in os.listdir(parent) if e.lower() == low]
        except OSError:
            return None
        return os.path.join(parent, hits[0]) if len(hits) == 1 else None

    @classmethod
    def realize_case_insensitive(cls, path: str) -> str:
        """Match an existing on-disk path case-insensitively.

        Wine prefixes are case-INsensitive (NTFS-like) but the Linux
        filesystem is case-SENSITIVE, so a save path whose casing differs
        from what the game actually created on disk (``Documents/My Games``
        vs ``documents/my games``) would point at a *different* directory.
        Handing that to gogdl/legendary makes them create a second,
        divergent-cased folder the game never reads (the same class of bug
        as the GOG namespace issue). Mirrors Ludusavi's case-insensitive
        path resolution (``glob_case_sensitive(false)`` / ``eq_ignore_ascii_case``).

        Walk from the filesystem root: prefer an exact child (the fast,
        no-op path when everything matches), else a UNIQUE case-insensitive
        sibling, else keep the literal segment (it gets created on sync).
        Only existing directories are ever substituted, so this never
        changes a correct path — it only repairs a casing mismatch.
        """
        norm = os.path.normpath(path)
        parts = norm.split(os.sep)
        # Rebuild from the root; parts[0] is "" for an absolute path.
        cur = os.sep if norm.startswith(os.sep) else (parts[0] or ".")
        for seg in parts[1:]:
            if not seg:
                continue
            exact = os.path.join(cur, seg)
            if os.path.exists(exact):
                cur = exact
            else:
                cur = cls._ci_child(cur, seg) or exact
        return cur

    @staticmethod
    def read_registry(wine_pfx: str) -> configparser.ConfigParser:
        reg = configparser.ConfigParser(
            comment_prefixes=(";", "#", "/", "WINE"),
            allow_no_value=True,
            strict=False,
            interpolation=None
        )
        reg.optionxform = str  # type: ignore[method-assign, assignment]  # configparser idiom: preserve key case
        reg.read(os.path.join(wine_pfx, "user.reg"))
        return reg

    @staticmethod
    def get_shell_folders(registry: configparser.ConfigParser, wine_pfx: str) -> dict[str, str]:
        folders: dict[str, str] = {}
        section = "Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Explorer\\\\Shell Fold Folders"
        if section not in registry:
            section = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Shell Folders"

        if section in registry:
            for k, v in registry[section].items():
                key_name = k.strip('"').strip()
                path_cleaned = v.strip('"').strip().replace("\\\\", "/").replace("C:/", "")
                folders[key_name] = os.path.join(wine_pfx, "drive_c", path_cleaned)
        return folders

    @staticmethod
    def _default_path_vars(
        prefix_path: str, install_path: str, account_id: str,
    ) -> dict[str, str]:
        """Default shell-folder token map (used when the registry is absent).

        NOTE: Epic's ``{AppData}`` cloud-save token resolves to
        %LOCALAPPDATA% (AppData/Local), NOT %APPDATA% (Roaming). Confirmed
        on real games: Felix The Reaper and Ghostrunner both ship a
        ``{AppData}/...`` CloudSaveFolder yet read/write saves under
        AppData/Local — mapping it to Roaming dropped the cloud save where
        the game never looks (Continue stayed greyed out).
        """
        return {
            "{appdata}": os.path.join(prefix_path, "drive_c/users/steamuser/AppData/Local"),
            "{localappdata}": os.path.join(prefix_path, "drive_c/users/steamuser/AppData/Local"),
            "{userdir}": os.path.join(prefix_path, "drive_c/users/steamuser/Documents"),
            "{userprofile}": os.path.join(prefix_path, "drive_c/users/steamuser"),
            "{usersavedgames}": os.path.join(prefix_path, "drive_c/users/steamuser/Saved Games"),
            "{installdir}": install_path,
            # Epic's ``{EpicID}`` token is the logged-in user's Epic ACCOUNT
            # id — NOT the game's catalog/app id. legendary resolves it the
            # same way (``self.lgd.userdata['account_id']``, core.py:834).
            # Games like Vampire Survivors / Brotato namespace saves under
            # ``Roaming/<Game>/<AccountId>/``; feeding the app id here pointed
            # the sync at a folder the game never reads (saves never appeared).
            "{epicid}": account_id,
            "{epic_id}": account_id,
        }

    @classmethod
    def _apply_registry_overrides(
        cls, path_vars: dict[str, str], prefix_path: str,
    ) -> None:
        """Override the AppData/Documents/SavedGames tokens from ``user.reg``.

        Best-effort: a missing/unreadable registry leaves the defaults in
        place. Mutates ``path_vars`` in place.
        """
        user_reg_path = os.path.join(prefix_path, "user.reg")
        if not os.path.isfile(user_reg_path):
            return
        try:
            folders = cls.get_shell_folders(cls.read_registry(prefix_path), prefix_path)
        except Exception:
            logger.exception("Failed to read registry")
            return
        if not folders:
            return
        # Epic {AppData} == Local AppData (see note above), so both tokens
        # resolve to the registry's Local AppData.
        if "Local AppData" in folders:
            path_vars["{appdata}"] = folders["Local AppData"]
            path_vars["{localappdata}"] = folders["Local AppData"]
        if "Personal" in folders:
            path_vars["{userdir}"] = folders["Personal"]
        if "{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}" in folders:
            path_vars["{usersavedgames}"] = folders["{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}"]

    @staticmethod
    def _resolve_template_parts(folder: str, path_vars: dict[str, str]) -> list[str]:
        """Map each ``/``-separated template segment through ``path_vars``."""
        resolved_parts = []
        for p in folder.split("/"):
            p_lower = p.lower()
            if p_lower in path_vars:
                resolved_parts.append(path_vars[p_lower])
            elif p_lower == "%userprofile%":
                resolved_parts.append(path_vars["{userprofile}"])
            else:
                resolved_parts.append(p)
        return resolved_parts

    @classmethod
    def resolve_path(cls, cloud_save_folder: str, prefix_path: str, install_path: str = "", account_id: str = "") -> str:
        # Normalize slashes
        folder = cloud_save_folder.replace("\\", "/").strip("/")

        path_vars = cls._default_path_vars(prefix_path, install_path, account_id)
        cls._apply_registry_overrides(path_vars, prefix_path)

        # Add common aliases/variations
        path_vars["{locallow}"] = os.path.join(prefix_path, "drive_c/users/steamuser/AppData/LocalLow")

        resolved_parts = cls._resolve_template_parts(folder, path_vars)
        resolved_path = os.path.normpath(os.path.join(*resolved_parts))

        # De-duplicate nested path issues (e.g. AppData/LocalLow nested multiple times)
        if "LocalLow" in resolved_path:
            match = re.search(r"LocalLow/(?:drive_c/users/[^/]+/AppData/LocalLow/)?(.*)", resolved_path, re.IGNORECASE)
            if match:
                game_subpath = match.group(1)
                resolved_path = os.path.join(path_vars["{locallow}"], game_subpath)

        resolved_path = cls.realize_case_insensitive(resolved_path)
        return os.path.realpath(resolved_path)

    # ------------------------------------------------------------------
    # Ludusavi / PCGamingWiki path tokens
    # ------------------------------------------------------------------
    #
    # SEPARATE token table from ``resolve_path`` above on purpose. Ludusavi
    # follows real Windows semantics: ``<winAppData>`` is %APPDATA% (Roaming)
    # and ``<winLocalAppData>`` is %LOCALAPPDATA% (Local). Epic's ``{AppData}``
    # token, by contrast, deliberately maps to Local (see the note in
    # ``resolve_path``). Mixing the two tables would reintroduce that bug.
    _LUDUSAVI_BASES = {
        "<home>": "drive_c/users/steamuser",
        "<winappdata>": "drive_c/users/steamuser/AppData/Roaming",
        "<winlocalappdata>": "drive_c/users/steamuser/AppData/Local",
        "<windocuments>": "drive_c/users/steamuser/Documents",
        "<winpublic>": "drive_c/users/Public",
        "<winprogramdata>": "drive_c/ProgramData",
        "<windir>": "drive_c/windows",
    }
    # Registry shell-folder name -> Ludusavi base token, so a real prefix's
    # redirected folders win over the defaults above.
    _SHELL_TO_TOKEN = {
        "AppData": "<winappdata>",
        "Local AppData": "<winlocalappdata>",
        "Personal": "<windocuments>",
    }

    @classmethod
    def _ludusavi_resolved_bases(cls, prefix_path: str) -> dict[str, str]:
        """Ludusavi base-token → prefix dir map, with registry overrides.

        Best-effort: a missing/unreadable registry just leaves the defaults.
        """
        resolved_bases = {
            tok: os.path.join(prefix_path, rel)
            for tok, rel in cls._LUDUSAVI_BASES.items()
        }
        user_reg_path = os.path.join(prefix_path, "user.reg")
        if not os.path.isfile(user_reg_path):
            return resolved_bases
        try:
            folders = cls.get_shell_folders(cls.read_registry(prefix_path), prefix_path)
        except Exception:  # pragma: no cover - registry is best-effort
            logger.debug("ludusavi: registry read failed", exc_info=True)
            return resolved_bases
        for shell_name, token in cls._SHELL_TO_TOKEN.items():
            if shell_name in folders:
                resolved_bases[token] = folders[shell_name]
        return resolved_bases

    @staticmethod
    def _linux_bases() -> dict[str, str]:
        """Real Linux base dirs for native (non-Proton) games — ``<home>`` and
        the XDG dirs resolve to the actual user home, NOT a Wine prefix."""
        home = os.path.expanduser("~")
        xdg_data = os.environ.get("XDG_DATA_HOME")
        xdg_conf = os.environ.get("XDG_CONFIG_HOME")
        return {
            "<home>": home,
            "<xdgdata>": xdg_data if (xdg_data and os.path.isabs(xdg_data))
            else os.path.join(home, ".local", "share"),
            "<xdgconfig>": xdg_conf if (xdg_conf and os.path.isabs(xdg_conf))
            else os.path.join(home, ".config"),
        }

    @classmethod
    def _ludusavi_linux_base_for(
        cls, first: str, install_path: str,
    ) -> str | None:
        """Resolve a leading Ludusavi token for a NATIVE-Linux game, or None."""
        bases = cls._linux_bases()
        if first in bases:
            return bases[first]
        if first in ("<base>", "<root>", "<game>"):
            return install_path or None
        # Windows tokens (<winAppData> …), <storeUserId>, unknown — N/A for a
        # native Linux game.
        return None

    @classmethod
    def _ludusavi_base_for(
        cls,
        first: str,
        resolved_bases: dict[str, str],
        prefix_path: str,
        install_path: str,
    ) -> str | None:
        """Resolve the leading Ludusavi token to a base dir, or ``None``."""
        if first in resolved_bases:
            return resolved_bases[first]
        if first in ("<base>", "<root>", "<game>"):
            # ``None`` when no install dir is known.
            return install_path or None
        if first == "<osusername>":
            return os.path.join(prefix_path, "drive_c/users/steamuser")
        # Absolute Windows drive path (``C:/Users/Public/…``) — map the C:
        # drive to the prefix's drive_c (case fixed up on disk later). Other
        # drive letters aren't reliably present in a prefix, so skip them.
        if first in ("c:", "c"):
            return os.path.join(prefix_path, "drive_c")
        # Unknown/Linux token (<xdgData>, <xdgConfig>, …) — not resolvable
        # for a Proton (Windows) prefix.
        return None

    @classmethod
    def resolve_ludusavi_path(
        cls,
        ludusavi_path: str,
        prefix_path: str,
        install_path: str = "",
        native_linux: bool = False,
    ) -> str | None:
        """Resolve a Ludusavi/PCGamingWiki save path into a directory.

        Ludusavi paths look like ``<winAppData>/Foo/Saves`` or
        ``<base>/save/user_*.dat``. We resolve the leading token, then walk
        until the first DYNAMIC segment (a wildcard ``*`` or ``<storeUserId>``
        / any other unresolved ``<...>`` token) and return the literal
        directory up to that point — the sync tool then syncs that whole
        subtree. Returns ``None`` when the path can't be resolved.

        ``native_linux`` switches the token meaning for games run NATIVELY
        (not through Proton): ``<home>``/``<xdgData>``/``<xdgConfig>`` resolve
        to the real Linux home/XDG dirs and ``<base>`` to the Linux install
        dir; Windows tokens (``<winAppData>`` …) then return None (and vice
        versa for the default Windows-prefix mode), so each runtime only
        resolves the paths that actually apply to it.
        """
        if not ludusavi_path:
            return None
        raw = ludusavi_path.replace("\\", "/").strip("/")
        segments = [s for s in raw.split("/") if s]
        if not segments:
            return None

        if native_linux:
            base = cls._ludusavi_linux_base_for(segments[0].lower(), install_path)
            username = os.environ.get("USER") or "deck"
        else:
            resolved_bases = cls._ludusavi_resolved_bases(prefix_path)
            base = cls._ludusavi_base_for(
                segments[0].lower(), resolved_bases, prefix_path, install_path,
            )
            username = "steamuser"
        if base is None:
            return None

        out_parts = [base]
        for seg in segments[1:]:
            low = seg.lower()
            # Stop at the first dynamic segment; sync the containing directory.
            # ``<osUserName>`` is the one token we expand inline rather than
            # stop at (it's a known value, not a wildcard).
            if "*" in seg or low == "<storeuserid>" or (
                seg.startswith("<") and seg.endswith(">") and low != "<osusername>"
            ):
                break
            if low == "<osusername>":
                out_parts.append(username)
                continue
            out_parts.append(seg)

        resolved = os.path.normpath(os.path.join(*out_parts))
        resolved = cls.realize_case_insensitive(resolved)
        return os.path.realpath(resolved)


# ── title-matched save-dir auto-detection ─────────────────────────────
#: The four in-prefix roots a Windows game plausibly writes saves under,
#: in the order worth trying. Both former copies of this heuristic listed
#: the same four in the same order.
_SAVE_DIR_ROOTS = (
    ("users", "steamuser", "Saved Games"),
    ("users", "steamuser", "Documents"),
    ("users", "steamuser", "AppData", "Local"),
    ("users", "steamuser", "AppData", "Roaming"),
)


def _sanitise_title(title: str) -> str:
    """Strip *title* to lowercase ASCII alphanumerics."""
    return re.sub(r"[^a-zA-Z0-9]", "", title).lower()


def find_save_dir_by_title(
    drive_c: str | os.PathLike[str],
    game_title: str,
    *,
    tag: str = "CloudSave",
) -> str | None:
    """Guess a game's in-prefix save directory by matching its title.

    Last-resort heuristic, used only when neither an explicit override nor
    the store's own cloud-save metadata resolved a path.

    **The empty-title guard is on the sanitised value, not the raw one.**
    This existed twice; the copy in ``gog_strategy`` guarded the *raw*
    title, so a title with no ASCII alphanumerics — a non-Latin GOG
    release, or one that is all punctuation — sanitised to ``""``, passed
    the guard, and then matched on ``"" in child_name``: the **first**
    directory under ``Saved Games`` was returned as the save dir, and
    cloud sync would have carried an unrelated folder. A drift finding
    names a difference, not a direction (audit §3.2); here the direction
    was clear once both were read side by side. Audit register item 47.

    Args:
        drive_c: the prefix's ``drive_c``.
        game_title: the game's display title; ``None``-ish or
            non-alphanumeric yields ``None`` rather than a guess.
        tag: log prefix, so a hit is still attributable to its caller.

    Returns:
        Absolute path of the matched directory, or ``None``.
    """
    safe_title = _sanitise_title(game_title or "")
    if not safe_title:
        return None
    for parts in _SAVE_DIR_ROOTS:
        candidate = os.path.join(str(drive_c), *parts)
        match = _match_child_by_title(candidate, safe_title, tag=tag)
        if match:
            return match
    return None


def _match_child_by_title(
    candidate: str, safe_title: str, *, tag: str,
) -> str | None:
    """First child dir of *candidate* whose sanitised name matches."""
    try:
        if not os.path.isdir(candidate):
            return None
        children = sorted(os.listdir(candidate))
    except OSError:
        return None
    for name in children:
        child = os.path.join(candidate, name)
        if not os.path.isdir(child):
            continue
        child_name = _sanitise_title(name)
        if not child_name:
            continue
        if safe_title in child_name or child_name in safe_title:
            logger.info("[%s] Auto-detected save dir via title match: %s", tag, child)
            return child
    return None
