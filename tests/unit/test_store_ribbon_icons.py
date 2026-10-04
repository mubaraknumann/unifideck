"""Store logos on the ownership ribbon: the SVG allowlist.

The logo data comes from the frontend and becomes elements on the logged-in
Steam Store origin. These tests pin that a real react-icons glyph survives
intact and that nothing executable does: no script or foreign tags, no event
handlers, no references (``href``, ``url(``), no style.
"""
from __future__ import annotations

import re
from typing import Any

import pytest

from unifideck.cdp.store_ribbon_js import _RIBBON_FN_JS
from unifideck.rpc.mixins._store_ribbon_icons import (
    ICON_TAGS,
    sanitize_icon,
    sanitize_store_icons,
)

# Shape of SiEpicgames as the frontend extracts it (path data shortened).
EPIC: dict[str, Any] = {
    "viewBox": "0 0 24 24",
    "nodes": [{"tag": "path", "attrs": {"d": "M3.537 0C2.165 0 1.66.506 1.66 1.879V18.44a4 4 0 0 0 .02.433Z"}}],
}
# GameVaultIcon: a plain <svg> with an evenodd path.
GAMEVAULT: dict[str, Any] = {
    "viewBox": "0 0 24 24",
    "nodes": [{"tag": "path", "attrs": {"fill-rule": "evenodd", "d": "M10.677 10.994C11.408 11.425 Z"}}],
}


def test_a_real_glyph_survives_intact() -> None:
    assert sanitize_icon(EPIC) == EPIC
    assert sanitize_icon(GAMEVAULT) == GAMEVAULT


def test_nested_groups_and_presentation_attributes_are_kept() -> None:
    spec = {
        "viewBox": "0 0 512 512",
        "nodes": [{
            "tag": "g",
            "attrs": {"transform": "translate(1 2) scale(0.5)", "fill": "currentColor"},
            "children": [
                {"tag": "circle", "attrs": {"cx": "4", "cy": "4", "r": "2.5"}},
                {"tag": "rect", "attrs": {"x": "0", "y": "0", "width": "1e2", "height": ".5"}},
            ],
        }],
    }

    assert sanitize_icon(spec) == spec


@pytest.mark.parametrize(
    ("attrs", "kept"),
    [
        ({"d": "M0 0Z", "onload": "alert(1)"}, {"d": "M0 0Z"}),
        ({"d": "M0 0Z", "href": "javascript:alert(1)"}, {"d": "M0 0Z"}),
        ({"d": "M0 0Z", "xlink:href": "#x"}, {"d": "M0 0Z"}),
        ({"d": "M0 0Z", "style": "fill:red"}, {"d": "M0 0Z"}),
        ({"d": "M0 0Z", "fill": "url(#evil)"}, {"d": "M0 0Z"}),
        ({"d": "M0 0Z", "fill": "#ff0000"}, {"d": "M0 0Z"}),
        ({"d": 'M0 0Z" onload="alert(1)'}, {}),
        ({"d": "javascript:alert(1)"}, {}),
        ({"transform": "url(javascript:alert(1))"}, {}),
        ({"transform": "translate(1 2) url(#x)"}, {}),
        ({"fill-rule": "evenodd\n"}, {}),
        ({"cx": "1px"}, {}),
        ({"d": 5}, {}),
    ],
)
def test_attributes_outside_the_allowlist_are_dropped(
    attrs: dict[str, Any], kept: dict[str, str],
) -> None:
    icon = sanitize_icon({"viewBox": "0 0 24 24", "nodes": [{"tag": "path", "attrs": attrs}]})

    assert icon is not None
    assert icon["nodes"][0]["attrs"] == kept


@pytest.mark.parametrize("tag", ["script", "foreignObject", "image", "use", "a", "style", "svg", None])
def test_non_shape_tags_are_dropped(tag: Any) -> None:
    spec = {"viewBox": "0 0 24 24", "nodes": [{"tag": tag, "attrs": {"d": "M0 0Z"}}]}

    assert sanitize_icon(spec) is None


@pytest.mark.parametrize(
    "view_box",
    ["0 0 24", "0 0 24 24 24", '0 0 24 24"', "url(#x)", "", None, 24, "0 0 24 24 Z"],
)
def test_a_bad_view_box_drops_the_icon(view_box: Any) -> None:
    assert sanitize_icon({"viewBox": view_box, "nodes": EPIC["nodes"]}) is None


def test_surrounding_whitespace_on_the_view_box_is_trimmed() -> None:
    icon = sanitize_icon({"viewBox": " 0 0 24 24\n", "nodes": EPIC["nodes"]})

    assert icon is not None and icon["viewBox"] == "0 0 24 24"


def test_depth_and_node_count_are_capped() -> None:
    deep: dict[str, Any] = {"tag": "path", "attrs": {"d": "M0 0Z"}}
    for _ in range(6):
        deep = {"tag": "g", "attrs": {}, "children": [deep]}
    many = [{"tag": "path", "attrs": {"d": "M0 0Z"}} for _ in range(100)]

    def depth(nodes: list[dict[str, Any]]) -> int:
        return 1 + max((depth(n.get("children", [])) for n in nodes), default=0) if nodes else 0

    deep_icon = sanitize_icon({"viewBox": "0 0 1 1", "nodes": [deep]})
    many_icon = sanitize_icon({"viewBox": "0 0 1 1", "nodes": many})
    assert deep_icon is not None and depth(deep_icon["nodes"]) == 4
    assert many_icon is not None and len(many_icon["nodes"]) == 32


def test_store_map_keeps_only_valid_entries() -> None:
    raw = {"epic": EPIC, "gog": {"viewBox": "bad", "nodes": []}, 3: EPIC, "": EPIC, "x" * 40: EPIC}

    assert sanitize_store_icons(raw) == {"epic": EPIC}
    assert sanitize_store_icons(None) == {}
    assert sanitize_store_icons(["epic"]) == {}


def test_the_page_guard_names_the_same_tags() -> None:
    """The in-page regex is defence in depth; it must not drift from the allowlist."""
    found = re.search(r"ICON_TAG_RE = /\^\(\?:([a-z|]+)\)\$/;", _RIBBON_FN_JS)

    assert found is not None
    assert set(found.group(1).split("|")) == ICON_TAGS
