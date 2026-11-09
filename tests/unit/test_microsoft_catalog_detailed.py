"""``fetch_products_detailed`` tells a failed batch from an absent product.

The Xbox PC library must keep its last good rows for ids whose catalog
batch failed, and drop ids the catalog says do not exist. ``fetch_products``
collapsed both into "missing", so it could not. The HTTP call is faked;
nothing here reaches the network.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from unifideck.stores.microsoft import microsoft_catalog as mc


def _product(pid: str) -> dict[str, Any]:
    return {"ProductId": pid, "LocalizedProperties": [{"ProductTitle": pid.title()}]}


@pytest.fixture
def batches(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], str, str]]:
    """Batch size 2. Any batch holding ``FAIL0000000X`` fails as a whole."""
    seen: list[tuple[list[str], str, str]] = []

    def fake(batch: list[str], market: str, languages: str = "en-US") -> list[dict[str, Any]] | None:
        seen.append((list(batch), market, languages))
        if any(pid.startswith("FAIL") for pid in batch):
            return None
        return [_product(pid.lower()) for pid in batch if not pid.startswith("GONE")]

    monkeypatch.setattr(mc, "_TITLE_BATCH_SIZE", 2)
    monkeypatch.setattr(mc, "_fetch_batch_displaycatalog", fake)
    return seen


async def test_failed_batch_ids_are_listed_and_absent_ids_are_not(batches) -> None:
    ids = ["AAAAAAAAAAAA", "GONE00000000", "FAIL0000000X", "BBBBBBBBBBBB"]
    products, failed = await mc.fetch_products_detailed(ids, "IN", languages="hi-IN")
    assert sorted(products) == ["AAAAAAAAAAAA"]
    assert failed == ["FAIL0000000X", "BBBBBBBBBBBB"]
    assert {(market, lang) for _b, market, lang in batches} == {("IN", "hi-IN")}


async def test_fetch_products_keeps_its_old_shape(batches) -> None:
    products = await mc.fetch_products(["AAAAAAAAAAAA", "FAIL0000000X"], "US")
    assert products == {}
    products = await mc.fetch_products(["AAAAAAAAAAAA", "CCCCCCCCCCCC"], "US")
    assert sorted(products) == ["AAAAAAAAAAAA", "CCCCCCCCCCCC"]
    assert batches[-1][2] == "en-US"


async def test_empty_input_makes_no_request(batches) -> None:
    assert await mc.fetch_products_detailed([], "US") == ({}, [])
    assert batches == []


def test_unreadable_body_is_a_failure_not_an_empty_answer() -> None:
    assert mc._products_or_none("<html>busy</html>") is None
    assert mc._products_or_none(json.dumps({"Error": "x"})) is None
    assert mc._products_or_none(json.dumps({"Products": []})) == []
    assert mc._parse_displaycatalog("<html>busy</html>") == []
