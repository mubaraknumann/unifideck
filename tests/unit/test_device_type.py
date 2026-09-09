"""Device classification from DMI.

The Fremont case is the one that matters: it is the only value here
taken from real hardware (a Steam Machine support bundle, 2026-08-03)
rather than from documentation, and getting it wrong mislabels the
library tab on a device we cannot test against.
"""

from __future__ import annotations

import pytest

from unifideck.utils import device
from unifideck.utils.device import DeviceType, detect_device_type


@pytest.fixture
def dmi(tmp_path, monkeypatch):
    """Point the module at a fake ``/sys`` DMI directory.

    Also clears the developer override, so a shell that happens to
    export ``UNIFIDECK_DEVICE_TYPE`` cannot make these pass for the
    wrong reason.
    """
    monkeypatch.delenv("UNIFIDECK_DEVICE_TYPE", raising=False)
    device.reset_cache()

    def _write(**fields: str):
        for name, value in fields.items():
            (tmp_path / name).write_text(value, encoding="utf-8")
        monkeypatch.setattr(device, "DMI_PATH", tmp_path)
        device.reset_cache()      # memoised — drop any earlier answer
        return tmp_path

    return _write


@pytest.mark.parametrize(
    ("product", "expected"),
    [
        ("Jupiter", DeviceType.DECK),  # Steam Deck LCD
        ("Galileo", DeviceType.DECK),  # Steam Deck OLED
        ("Fremont", DeviceType.MACHINE),  # Steam Machine (measured)
    ],
)
def test_valve_hardware_is_classified(dmi, product, expected):
    dmi(sys_vendor="Valve", product_name=product)
    assert detect_device_type() is expected


def test_dmi_values_are_matched_case_insensitively(dmi):
    """Firmware authors these strings; they are not an API contract."""
    dmi(sys_vendor="VALVE", product_name="JUPITER")
    assert detect_device_type() is DeviceType.DECK


def test_unknown_valve_product_falls_back_rather_than_guessing(dmi):
    """Future Valve hardware must degrade to a true generic label."""
    dmi(sys_vendor="Valve", product_name="SomeUnreleasedThing")
    assert detect_device_type() is DeviceType.OTHER


def test_third_party_vendor_is_other_even_with_a_colliding_product(dmi):
    """A non-Valve board is free to call itself anything at all."""
    dmi(sys_vendor="Acme Corp", product_name="Fremont")
    assert detect_device_type() is DeviceType.OTHER


def test_absent_dmi_is_other_not_an_exception(tmp_path, monkeypatch):
    """Containers, VMs and CI have no DMI. This runs in a UI init path."""
    monkeypatch.delenv("UNIFIDECK_DEVICE_TYPE", raising=False)
    monkeypatch.setattr(device, "DMI_PATH", tmp_path / "does-not-exist")
    device.reset_cache()
    assert detect_device_type() is DeviceType.OTHER


def test_device_type_serialises_as_a_plain_string():
    """The RPC boundary sends ``.value`` with no conversion step."""
    assert DeviceType.MACHINE.value == "machine"
    assert [d.value for d in DeviceType] == ["deck", "machine", "other"]


def test_support_bundle_reports_the_derived_type(dmi):
    """A bundle should state the device class, not just its codename."""
    from unifideck.services.support_bundle import probe_device

    root = dmi(sys_vendor="Valve", product_name="Fremont")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(probe_device, "DMI_PATH", root)
    try:
        block = probe_device.device_block()
        assert block["device_type"] == "machine"
        # Nothing forced it, so the bundle must not claim otherwise.
        assert "device_type_forced" not in block
    finally:
        monkey.undo()


# --- developer override -------------------------------------------------
#
# The override exists so Machine-only paths can be exercised on a Deck.
# It is load-bearing for that rehearsal, so it is tested like a feature.


@pytest.mark.parametrize(
    ("value", "expected"),
    [("deck", DeviceType.DECK),
     ("machine", DeviceType.MACHINE),
     ("other", DeviceType.OTHER)],
)
def test_override_wins_over_dmi(dmi, monkeypatch, value, expected):
    """Real Deck DMI underneath; the override still decides."""
    dmi(sys_vendor="Valve", product_name="Jupiter")
    monkeypatch.setenv("UNIFIDECK_DEVICE_TYPE", value)
    device.reset_cache()
    assert detect_device_type() is expected


def test_override_is_case_and_space_insensitive(dmi, monkeypatch):
    """It gets typed by hand into a systemd unit, not generated."""
    dmi(sys_vendor="Valve", product_name="Jupiter")
    monkeypatch.setenv("UNIFIDECK_DEVICE_TYPE", "  MACHINE ")
    device.reset_cache()
    assert detect_device_type() is DeviceType.MACHINE


def test_unparseable_override_falls_through_to_dmi(dmi, monkeypatch):
    """A typo must not take the plugin down, and must not lie either."""
    dmi(sys_vendor="Valve", product_name="Jupiter")
    monkeypatch.setenv("UNIFIDECK_DEVICE_TYPE", "steammachine")
    device.reset_cache()
    assert detect_device_type() is DeviceType.DECK
    assert device.device_override() is None


def test_empty_override_is_not_an_override(dmi, monkeypatch):
    """An exported-but-empty var is how shells leave a cleared setting."""
    dmi(sys_vendor="Valve", product_name="Jupiter")
    monkeypatch.setenv("UNIFIDECK_DEVICE_TYPE", "")
    device.reset_cache()
    assert detect_device_type() is DeviceType.DECK


def test_support_bundle_flags_a_forced_device(dmi, monkeypatch):
    """A forced bundle must say so, or its DMI fields read as a lie."""
    from unifideck.services.support_bundle import probe_device

    root = dmi(sys_vendor="Valve", product_name="Jupiter")
    monkeypatch.setattr(probe_device, "DMI_PATH", root)
    monkeypatch.setenv("UNIFIDECK_DEVICE_TYPE", "machine")
    device.reset_cache()
    block = probe_device.device_block()
    assert block["device_type"] == "machine"
    assert block["device_type_forced"] is True
    # The raw DMI is still reported truthfully alongside it, verbatim —
    # only the classifier lowercases, the bundle preserves what firmware
    # wrote.
    assert block["product_name"] == "Jupiter"
