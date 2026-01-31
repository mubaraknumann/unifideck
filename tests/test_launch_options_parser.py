from __future__ import annotations

import launch_options_parser as lop


def test_extract_store_id_basic() -> None:
    assert lop.extract_store_id("epic:4141431341") == ("epic", "4141431341")


def test_extract_store_id_with_prefix_and_suffix() -> None:
    assert lop.extract_store_id("MANGOHUD=1 gog:abc-123 --no-splash") == (
        "gog",
        "abc-123",
    )


def test_extract_store_id_amazon_format() -> None:
    launch_options = (
        "PROTON_LOG=1 amazon:amzn1.adg.product.8a584db6-f8e5-4bfa-87a8-256a9d3506c4"
    )
    assert lop.extract_store_id(launch_options) == (
        "amazon",
        "amzn1.adg.product.8a584db6-f8e5-4bfa-87a8-256a9d3506c4",
    )


def test_extract_store_id_none() -> None:
    assert lop.extract_store_id("--some-random-option") is None


def test_is_unifideck_shortcut() -> None:
    assert lop.is_unifideck_shortcut("LSFG=1 epic:game123") is True
    assert lop.is_unifideck_shortcut("--custom-options") is False


def test_get_store_prefix() -> None:
    assert lop.get_store_prefix("MANGOHUD=1 gog:12345") == "gog"
    assert lop.get_store_prefix("--no-store-here") is None


def test_get_full_id() -> None:
    assert lop.get_full_id("PROTON_LOG=1 epic:abc123 --skip-launcher") == "epic:abc123"
    assert lop.get_full_id("random text") is None
