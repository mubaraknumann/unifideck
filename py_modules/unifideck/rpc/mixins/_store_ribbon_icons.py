"""Store logos for the ownership ribbon: the allowlist for frontend SVG data.

The frontend reads each store's logo out of the same react-icons glyphs
``<StoreIcon>`` renders and sends it as data, ``{viewBox, nodes: [{tag,
attrs, children?}]}``; the store page rebuilds it with ``createElementNS``.
That data ends up as elements on the logged-in Steam Store origin, so this
module is the **one authority** on what may: shape tags only, presentation
attributes only, each value matched against a pattern that admits no quote,
colon or ``url(``. Nothing here can carry a script, an event handler or a
reference to another document. Anything outside the allowlist is dropped
silently; a store whose logo does not survive keeps the ribbon's plain dot.
"""
from __future__ import annotations

import re
from typing import Any

#: The shapes a logo is made of. The in-page script repeats this set as a
#: guard regex (``ICON_TAG_RE``); a test pins the two together.
ICON_TAGS = frozenset({"path", "circle", "ellipse", "rect", "polygon", "polyline", "line", "g"})

_NUMBER = re.compile(r"^-?(?:\d+\.?\d*|\.\d+)(?:[eE]-?\d+)?$")
_PAINT = re.compile(r"^(?:none|currentColor)$")
_RULE = re.compile(r"^(?:nonzero|evenodd)$")
_NUMERIC_ATTRS = (
    "cx", "cy", "r", "rx", "ry", "x", "y", "width", "height",
    "x1", "y1", "x2", "y2", "opacity", "fill-opacity", "stroke-width",
)

_ATTR_PATTERNS: dict[str, re.Pattern[str]] = {
    "d": re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9eE.,\s+-]{1,12000}$"),
    "points": re.compile(r"^[0-9eE.,\s+-]{1,4000}$"),
    "transform": re.compile(
        r"^(?:\s*(?:matrix|translate|scale|rotate|skewX|skewY)\([0-9eE.,\s+-]{0,80}\))+\s*$",
    ),
    "fill-rule": _RULE,
    "clip-rule": _RULE,
    "fill": _PAINT,
    "stroke": _PAINT,
    **dict.fromkeys(_NUMERIC_ATTRS, _NUMBER),
}

_VIEWBOX = re.compile(r"^-?\d+(?:\.\d+)?(?:[ ,]+-?\d+(?:\.\d+)?){3}$")
_MAX_DEPTH = 3
_MAX_NODES = 32
_MAX_STORES = 16


def sanitize_store_icons(raw: Any) -> dict[str, dict[str, Any]]:
    """``{store: icon}`` keeping only icons that survive :func:`sanitize_icon`."""
    if not isinstance(raw, dict):
        return {}
    icons: dict[str, dict[str, Any]] = {}
    for store, spec in list(raw.items())[:_MAX_STORES]:
        if not isinstance(store, str) or not 0 < len(store) <= 32:
            continue
        icon = sanitize_icon(spec)
        if icon is not None:
            icons[store] = icon
    return icons


def sanitize_icon(spec: Any) -> dict[str, Any] | None:
    """One logo reduced to allowlisted shapes, or None if nothing drawable is left."""
    if not isinstance(spec, dict):
        return None
    view_box = spec.get("viewBox")
    if not isinstance(view_box, str) or not _VIEWBOX.fullmatch(view_box.strip()):
        return None
    budget = [_MAX_NODES]
    nodes = _clean_nodes(spec.get("nodes"), 0, budget)
    if not nodes:
        return None
    return {"viewBox": view_box.strip(), "nodes": nodes}


def _clean_nodes(raw: Any, depth: int, budget: list[int]) -> list[dict[str, Any]]:
    """Allowlisted nodes from *raw*; *budget* caps the total across all depths."""
    if depth > _MAX_DEPTH or not isinstance(raw, list):
        return []
    nodes: list[dict[str, Any]] = []
    for node in raw:
        if budget[0] <= 0:
            break
        if not isinstance(node, dict) or node.get("tag") not in ICON_TAGS:
            continue
        budget[0] -= 1
        clean: dict[str, Any] = {"tag": node["tag"], "attrs": _clean_attrs(node.get("attrs"))}
        children = _clean_nodes(node.get("children"), depth + 1, budget)
        if children:
            clean["children"] = children
        nodes.append(clean)
    return nodes


def _clean_attrs(raw: Any) -> dict[str, str]:
    """Attributes whose name is allowlisted and whose value matches its pattern."""
    if not isinstance(raw, dict):
        return {}
    attrs: dict[str, str] = {}
    for name, value in raw.items():
        pattern = _ATTR_PATTERNS.get(name) if isinstance(name, str) else None
        if pattern is not None and isinstance(value, str) and pattern.fullmatch(value):
            attrs[name] = value
    return attrs
