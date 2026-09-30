"""Tests for proxy_dll — choosing the OptiScaler proxy DLL from a PE import table.

The core behaviour: read a Windows exe's import table (pure stdlib) and pick a
proxy DLL name the exe actually imports, preferring dxgi (fgmod's default) but
falling back to an imported alternate (e.g. winmm for Nixxes ports that load
dxgi only dynamically). Robustness: malformed/non-PE input must never raise —
it falls back to the dxgi default.
"""
from __future__ import annotations

import struct

from unifideck.launcher.proton.fixes import proxy_dll


def _build_pe(imported_dlls: list[str]) -> bytes:
    """Build a minimal but valid PE32+ with the given imported DLL names.

    Just enough structure for ``_read_imported_dll_names`` to walk: MZ stub,
    PE signature, COFF + optional header (PE32+), one section covering the
    import directory, an import descriptor per DLL, and the name strings.
    Layout is laid out by hand at fixed offsets so RVA==file-offset (the
    single section maps virtual address 0 to raw pointer 0).
    """
    buf = bytearray(4096)
    buf[0:2] = b"MZ"
    e_lfanew = 0x80
    struct.pack_into("<I", buf, 0x3C, e_lfanew)
    buf[e_lfanew:e_lfanew + 4] = b"PE\x00\x00"
    coff = e_lfanew + 4
    num_sections = 1
    opt_size = 240
    struct.pack_into("<H", buf, coff + 2, num_sections)
    struct.pack_into("<H", buf, coff + 16, opt_size)
    opt = coff + 20
    struct.pack_into("<H", buf, opt, 0x20B)  # PE32+ magic

    # Section table follows the optional header. One section, identity map.
    sec = opt + opt_size
    struct.pack_into("<I", buf, sec + 12, 0)        # VirtualAddress = 0
    struct.pack_into("<I", buf, sec + 16, len(buf)) # SizeOfRawData
    struct.pack_into("<I", buf, sec + 20, 0)        # PointerToRawData = 0

    # Lay out import descriptors + name strings in a free region.
    import_off = 0x600
    dd = opt + 112  # data directory start for PE32+
    struct.pack_into("<I", buf, dd + 1 * 8, import_off)  # import dir RVA

    name_cursor = import_off + (len(imported_dlls) + 1) * 20
    for i, dll in enumerate(imported_dlls):
        desc = import_off + i * 20
        struct.pack_into("<I", buf, desc + 12, name_cursor)  # NameRVA
        struct.pack_into("<I", buf, desc + 16, 0x1000 + i)   # FirstThunk (nonzero)
        raw = dll.encode() + b"\x00"
        buf[name_cursor:name_cursor + len(raw)] = raw
        name_cursor += len(raw)
    # Null terminator descriptor is already zeroed.
    return bytes(buf)


def _write(tmp_path, name, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_picks_dxgi_when_imported(tmp_path):
    exe = _write(tmp_path, "g.exe", _build_pe(["dxgi.dll", "kernel32.dll"]))
    assert proxy_dll.pick_proxy_dll(exe) == "dxgi"


def test_picks_winmm_when_dxgi_not_imported(tmp_path):
    # Nixxes-style: winmm imported, dxgi only loaded dynamically (absent).
    exe = _write(tmp_path, "spiderman.exe", _build_pe(["winmm.dll", "winhttp.dll"]))
    assert proxy_dll.pick_proxy_dll(exe) == "winmm"


def test_dxgi_preferred_over_winmm_when_both_imported(tmp_path):
    exe = _write(tmp_path, "g.exe", _build_pe(["winmm.dll", "dxgi.dll"]))
    assert proxy_dll.pick_proxy_dll(exe) == "dxgi"


def test_defaults_to_dxgi_when_no_supported_import(tmp_path):
    exe = _write(tmp_path, "g.exe", _build_pe(["kernel32.dll", "user32.dll"]))
    assert proxy_dll.pick_proxy_dll(exe) == proxy_dll.DEFAULT_PROXY_DLL


def test_non_pe_falls_back_to_default(tmp_path):
    exe = _write(tmp_path, "notpe.exe", b"this is not a PE file at all")
    assert proxy_dll.pick_proxy_dll(exe) == proxy_dll.DEFAULT_PROXY_DLL


def test_missing_file_falls_back_to_default(tmp_path):
    assert proxy_dll.pick_proxy_dll(str(tmp_path / "nope.exe")) == \
        proxy_dll.DEFAULT_PROXY_DLL


def test_override_string_is_native_then_builtin():
    assert proxy_dll.wine_dll_override_for("winmm") == "winmm=n,b"
