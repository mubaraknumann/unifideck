"""Opaque game IDs bind server-local ROM numbers to one configured URL.

RomM numbers ROMs per server, so a bare ``42`` is a different game on every
server a user might connect. An ID therefore carries a hash of the normalized
server URL as its prefix, which keeps an ID stable across a reconnect to the
same address while making a different address a different library: an install
recorded against one server can never be matched to a ROM on another.
"""
from __future__ import annotations

import hashlib

from .client import RomMAPIError, normalize_server_url


def _server_namespace(server_url: str) -> str:
    return hashlib.sha256(normalize_server_url(server_url).encode()).hexdigest()


def scoped_game_id(server_url: str, rom_id: int) -> str:
    """The opaque ID for *rom_id* on *server_url*.

    Rejects anything that is not a positive ``int``, ``True`` included: a bool
    passes ``isinstance(x, int)``, and a malformed payload must not become a
    plausible-looking game ID.
    """
    if isinstance(rom_id, bool) or not isinstance(rom_id, int) or rom_id <= 0:
        raise RomMAPIError("Invalid RomM ROM ID", code="invalid_id")
    return f"{_server_namespace(server_url)}_{rom_id}"


def rom_id_for_server(server_url: str, game_id: str) -> int:
    """The ROM number named by *game_id*, or raise if it is not this server's.

    The prefix is compared against this server's own namespace, so an ID
    created for another address cannot be resolved here even when its numeric
    suffix is valid.

    The suffix is parsed strictly: ASCII decimal digits, no sign, no leading
    zeros, and it must round-trip through ``int``. Text that merely looks
    numeric is rejected, so a damaged or fabricated ID cannot resolve to a ROM
    other than the one it names.
    """
    prefix, separator, suffix = game_id.partition("_")
    if not separator or prefix != _server_namespace(server_url):
        raise RomMAPIError("This game belongs to a different RomM server", code="wrong_server")
    if not suffix.isascii() or not suffix.isdecimal() or int(suffix) <= 0 or str(int(suffix)) != suffix:
        raise RomMAPIError("Invalid RomM game ID", code="invalid_id")
    return int(suffix)
