"""Payload shaping for the Steam Store ownership ribbon (RPC-private).

Kept out of ``store_ownership.py`` so the endpoint stays a thin check-order
wrapper, and so the rules for what the frontend may send and what the page
is told to draw are testable without a mixin instance.

**Strings come from the frontend.** The ribbon renders inside the store page,
where there is no i18next; the frontend translates once and sends the
strings with each call. They are sanitised here and there is deliberately no
English fallback in Python: a second copy of ``en-US.json`` would drift, and
a call with missing strings is a frontend bug that should draw nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from unifideck.core.cross_store_ownership import OwnedCopy
from unifideck.metadata.unifidb import normalize_title_for_matching
from unifideck.rpc.mixins._metadata_display import read_steam_metadata
from unifideck.rpc.mixins._store_ribbon_icons import sanitize_store_icons

#: Shortcut AppIDs live at 2**31 and above; real Steam AppIDs are far below.
_MAX_STEAM_APP_ID = 2_000_000_000

#: Cap on any string the frontend sends. It is enough for every locale's
#: longest sentence and small enough that a runaway value cannot bloat the page.
_MAX_STRING = 160

_REQUIRED_STRINGS = (
    "tag_owned", "tag_cloud", "message_owned", "message_cloud", "message_xcloud",
    "installed", "via",
)

#: The chip notes the frontend may translate (``RibbonStrings.note_labels``).
_NOTE_KEYS = frozenset({"pc", "console", "pc_console", "play_anywhere", "gold", "cloud"})


@dataclass(frozen=True)
class RibbonStrings:
    """Translated ribbon text, as sanitised from the frontend."""

    tag_owned: str
    tag_cloud: str
    message_owned: str
    #: The Game Pass line ("Xbox Game Pass").
    message_cloud: str
    #: The neutral line for a title that streams for a reason we cannot name
    #: ("Xbox Cloud Gaming"): never says Game Pass, never says owned.
    message_xcloud: str
    installed: str
    via: str
    direction: str
    store_labels: dict[str, str]
    #: Allowlisted logo SVG data per store (see ``_store_ribbon_icons``).
    store_icons: dict[str, dict[str, Any]]
    #: Chip notes for indexed purchases: ``pc``, ``console``, ``pc_console``,
    #: ``play_anywhere``, ``gold``, ``cloud``. Optional: missing ones draw no note.
    note_labels: dict[str, str] = field(default_factory=dict)


def parse_steam_app_id(raw: Any) -> int | None:
    """A positive real Steam AppID from *raw* (int or digit string), else None."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        if not raw.isdigit():
            return None
        raw = int(raw)
    if not isinstance(raw, int) or not 0 < raw < _MAX_STEAM_APP_ID:
        return None
    return raw


def sanitize_ribbon_strings(raw: Any) -> RibbonStrings | None:
    """Validate and cap the frontend's strings; None if any are missing."""
    if not isinstance(raw, dict):
        return None
    values: dict[str, str] = {}
    for key in _REQUIRED_STRINGS:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        values[key] = value.strip()[:_MAX_STRING]
    return RibbonStrings(
        direction="rtl" if raw.get("dir") == "rtl" else "ltr",
        store_labels=_string_map(raw.get("store_labels")),
        store_icons=sanitize_store_icons(raw.get("store_icons")),
        note_labels={
            k: v for k, v in _string_map(raw.get("note_labels")).items() if k in _NOTE_KEYS
        },
        **values,
    )


def _string_map(raw: Any) -> dict[str, str]:
    """A ``{str: non-empty str}`` map with every key and value capped."""
    return {
        key[:_MAX_STRING]: value.strip()[:_MAX_STRING]
        for key, value in (raw.items() if isinstance(raw, dict) else ())
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }


def read_steam_name(cache: Any, steam_app_id: int) -> str:
    """The Steam Store name for *steam_app_id* from the metadata cache, or ``""``."""
    name = read_steam_metadata(cache, steam_app_id).get("name")
    return name.strip() if isinstance(name, str) else ""


def edition_detail(titles: tuple[str, ...], steam_name: str) -> str:
    """The owned title(s) that differ from the Steam name, or ``""``.

    The mapping is a fuzzy title search, so the copy the user owns can be a
    different edition (or, rarely, a different game). Showing the owned
    title lets the user judge; showing it when it matches is just noise.
    Unknown Steam name → nothing to compare against → no detail.
    """
    if not steam_name:
        return ""
    wanted = normalize_title_for_matching(steam_name)
    differing = [t for t in titles if normalize_title_for_matching(t) != wanted]
    return " / ".join(differing)


def build_ribbon_payload(
    steam_app_id: int,
    copies: list[OwnedCopy],
    strings: RibbonStrings,
    steam_name: str,
) -> dict[str, Any]:
    """The data the in-page ribbon script draws (see ``cdp/store_ribbon_js``).

    Purchases and subscription rows are separate sections: a subscription
    row is "playable", never "owned". Game Pass and the neutral cloud line
    are separate too, so an owned game that is also in Game Pass shows both
    facts. The overlay names the purchase stores when there are any,
    otherwise the subscription service.
    """
    owned = [c for c in copies if not c.subscription]
    cloud = [c for c in copies if c.subscription]
    sections: list[dict[str, Any]] = []
    if owned:
        sections.append({
            "kind": "owned",
            "tag": strings.tag_owned,
            "message": strings.message_owned,
            "chips": [_owned_chip(c, strings, steam_name) for c in owned],
        })
    for message, group in (
        (strings.message_cloud, [c for c in cloud if c.game_pass]),
        (strings.message_xcloud, [c for c in cloud if not c.game_pass]),
    ):
        if group:
            sections.append({
                "kind": "cloud",
                "tag": strings.tag_cloud,
                "message": message,
                "chips": _cloud_chips(group, strings, steam_name),
            })
    return {
        "appid": steam_app_id,
        "dir": strings.direction,
        "overlay": _overlay(owned, cloud, strings),
        "sections": sections,
        "installed": strings.installed,
        "via": strings.via,
    }


def _owned_chip(
    copy: OwnedCopy, strings: RibbonStrings, steam_name: str,
) -> dict[str, Any]:
    return {
        "label": strings.store_labels.get(copy.store) or copy.store,
        "detail": edition_detail(copy.titles, steam_name),
        "installed": copy.installed,
        "icon": strings.store_icons.get(copy.store),
        "notes": purchase_notes(copy, strings),
    }


def purchase_notes(copy: OwnedCopy, strings: RibbonStrings) -> list[str]:
    """Where an indexed purchase plays, "Gold" if it needs a subscription,
    and "Cloud" if it streams as the user's own game. Empty for stores
    without an index.

    "Cloud" is never Game Pass: a Game Pass title gets its own line, and
    ``copy.streams`` is only set on an owned copy outside Game Pass.

    No "Cloud" next to "Play Anywhere": Play Anywhere already includes the
    cloud. It stays for a console-only or PC-only purchase that also streams,
    where it is new information.
    """
    labels = strings.note_labels
    notes = []
    if copy.platform and labels.get(copy.platform):
        notes.append(labels[copy.platform])
    if copy.gold and labels.get("gold"):
        notes.append(labels["gold"])
    if copy.streams and copy.platform not in ("", "play_anywhere") and labels.get("cloud"):
        notes.append(labels["cloud"])
    return notes


def _cloud_chips(
    cloud: list[OwnedCopy], strings: RibbonStrings, steam_name: str,
) -> list[dict[str, Any]]:
    """Logo + edition chips: the cloud message already names the service."""
    chips = []
    for copy in cloud:
        detail = edition_detail(copy.titles, steam_name)
        if detail:
            chips.append({
                "label": "",
                "detail": detail,
                "installed": False,
                "icon": strings.store_icons.get(copy.store),
            })
    return chips


def _overlay(
    owned: list[OwnedCopy], cloud: list[OwnedCopy], strings: RibbonStrings,
) -> dict[str, Any]:
    """The tag plus a few words: the capsule strip is narrow in Gaming Mode
    (a sentence was cut to "Included with Game P…"). Purchases name their
    stores; the subscription line names the service ("Xbox Game Pass", or
    "Xbox Cloud Gaming" when it is not Game Pass), because "Xbox" alone read
    as owned."""
    if owned:
        labels = [strings.store_labels.get(c.store) or c.store for c in owned]
        return {"tag": strings.tag_owned, "text": " · ".join(labels)}
    game_pass = any(c.game_pass for c in cloud)
    return {
        "tag": strings.tag_cloud,
        "text": strings.message_cloud if game_pass else strings.message_xcloud,
    }
