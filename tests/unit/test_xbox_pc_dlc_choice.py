"""``select_dlc_content``: the one PC DLC package of a ``resolve-package --include-dlc`` answer.

Shapes are from a real answer (a DLC owned on the dev account, 2026-11-17):
one supported ``Windows.Desktop`` MSIXVC ``Durable`` choice and one
unsupported ``Windows.Xbox`` XVC choice, both naming the parent game.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.stores.microsoft.xodus.errors import InstallError
from unifideck.stores.microsoft.xodus.package_choice import (
    select_content,
    select_dlc_content,
)

DLC = "9NB77CW5J1TT"
PARENT = "C2KDNLT2H7DM"
CID = "bfedb5f5-cd3c-40b7-9b43-f169b55cbc78"
XBOX_CID = "9be0ce6a-ebc0-4e39-bba2-37ea79edf629"


def durable(cid: str = CID, **over: Any) -> dict[str, Any]:
    choice = {
        "contentId": cid, "downloadBytes": 16941056, "installBytes": None,
        "kind": "Durable", "mainPackageFamilyName": "Example.Base_8wekyb3d8bbwe",
        "packageFormat": "MSIXVC", "parentProductId": PARENT,
        "platform": "Windows.Desktop", "productId": DLC, "supported": True,
        "title": "Example add-on",
    }
    choice.update(over)
    return choice


def answer(*choices: dict[str, Any]) -> dict[str, Any]:
    return {"schema": 1, "rootProductId": DLC, "market": "IN", "status": "choice",
            "choices": list(choices)}


XBOX_ONLY = durable(XBOX_CID, platform="Windows.Xbox", packageFormat="XVC", supported=False)


def test_the_pc_durable_of_the_right_parent_is_picked() -> None:
    assert select_dlc_content(answer(XBOX_ONLY, durable()), DLC, PARENT) == CID


def test_ids_are_compared_without_case() -> None:
    assert select_dlc_content(answer(durable()), DLC.lower(), PARENT.lower()) == CID


@pytest.mark.parametrize("over", [
    {"parentProductId": "9CCCCCCCCCCC"},   # a DLC of another game
    {"parentProductId": None},
    {"supported": False},
    {"platform": "Windows.Xbox"},
    {"kind": "Game"},
    {"productId": "9DDDDDDDDDDD"},
    {"contentId": "not-a-uuid"},
])
def test_anything_else_is_unavailable(over: dict[str, Any]) -> None:
    with pytest.raises(InstallError) as err:
        select_dlc_content(answer(durable(**over)), DLC, PARENT)
    assert err.value.code == "xbox_dlc_unavailable"


def test_two_packages_are_refused_not_guessed() -> None:
    other = "11111111-2222-3333-4444-555555555555"
    with pytest.raises(InstallError) as err:
        select_dlc_content(answer(durable(), durable(other)), DLC, PARENT)
    assert err.value.code == "xbox_dlc_choice"


def test_an_answer_for_another_product_is_refused() -> None:
    wrong = dict(answer(durable()), rootProductId=PARENT)
    with pytest.raises(InstallError) as err:
        select_dlc_content(wrong, DLC, PARENT)
    assert err.value.code == "xbox_resolve_failed"


def test_a_durable_is_never_a_game_install() -> None:
    """The base-game picker still ignores DLC, whatever ``--include-dlc`` returned."""
    with pytest.raises(InstallError) as err:
        select_content(answer(durable()), DLC)
    assert err.value.code == "xbox_package_unavailable"
