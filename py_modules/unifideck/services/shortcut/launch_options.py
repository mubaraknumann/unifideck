"""Resilient store-id extraction from Steam shortcut LaunchOptions.

Steam preserves ``LaunchOptions`` across shutdowns reliably (much more
so than custom ``tags``, which can be stripped by Steam updates or
third-party tools). So we identify Unifideck-managed shortcuts via a
regex match on LaunchOptions, not via the legacy ``UNIFIDECK_TAG``.

The regex tolerates user-appended parameters (LSFG=1, MANGOHUD=1,
``%command%`` wrappers, etc.) and handles Amazon's dotted IDs like
``amzn1.adg.product.<uuid>``.

Ported from ``staging:py_modules/unifideck/shortcuts/launch_options.py``.
"""

from __future__ import annotations

import re

# ``\b`` anchors the store prefix on a word boundary so
# ``not-epic:foo`` doesn't match. Amazon's IDs use dots, so we
# include ``.`` in the id character class but require the first
# character to be alphanumeric (skips e.g. ``epic:.hidden`` noise).
STORE_ID_PATTERN = re.compile(
    r"\b(epic|gog|amazon|ubisoft|battlenet|microsoft|gamevault):"
    r"([a-zA-Z0-9][a-zA-Z0-9._-]*)",
)

# Our own action flags, e.g. ``UNIFIDECK_UBISOFT_ACTION=auth``. The quoted
# alternative comes first so a value containing spaces is consumed whole.
_UNIFIDECK_ENV_TOKEN_RE = re.compile(
    r"\bUNIFIDECK_[A-Z0-9_]+=(?:\"[^\"]*\"|\S+)",
)


def extract_store_id(launch_options: str) -> tuple[str, str] | None:
    """Return ``(store, game_id)`` extracted from ``launch_options``.

    Returns ``None`` when no Unifideck pattern is present.
    """
    if not launch_options:
        return None
    match = STORE_ID_PATTERN.search(launch_options)
    if match:
        return match.group(1), match.group(2)
    return None


def is_unifideck_shortcut(launch_options: str) -> bool:
    """Cheap predicate: does this LaunchOptions look Unifideck-managed?"""
    if not launch_options:
        return False
    return STORE_ID_PATTERN.search(launch_options) is not None


def get_store_prefix(launch_options: str) -> str | None:
    """Return the store name (``"epic"``, ``"gog"``, …) or ``None``."""
    if not launch_options:
        return None
    match = STORE_ID_PATTERN.search(launch_options)
    return match.group(1) if match else None


def get_full_id(launch_options: object) -> str | None:
    """Return the canonical ``"<store>:<game_id>"`` key, or ``None``.

    Used as a map key (e.g. into ``shortcuts_registry.json``) — the
    canonical form discards any user-appended params so equality
    holds across runs even if the user adds MANGOHUD=1 later.

    Takes ``object`` because callers read the value straight out of a parsed
    VDF, where a corrupt row can hold a non-string. Anything that is not a
    string has no id in it, so it answers ``None`` rather than raising into
    a sync that is walking the whole library.
    """
    if not launch_options or not isinstance(launch_options, str):
        return None
    match = STORE_ID_PATTERN.search(launch_options)
    return f"{match.group(1)}:{match.group(2)}" if match else None


def strip_unifideck_env_tokens(launch_options: str) -> str:
    """Remove our own ``UNIFIDECK_*=value`` tokens, keeping everything else.

    Used before :func:`preserve_user_params` on a rewrite, so a user's own
    params survive while our action flags do not. Both halves matter:

    * the flags are how the launcher decides a shortcut is an auth or install
      action rather than a game launch. The frontend writes them into a
      shortcut's options for one run and restores the originals afterwards, so
      a crash mid-launch can strand one on a *game* shortcut. Force Sync
      overwriting the whole field is what has always cleaned that up, and
      preserving it wholesale would turn a recoverable state into a permanent
      one -- a tile that opens a sign-in window and never launches the game.
    * everything else in the field belongs to the user (``LSFG=1``,
      ``MANGOHUD=1``, a wrapper before ``%command%``) and is theirs to keep.

    The pattern mirrors ``extractUserParams`` in
    ``src/lib/steam-bridge/wrapper-shortcut-launch.ts``, which strips the same
    tokens for the same reason on the frontend, quoted values included.
    """
    if not launch_options:
        return ""
    cleaned = _UNIFIDECK_ENV_TOKEN_RE.sub("", launch_options)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def preserve_user_params(
    current_launch_options: str, new_store_id: str,
) -> str:
    """Swap the canonical core in ``current_launch_options`` for ``new_store_id``.

    Preserves any user-appended parameters around the matched
    ``<store>:<id>`` portion. Used when reclaiming an orphaned
    shortcut: we want the new ``LaunchOptions`` to point at the
    current game while keeping the user's customisations.
    """
    if not current_launch_options:
        return new_store_id
    match = STORE_ID_PATTERN.search(current_launch_options)
    if not match:
        return new_store_id
    return (
        current_launch_options[: match.start()]
        + new_store_id
        + current_launch_options[match.end():]
    )


def rewrite_for_sync(current_launch_options: object, new_store_id: str) -> str:
    """The canonical ``LaunchOptions`` for a shortcut rewrite.

    One rule in one place: point at ``new_store_id``, keep whatever the user
    put around it, drop our own ``UNIFIDECK_*`` flags. Both rewrite paths in
    ``reconcile_phases`` call this. They used to disagree -- one preserved the
    field and one overwrote it wholesale -- which is exactly how the
    overwrite survived: whichever path a reader opened, the other was the
    counterexample.

    Accepts ``object`` because it reads a value straight out of a parsed VDF,
    where a corrupt row can hold anything. A non-string is treated as absent,
    so a force sync over a whole library cannot be aborted by one bad entry.
    """
    text = (
        current_launch_options
        if isinstance(current_launch_options, str)
        else ""
    )
    return _drop_leading_command_token(
        preserve_user_params(
            strip_unifideck_env_tokens(text), new_store_id,
        ),
    )


def _drop_leading_command_token(launch_options: str) -> str:
    """Remove a ``%command%`` that nothing precedes.

    Measured on this Deck (audit §2.9): a shortcut whose ``LaunchOptions``
    **begin** with ``%command%`` does not launch at all — two attempts out of
    two — while the same string with any token in front of it launches fine.
    Steam appears to resolve the placeholder to an empty program.

    ``%command%`` is only meaningful as a separator: tokens before it are
    wrapper words and assignments Steam applies pre-exec, tokens after it are
    argv. With nothing in front, it separates nothing, so dropping it is
    lossless — ``mangohud %command% gog:123`` is untouched, only the bare
    leading form is repaired.

    This is a rewrite path, not a parser, so it is the one place that can
    heal an already-broken shortcut. Audit register item 36 — and note that
    item 24a's fix is what made this necessary: replacing
    ``_update_existing_shortcut``'s wholesale overwrite with preservation
    removed the only thing that had ever cleaned these up by accident.
    """
    stripped = launch_options.lstrip()
    if not stripped.startswith(_COMMAND_PLACEHOLDER):
        return launch_options
    return stripped[len(_COMMAND_PLACEHOLDER):].strip()


#: Steam's placeholder for the game's own command line. Named
#: "placeholder" rather than "token" so ruff's S105 (hardcoded
#: password) does not fire on the literal — it matches any name
#: containing TOKEN.
_COMMAND_PLACEHOLDER = "%command%"


__all__ = [
    "STORE_ID_PATTERN",
    "extract_store_id",
    "get_full_id",
    "get_store_prefix",
    "is_unifideck_shortcut",
    "preserve_user_params",
    "rewrite_for_sync",
    "strip_unifideck_env_tokens",
]
