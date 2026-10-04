"""Regression: Microsoft display names degraded to titleId slugs.

``store_game_id`` is sometimes lowercase (e.g. ``brrc2bp0g9p0``) but
``displaycatalog`` returns the canonical UPPERCASE ``ProductId``
(``BRRC2BP0G9P0``), which is what ``fetch_products`` keys on. The old
case-sensitive lookup missed every lowercase id and fell back to the ugly
xCloud ``titleId`` slug ("HALO5", "GEARSOFWAR4", "DEADBYDEADLIGHT") — a
wrong title that then poisoned metadata, compatibility, and artwork search.
``_title_for`` now case-folds the lookup.

The title map is built the way the library builds it
(``_batch_resolve_titles`` → ``fetch_products``), with only the HTTP call
faked.
"""
from __future__ import annotations

import json

import pytest

from unifideck.stores.microsoft import microsoft_catalog as mc
from unifideck.stores.microsoft.microsoft_catalog import _title_for

# displaycatalog normally returns the canonical UPPERCASE ProductId; the
# second product checks that a lowercase one is keyed upper-case too.
_RAW = json.dumps({
    "Products": [
        {
            "ProductId": "BRRC2BP0G9P0",
            "LocalizedProperties": [{"ProductTitle": "Halo 5: Guardians"}],
        },
        {
            "ProductId": "9nblggh1z149",
            "LocalizedProperties": [{"ProductTitle": "Killer Instinct"}],
        },
    ],
})


async def _title_map(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(
        mc, "_fetch_batch_displaycatalog",
        lambda batch, market: mc._parse_displaycatalog(_RAW),
    )
    reader = mc.MicrosoftCatalogReader.__new__(mc.MicrosoftCatalogReader)
    return await reader._batch_resolve_titles(["brrc2bp0g9p0"], "US")


async def test_keys_are_uppercased(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _title_map(monkeypatch) == {
        "BRRC2BP0G9P0": "Halo 5: Guardians",
        "9NBLGGH1Z149": "Killer Instinct",
    }


async def test_title_for_resolves_lowercase_store_game_id(monkeypatch: pytest.MonkeyPatch) -> None:
    tm = await _title_map(monkeypatch)
    # the bug: lookup by the lowercase store_game_id used to miss → "HALO5"
    assert _title_for(tm, "brrc2bp0g9p0", "HALO5") == "Halo 5: Guardians"


async def test_title_for_still_resolves_uppercase(monkeypatch: pytest.MonkeyPatch) -> None:
    tm = await _title_map(monkeypatch)
    assert _title_for(tm, "BRRC2BP0G9P0", "HALO5") == "Halo 5: Guardians"


async def test_title_for_falls_back_to_slug_on_genuine_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    tm = await _title_map(monkeypatch)
    assert _title_for(tm, "NOTINCATALOG1", "SLUGNAME") == "SLUGNAME"


def test_non_json_answer_is_no_products() -> None:
    assert mc._parse_displaycatalog("<html>busy</html>") == []
