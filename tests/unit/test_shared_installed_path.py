"""One rule for "is this a usable install directory?" — audit item 48.

GOG's and Ubisoft's ``get_installed_path`` bodies were byte-identical apart
from the docstring, and Amazon's was the same shape on a different key. What
those three shared was the *guard*, and the guard is where a live defect was
found: §3.4 records that ``amazon_library.read_installed_ids`` defaults a
missing path to ``""``, and a falsy-but-present value flowed through as if it
were a real directory — the same class as the Vampire Survivors case where
Steam showed PLAY for a game with no files.

The fetch is deliberately NOT shared. GOG and Ubisoft scan in a thread,
Amazon awaits a map and indexes it, Epic goes through legendary's own reader,
Battle.net through its id-map. §3.2's lesson is that the majority
implementation is not automatically the right one, so only the part that was
genuinely identical moved.
"""
from __future__ import annotations

import pytest

from unifideck.stores.shared.installed_path import (
    DEFAULT_PATH_KEY,
    install_path_from_record,
)


def test_a_real_path_comes_back() -> None:
    assert install_path_from_record({"install_path": "/games/x"}) == "/games/x"


def test_the_amazon_key_is_a_parameter_not_a_second_function() -> None:
    """nile calls the field ``path``; that literal is the whole difference."""
    assert install_path_from_record({"path": "/games/y"}, key="path") == "/games/y"


@pytest.mark.parametrize(
    "record",
    [
        pytest.param({"install_path": ""}, id="empty-string-the-live-defect"),
        pytest.param({"install_path": None}, id="explicit-null"),
        pytest.param({"install_path": 0}, id="not-a-string"),
        pytest.param({"install_path": []}, id="wrong-container"),
        pytest.param({}, id="key-absent"),
        pytest.param(None, id="no-record"),
        pytest.param([], id="json-array-not-object"),
        pytest.param("nonsense", id="json-string-not-object"),
    ],
)
def test_an_unusable_record_yields_none(record: object) -> None:
    assert install_path_from_record(record) is None


def test_an_empty_string_is_not_a_directory() -> None:
    """Called out on its own because it is the case that shipped.

    A blank path is falsy but *present*, so a plain ``.get(key)`` returns it
    and every downstream check that only tested for ``None`` let it through.
    """
    assert install_path_from_record({"path": ""}, key="path") is None


def test_a_json_array_does_not_raise() -> None:
    """§3.2: Amazon called ``.get`` on whatever ``json.load`` returned.

    A ``user.json`` holding an array raised ``AttributeError`` out of the
    store-status path. A malformed file must not break a status refresh.
    """
    assert install_path_from_record([{"path": "/x"}], key="path") is None


def test_the_other_key_is_not_read_by_accident() -> None:
    """Asking for ``path`` must not silently fall back to ``install_path``."""
    record = {"install_path": "/gog/style"}
    assert install_path_from_record(record, key="path") is None
    assert install_path_from_record(record) == "/gog/style"


def test_the_default_key_is_the_gog_ubisoft_one() -> None:
    assert DEFAULT_PATH_KEY == "install_path"
