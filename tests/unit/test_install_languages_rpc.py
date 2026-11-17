"""``get_install_languages``: one language RPC for every store whose store
object answers ``get_install_language_options`` (Epic, EA).

An empty label leaves the name to the modal; a store outside the picker set,
or a lookup that raises, fails open to an empty list so the install proceeds
with the store's default.
"""
from __future__ import annotations

from types import SimpleNamespace

from unifideck.rpc.mixins.download import DownloadRPCMixin


class _Store:
    install_language_follows_ui = False

    def __init__(self, options: dict[str, str] | Exception) -> None:
        self.options = options
        self.asked: list[str] = []

    async def get_install_language_options(self, game_id: str) -> dict[str, str]:
        self.asked.append(game_id)
        if isinstance(self.options, Exception):
            raise self.options
        return self.options


def _host(store: object) -> DownloadRPCMixin:
    host = DownloadRPCMixin.__new__(DownloadRPCMixin)
    host.registry = SimpleNamespace(get_store=lambda _name: store)  # type: ignore[attr-defined]
    return host


async def test_ea_codes_come_back_without_labels() -> None:
    store = _Store({"en-US": "", "de-DE": ""})
    store.install_language_follows_ui = True
    result = await _host(store).get_install_languages("ea", "OFB-EAST__1000017")
    assert result == {"success": True, "languages": ["en-US", "de-DE"], "labels": {}, "follow_ui": True}
    assert store.asked == ["OFB-EAST__1000017"]


async def test_epic_labels_are_kept() -> None:
    store = _Store({"en": "English", "de": "Deutsch"})
    result = await _host(store).get_install_languages("epic", "Hogwarts")
    assert result["languages"] == ["en", "de"]
    assert result["labels"] == {"en": "English", "de": "Deutsch"}
    # An empty Epic list means "base game only": never swapped for the UI language.
    assert result["follow_ui"] is False


async def test_a_store_without_a_picker_is_never_asked() -> None:
    store = _Store({"en-US": ""})
    result = await _host(store).get_install_languages("amazon", "x")
    assert result["languages"] == [] and result["success"] is False
    assert store.asked == []


async def test_a_failed_lookup_fails_open() -> None:
    result = await _host(_Store(RuntimeError("EA unreachable"))).get_install_languages("ea", "x")
    assert result["success"] is False and result["languages"] == []
