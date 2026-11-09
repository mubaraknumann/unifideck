"""Guard test — every failure row key has a short toast twin.

``src/lib/download-errors.ts`` and ``src/lib/xbox-pc-errors.ts`` classify a
failure once and render it two ways: the row shows the key's sentence, and
the toast shows its twin from the file's ``TOAST_KEYS`` map, which fits a
toast line. A row key added without a twin falls back to the generic toast
reason, so this pins the pairing (vitest does not run in CI).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_EN = _ROOT / "src" / "i18n" / "locales" / "en-US.json"
_TWIN = re.compile(r'"([\w.]+)":\s*"(toasts\.[\w.]+)"')

#: source file -> the pattern of its row keys
_SOURCES = {
    "src/lib/download-errors.ts": r'"((?:errors\.download|xboxPc\.account\.errors)\.\w+)"',
    "src/lib/xbox-pc-errors.ts": r'"(xboxPc\.(?:runtime|account|section|errors)[\w.]*)"',
}


def _lookup(tree: dict[str, object], key: str) -> object:
    node: object = tree
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


@pytest.mark.parametrize("source", sorted(_SOURCES))
def test_every_row_key_has_a_toast_twin(source: str) -> None:
    text = (_ROOT / source).read_text(encoding="utf-8")
    twins = dict(_TWIN.findall(text))
    rows = set(re.findall(_SOURCES[source], text)) - set(twins.values())
    assert rows, "no row keys found; the pattern no longer matches the source"
    assert sorted(rows - set(twins)) == []
    en = json.loads(_EN.read_text(encoding="utf-8"))
    assert sorted(t for t in twins.values() if not isinstance(_lookup(en, t), str)) == []
