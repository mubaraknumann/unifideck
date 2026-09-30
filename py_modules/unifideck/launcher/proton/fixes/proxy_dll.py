"""proxy_dll.py — pick the OptiScaler proxy DLL that a game's exe will load.

OptiScaler (and the DLSS-Enabler / fgmod stack) works by shipping a *proxy*
DLL next to the game executable. Windows' loader only pulls that proxy in if
the executable **statically imports** a DLL of that exact name — the proxy
sits in the import chain, forwards the real exports, and hooks the graphics
API on the way. If the game never statically imports the chosen proxy name,
the proxy simply never loads: no OptiScaler overlay, no FSR/DLSS injection,
no ``OptiScaler.log`` — even when ``WINEDLLOVERRIDES=<name>=n,b`` is set.

fgmod defaults to ``dxgi.dll``. That is correct for most DX12 titles, but a
real, common failure mode is a title that loads ``dxgi`` **dynamically**
(via ``LoadLibrary`` / ``CreateDXGIFactory``) and therefore does NOT list it
in its import table — Nixxes' PC ports (e.g. Marvel's Spider-Man Remastered)
are exactly this case: the import table carries ``winmm.dll`` but not
``dxgi.dll``. Installed as ``dxgi.dll`` the proxy is dead weight; installed
as ``winmm.dll`` it loads. OptiScaler documents this "rename the proxy to a
DLL the game imports" workaround; this module automates the choice.

We read the PE import table directly (no external dependency) and return the
best proxy DLL name from the set OptiScaler supports, preferring a DLL the
exe actually imports. ``dxgi`` stays the default when nothing better is found
so behaviour is unchanged for the DX12 titles fgmod already handled.
"""
from __future__ import annotations

import logging
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

# Proxy DLL names OptiScaler can masquerade as, in PREFERENCE order. dxgi
# first because it is the documented default and correct for most DX12
# games; the rest are the alternates OptiScaler's own docs list for titles
# that don't import dxgi statically. Order matters: the first one the exe
# actually imports wins.
SUPPORTED_PROXY_DLLS: tuple[str, ...] = (
    "dxgi",
    "winmm",
    "version",
    "dbghelp",
    "dinput8",
    "wininet",
    "winhttp",
)

# Fallback when the import table can't be read or matches nothing — keeps
# fgmod's historical default so this never regresses a working DX12 title.
DEFAULT_PROXY_DLL = "dxgi"

# Bound how much of the file we scan; import tables live early in the PE.
_MAX_PE_READ = 8 * 1024 * 1024


def _read_imported_dll_names(exe_path: str) -> set[str]:
    """Return the lower-cased DLL names in ``exe_path``'s PE import table.

    Pure stdlib PE parsing — walks the optional header to the import
    directory and reads each descriptor's name. Best-effort: any malformed
    or unexpected structure returns an empty set (caller falls back to the
    default), never raises. Names are returned WITHOUT the ``.dll`` suffix,
    lower-cased, to match ``SUPPORTED_PROXY_DLLS`` keys.
    """
    try:
        data = Path(exe_path).read_bytes()[:_MAX_PE_READ]
    except OSError:
        return set()
    try:
        return _parse_import_names(data)
    except (struct.error, IndexError, ValueError):
        # Malformed / truncated PE — treat as "no info", not an error.
        return set()


def _parse_import_names(data: bytes) -> set[str]:
    """Parse imported DLL names from raw PE ``data`` (may raise on garbage)."""
    if data[:2] != b"MZ":
        return set()
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        return set()
    coff = e_lfanew + 4
    num_sections = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    magic = struct.unpack_from("<H", data, opt)[0]
    # PE32 (0x10b): data directories start at opt+96; PE32+ (0x20b): opt+112.
    if magic == 0x10B:
        dd = opt + 96
    elif magic == 0x20B:
        dd = opt + 112
    else:
        return set()
    # Import directory is data-directory entry index 1 (RVA, size).
    import_rva = struct.unpack_from("<I", data, dd + 1 * 8)[0]
    if not import_rva:
        return set()
    sections = _section_table(data, opt + opt_size, num_sections)
    import_off = _rva_to_offset(import_rva, sections)
    if import_off is None:
        return set()
    return _walk_import_descriptors(data, import_off, sections)


def _section_table(
    data: bytes, off: int, count: int,
) -> list[tuple[int, int, int]]:
    """Return ``(virtual_addr, raw_size, raw_ptr)`` for each section."""
    out: list[tuple[int, int, int]] = []
    for i in range(count):
        base = off + i * 40
        vaddr = struct.unpack_from("<I", data, base + 12)[0]
        raw_size = struct.unpack_from("<I", data, base + 16)[0]
        raw_ptr = struct.unpack_from("<I", data, base + 20)[0]
        out.append((vaddr, raw_size, raw_ptr))
    return out


def _rva_to_offset(
    rva: int, sections: list[tuple[int, int, int]],
) -> int | None:
    """Map a virtual address to a raw file offset via the section table."""
    for vaddr, raw_size, raw_ptr in sections:
        if vaddr <= rva < vaddr + max(raw_size, 1):
            return raw_ptr + (rva - vaddr)
    return None


def _walk_import_descriptors(
    data: bytes, off: int, sections: list[tuple[int, int, int]],
) -> set[str]:
    """Read each 20-byte import descriptor's DLL name until the null entry."""
    names: set[str] = set()
    while True:
        # Descriptor layout: [OFTs, TimeDateStamp, Forwarder, NameRVA, FThunk]
        name_rva = struct.unpack_from("<I", data, off + 12)[0]
        first_thunk = struct.unpack_from("<I", data, off + 16)[0]
        if name_rva == 0 and first_thunk == 0:
            break
        name_off = _rva_to_offset(name_rva, sections)
        if name_off is not None:
            end = data.find(b"\x00", name_off)
            raw = data[name_off:end] if end != -1 else b""
            name = raw.decode("ascii", errors="ignore").lower()
            if name.endswith(".dll"):
                names.add(name[:-4])
        off += 20
        if len(names) > 512:  # pathological guard
            break
    return names


def pick_proxy_dll(exe_path: str) -> str:
    """Choose the OptiScaler proxy DLL name ``exe_path`` will actually load.

    Returns a name WITHOUT the ``.dll`` suffix (e.g. ``"winmm"``), suitable
    both as the proxy filename stem and as the ``WINEDLLOVERRIDES`` key.
    Prefers, in ``SUPPORTED_PROXY_DLLS`` order, a DLL the exe statically
    imports; falls back to :data:`DEFAULT_PROXY_DLL` (``dxgi``) when the
    import table yields nothing usable, so DX12 titles keep working exactly
    as before.
    """
    imported = _read_imported_dll_names(exe_path)
    if not imported:
        logger.info(
            "[proxy_dll] no import info for %s — defaulting to %s",
            exe_path, DEFAULT_PROXY_DLL,
        )
        return DEFAULT_PROXY_DLL
    for candidate in SUPPORTED_PROXY_DLLS:
        if candidate in imported:
            logger.info(
                "[proxy_dll] %s statically imports %s.dll — using it as proxy",
                exe_path, candidate,
            )
            return candidate
    logger.info(
        "[proxy_dll] %s imports none of the supported proxies — defaulting "
        "to %s (imports=%s)",
        exe_path, DEFAULT_PROXY_DLL, sorted(imported),
    )
    return DEFAULT_PROXY_DLL


def wine_dll_override_for(proxy: str) -> str:
    """Return the ``WINEDLLOVERRIDES`` value that makes Wine load the proxy.

    Native-then-builtin (``=n,b``) so the game's own (OptiScaler) copy loads
    first, with Wine's builtin as fallback — the same shape used elsewhere
    in the codebase (Rockstar ``vulkan-1=n,b``, native-ICU ``icuuc=n,b``).
    """
    return f"{proxy}=n,b"
