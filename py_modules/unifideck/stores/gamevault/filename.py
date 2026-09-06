"""GameVault archive-name grammar — one parser for both modes.

GameVault names a library file like this, every token after the title
optional and the parentheses literal::

    Title (v1.5.0) (EA) (W_P) (NC) (2021).zip

The remote store already had to read that shape, because a server whose
metadata lookup found nothing hands back a bare ``file_path`` and the title
has to come from the filename. Local mode reads *only* filenames, so the
grammar became the whole identity of a game rather than a fallback — and a
second copy of a parser this fiddly is exactly the drift the shared-helper
rule exists to stop. One module, two callers.

The title normalisation is deliberately lossy in the same way the original
``_parse_title_from_filename`` was (``The_Witcher-3_Wild-Hunt`` →
``The Witcher 3 Wild Hunt``): real archives use ``_`` and ``-`` as word
separators far more often than as part of a title, and unifiDB's 0.65 match
threshold absorbs the occasional ``Half-Life`` → ``Half Life``. Keeping the
old behaviour also means the remote path's pinned tests still describe what
ships.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

# Windows/Linux × portable/setup, as GameVault spells them. ``_SETUP_TYPES``
# is the pair that means "this archive contains an installer, not a game" —
# the known gap documented in ``install.py``. Knowing it up front is what
# lets us warn the user instead of silently producing a shortcut that
# launches Setup.exe.
GAME_TYPE_WINDOWS_PORTABLE = "W_P"
GAME_TYPE_WINDOWS_SETUP = "W_S"
GAME_TYPE_LINUX_PORTABLE = "L_P"
GAME_TYPE_LINUX_SOFTWARE = "L_SW"

_GAME_TYPES = frozenset(
    {
        GAME_TYPE_WINDOWS_PORTABLE,
        GAME_TYPE_WINDOWS_SETUP,
        GAME_TYPE_LINUX_PORTABLE,
        GAME_TYPE_LINUX_SOFTWARE,
    }
)
_SETUP_TYPES = frozenset({GAME_TYPE_WINDOWS_SETUP, GAME_TYPE_LINUX_SOFTWARE})
_LINUX_TYPES = frozenset({GAME_TYPE_LINUX_PORTABLE, GAME_TYPE_LINUX_SOFTWARE})

# Longest-first: ``.tar.gz`` must win over ``.gz`` or the stem keeps a
# ``.tar``. ``.exe`` and ``.sh`` are here because the *title* parser has
# always stripped them (a GameVault library may hold a bare executable);
# whether such a file is indexable is a separate question, answered by
# ``ARCHIVE_EXTENSIONS`` below.
_STRIPPABLE_EXTENSIONS = (
    ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst",
    ".zip", ".7z", ".rar", ".tar", ".iso", ".wim", ".cab", ".msi",
    ".gz", ".bz2", ".xz", ".zst",
    ".exe", ".sh", ".appimage",
)

# What local mode will actually index. A deliberate subset of the formats
# GameVault's server accepts. Every entry must be recognised by
# ``archive.detect_format``, which is the gate that decides whether an
# archive can be unpacked — an extension listed here but unknown to that
# function produces a library entry that fails at install, which is exactly
# what ``.tar*``/``.iso``/``.wim``/``.cab`` used to do. Bare executables are
# excluded: the vault holds archives.
ARCHIVE_EXTENSIONS = (
    ".zip", ".7z", ".rar",
    ".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst",
    ".iso", ".wim", ".cab",
)

# Sentinel field name for a token that is recognised but carries nothing.
_IGNORED = "_ignored"

_VERSION_RE = re.compile(r"^v\d[\w.\-]*$", re.IGNORECASE)
_YEAR_RE = re.compile(r"^\d{4}$")
_TRAILING_TOKEN_RE = re.compile(r"\s*[\(\[]([^()\[\]]*)[\)\]]\s*$")


@dataclass(frozen=True)
class ParsedName:
    """One archive filename, decomposed.

    ``title`` is normalised for display and for metadata matching;
    ``identity`` is the stable key two files of the same game share.
    """

    title: str
    version: str | None = None
    early_access: bool = False
    game_type: str | None = None
    year: int | None = None

    @property
    def is_installer(self) -> bool:
        """True when the type token says the archive contains a setup.

        Only ever True when the user labelled the file. An unlabelled
        installer still slips through to the exe scorer's fallback — this is
        the cheap half of the fix, not a detector.
        """
        return self.game_type in _SETUP_TYPES

    @property
    def is_linux(self) -> bool:
        """True when the type token says the payload is a Linux build."""
        return self.game_type in _LINUX_TYPES

    @property
    def identity(self) -> str:
        """The key that groups every version of one game.

        Title plus year, not the filename: replacing ``Game (v1.0).zip`` with
        ``Game (v1.1).zip`` has to keep the same shortcut, appId, artwork and
        playtime, and it only can if the id survives the rename.
        """
        return f"{_normalise_for_identity(self.title)}|{self.year or ''}"


# A path only Windows could have written: a drive letter, or a UNC share.
# A GameVault server stores each library file's path as its own OS spells it,
# so a self-hosted Windows server hands a Linux client
# ``C:\Users\me\Vault\files\Game.zip`` — and ``Path(...).name`` there is
# ``PosixPath``, which does not treat ``\`` as a separator and so returns the
# *whole string* as the filename. That is how a shortcut ended up named
# ``C:\Users\numan\Vault\files\Endless Sky``.
#
# Gated on the prefix rather than splitting on ``[\\/]`` unconditionally, and
# that is load-bearing: local mode parses *bare* filenames
# (``local_catalog._scan``), a backslash is a legal character in a Linux
# filename, and ``ParsedName.identity`` is what ``lv_`` game ids — and
# therefore Steam appIds, playtime and grid files — derive from. Splitting a
# bare ``My\Game.zip`` would silently re-key that shortcut and orphan it.
# No real vault filename starts with ``X:\``, ``X:/`` or ``\\``.
_WINDOWS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def leaf_name(file_path: str) -> str:
    """The filename part of *file_path*, whichever OS wrote it."""
    if _WINDOWS_PATH_RE.match(file_path):
        return PureWindowsPath(file_path).name
    return PurePosixPath(file_path).name


def strip_extension(name: str) -> str:
    """*name* without a recognised archive/executable extension."""
    lowered = name.lower()
    for ext in _STRIPPABLE_EXTENSIONS:
        if lowered.endswith(ext):
            return name[: -len(ext)]
    return name


def is_indexable(name: str) -> bool:
    """True when local mode should treat *name* as a game archive."""
    lowered = name.lower()
    return any(lowered.endswith(ext) for ext in ARCHIVE_EXTENSIONS)


def parse_archive_name(file_path: str) -> ParsedName:
    """Decompose a GameVault-style filename.

    Tokens are consumed from the right and classified one at a time; the
    first token that matches nothing ends the scan and stays part of the
    title. That tolerance is the point — ``Game (Deluxe Edition) (2019).zip``
    must keep its edition, while ``(2019)`` is metadata.
    """
    stem = strip_extension(leaf_name(file_path))
    fields: dict[str, Any] = {}

    while (match := _TRAILING_TOKEN_RE.search(stem)) is not None:
        classified = _classify_token(match.group(1).strip(), match.group(0))
        if classified is None:
            break
        key, value = classified
        if key != _IGNORED:
            fields[key] = value
        stem = stem[: match.start()]

    return ParsedName(
        title=_normalise_title(stem),
        version=fields.get("version"),
        early_access=bool(fields.get("early_access", False)),
        game_type=fields.get("game_type"),
        year=fields.get("year"),
    )


def _classify_token(token: str, raw: str) -> tuple[str, Any] | None:
    """``(field, value)`` for one trailing token, or None to stop the scan.

    A flat sequence of guards rather than a chain inside the loop: the two
    together were over the nesting cap, and the classification is the part
    worth reading on its own.
    """
    upper = token.upper()
    if _YEAR_RE.match(token):
        return ("year", int(token))
    if upper in _GAME_TYPES:
        return ("game_type", upper)
    if upper == "EA":
        return ("early_access", True)
    if upper == "NC":
        # no-cache: a server-side hint with no meaning for us
        return (_IGNORED, None)
    if _VERSION_RE.match(token):
        return ("version", token)
    if raw.lstrip().startswith("["):
        # A square-bracket group we could not classify is a scene or repack
        # tag ("[DODI Repack]", "[FitGirl]"). Parentheses are not treated
        # this way: those hold real title parts, and dropping
        # "(Deluxe Edition)" would merge two genuinely different games.
        return (_IGNORED, None)
    return None


def _normalise_title(raw: str) -> str:
    """Separator characters to spaces, whitespace collapsed."""
    text = re.sub(r"[_\-]+", " ", raw).strip()
    return re.sub(r"\s{2,}", " ", text).strip()


def _normalise_for_identity(title: str) -> str:
    """A title reduced to what two spellings of one game have in common."""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def version_sort_key(version: str | None) -> tuple[int, ...]:
    """Sort key that orders ``v1.10`` above ``v1.9``.

    A plain string compare puts ``v1.9`` last, which would pick the older
    archive as the install target for every game that reaches double-digit
    point releases. Non-numeric fragments (``v1.8b``) contribute their
    leading digits and nothing else.
    """
    if not version:
        return (0,)
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts) or (0,)
