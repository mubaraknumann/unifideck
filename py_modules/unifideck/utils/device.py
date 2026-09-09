"""Which Valve device is this — Deck, Steam Machine, or neither.

py_modules/unifideck/utils/device.py

The library's compatibility tab is titled after the hardware it is
filtering for, so it has to name the machine the user is actually
holding. Getting it wrong is not cosmetic: telling a Steam Machine owner
their games are "Great on Deck" names a device they do not own.

**DMI is the only signal that discriminates.** Measured against a real
Steam Machine support bundle (2026-08-03), because two more obvious
signals both look right and are both wrong:

* ``/etc/os-release`` ``VARIANT_ID`` is ``steamdeck`` on a Steam Machine
  too, so "is this SteamOS" answers yes for both devices.
* The ``SteamDeck`` environment variable is session-scoped. The same
  bundle recorded it *empty* on the Machine simply because the probe ran
  outside the gamescope session, and the backend runs outside it too.

What does discriminate is the DMI identity that bundle recorded::

    sys_vendor      "Valve"
    product_name    "Fremont"     (board_name "Fremont", family "HawkPoint")

against a Deck's ``Jupiter`` (LCD) or ``Galileo`` (OLED).

Unknown Valve hardware deliberately resolves to :attr:`DeviceType.OTHER`
rather than being guessed into the nearest match. The cost of the
fallback is a generic-but-true label; the cost of a guess is a wrong
device name on hardware that did not exist when this was written. That
is the same trade the 32-bit Vulkan probe got wrong by inferring driver
support from filenames, and it is worth paying once here.

This module is the **single** answer to "what device is this". Anything
that needs to branch on hardware reads it rather than re-reading DMI --
the support bundle grew its own copy once and that is how two readers
start disagreeing.

Stdlib only, never raises.
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

#: Canonical DMI directory. Public so the support bundle dumps its raw
#: fields from the same place this classifies from, rather than keeping
#: a second path constant that can drift.
DMI_PATH = Path("/sys/devices/virtual/dmi/id")

#: Forces the detected device, for exercising Machine-only code paths on
#: a Deck. Development seam, not a user setting: there is no config key
#: and no UI for it. Same shape as ``UNIFIDECK_GAMESCOPE_DISPLAY``.
_OVERRIDE_ENV = "UNIFIDECK_DEVICE_TYPE"

#: DMI ``sys_vendor`` on Valve hardware. Compared case-insensitively
#: because it is a firmware-authored string, not an API contract.
_VALVE_VENDOR = "valve"

#: ``product_name`` per device. Jupiter is the LCD Deck, Galileo the
#: OLED refresh, Fremont the Steam Machine.
_DECK_PRODUCTS = frozenset({"jupiter", "galileo"})
_MACHINE_PRODUCTS = frozenset({"fremont"})


class DeviceType(Enum):
    """Device class the UI labels itself after.

    A plain ``Enum`` rather than ``StrEnum``: callers send ``.value``
    over RPC explicitly, and ``StrEnum`` would put a 3.11 floor on a
    module that has no other reason to carry one.
    """

    DECK = "deck"
    MACHINE = "machine"
    OTHER = "other"


def _read_dmi(field: str) -> str:
    """Read one ``/sys`` DMI field, lowercased. "" on any failure.

    Absent DMI is normal, not exceptional: containers, VMs and CI have
    none, and this must return a usable answer there rather than raise
    into a UI init path.
    """
    try:
        return (DMI_PATH / field).read_text(encoding="utf-8", errors="replace").strip().lower()
    except OSError:
        return ""


def device_override() -> DeviceType | None:
    """The forced device from ``UNIFIDECK_DEVICE_TYPE``, or ``None``.

    Warns rather than raising on an unrecognised value: a typo in a
    development env var must not take the plugin down, and falling
    through to DMI is the honest answer when the override is unusable.
    """
    raw = os.environ.get(_OVERRIDE_ENV, "").strip().lower()
    if not raw:
        return None
    try:
        forced = DeviceType(raw)
    except ValueError:
        logger.warning(
            "[device] ignoring %s=%r — expected one of %s",
            _OVERRIDE_ENV, raw, [d.value for d in DeviceType],
        )
        return None
    # Deliberately warning, not info: every downstream label, filter and
    # compat record now describes hardware this is not, and a support
    # bundle taken in this state must say so loudly.
    logger.warning(
        "[device] %s=%s — DMI detection overridden", _OVERRIDE_ENV, forced.value,
    )
    return forced


#: Memoised answer. The device cannot change without a reboot, and this
#: is called per-shortcut from the facets builder — 1000 titles meant
#: ~2000 sysfs reads per boot-path RPC, plus one WARNING line per title
#: when the override is set, which drowned the log a developer
#: exercising the Machine path needs to read.
_cached: DeviceType | None = None


def reset_cache() -> None:
    """Drop the memoised answer. For tests only."""
    global _cached
    _cached = None


def detect_device_type() -> DeviceType:
    """Classify the host as Deck, Steam Machine, or neither.

    Memoised — see :data:`_cached`. Cheap enough to call anywhere.
    """
    global _cached
    if _cached is None:
        _cached = _classify()
    return _cached


def _classify() -> DeviceType:
    """The uncached classification.

    Non-Valve hardware short-circuits before ``product_name`` is read:
    a third-party board is free to call itself anything, and matching
    its product name against Valve's would be a collision waiting to
    happen.
    """
    forced = device_override()
    if forced is not None:
        return forced
    vendor = _read_dmi("sys_vendor")
    if vendor != _VALVE_VENDOR:
        return DeviceType.OTHER
    product = _read_dmi("product_name")
    if product in _DECK_PRODUCTS:
        return DeviceType.DECK
    if product in _MACHINE_PRODUCTS:
        return DeviceType.MACHINE
    logger.info(
        "[device] unrecognised Valve product_name %r — treating as generic",
        product,
    )
    return DeviceType.OTHER
