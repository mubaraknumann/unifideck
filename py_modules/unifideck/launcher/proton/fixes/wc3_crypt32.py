"""launcher/proton/fixes/wc3_crypt32.py — Warcraft III: Reforged login check.

Runs inside the out-of-process launcher, under the SYSTEM python (3.10 to
3.14), so this is stdlib-only and must not import the plugin backend.

**The bug is upstream Wine, not ours.** The September 2026 Warcraft III:
Reforged 3.0 expansion ships a ``ClientSdk.dll`` that calls
``CertCreateCertificateChainEngine()`` with the current 88-byte
``CERT_CHAIN_ENGINE_CONFIG``. Wine 11.0's ``crypt32`` accepts only the 64-
and 80-byte layouts and rejects anything else with ``E_INVALIDARG``. The
game cannot build a certificate chain engine, so it treats Blizzard's valid
TLS as untrusted and tells the user to "check your VPN". Upstream fixed it
in Wine 11.6 (commits ``02bb0a34``, ``eef8e97d``, ``c7cc9be8``), and
**GE-Proton11-7** is the first Proton measured carrying the backport
(2026-09-17).

**What we do.** Check the selected Proton's own ``crypt32`` for the fix and,
when it is missing, tell the user to pick GE-Proton11-7 or newer. 0.7.6
development builds instead bundled a patched ``crypt32.dll`` for GE-Proton11-6
and built a private Proton variant around it. That DLL is the component
that validates Blizzard's certificate chain, came prebuilt from a
third-party release nobody here could rebuild byte-for-byte, and served
one Proton build that a newer one already fixes, so it was dropped.
:func:`remove_stale_variants` deletes the variants that version left
behind.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

WC3_FAMILY = "W3"
#: Suffix and marker of the patched variants that 0.7.6 development builds made.
VARIANT_SUFFIX = "-wc3fix"
MARKER_NAME = ".unifideck_wc3fix"
_DLL_NAME = "crypt32.dll"

# Present in any crypt32 carrying the upstream backport; absent from an
# unfixed one. See :func:`proton_has_crypt32_fix`.
_FIX_MARKER = b"dwExclusiveFlags"


def is_affected_title(family: str | None) -> bool:
    """Whether this Battle.net family needs the cert-chain check."""
    return family == WC3_FAMILY


def proton_has_crypt32_fix(proton_dir: Path) -> bool:
    """Whether this Proton's own ``crypt32`` already accepts 88 bytes.

    Asked of the DLL rather than of a version number, because that is the
    thing that matters. The three upstream commits add ``TRACE``/``FIXME``
    lines naming ``dwExclusiveFlags``, the struct field whose absence was
    the bug, so a ``crypt32`` carrying the fix carries the string and one
    without it does not. Measured 2026-09-17: GE-Proton11-6 has none,
    GE-Proton11-7 has it.
    """
    probe = proton_dir / "files" / "lib" / "wine" / "x86_64-windows" / _DLL_NAME
    try:
        return _FIX_MARKER in probe.read_bytes()
    except OSError:
        return False


def variants_root() -> Path:
    """Where 0.7.6 development builds put their patched Proton variants."""
    return Path("~/.local/share/unifideck/proton").expanduser()


def remove_stale_variants() -> list[str]:
    """Delete the patched Proton variants a 0.7.6 development build left.

    Only directories named ``*-wc3fix`` that carry our marker are touched.
    They are hardlink trees, so removing one unlinks names and never
    changes the real Proton build the links point into.
    """
    root = variants_root()
    removed: list[str] = []
    if not root.is_dir():
        return removed
    for variant in root.iterdir():
        if (
            variant.name.endswith(VARIANT_SUFFIX)
            and not variant.is_symlink()
            and (variant / MARKER_NAME).is_file()
        ):
            shutil.rmtree(variant, ignore_errors=True)
            removed.append(variant.name)
    if removed:
        logger.info(
            "[launcher.proton.wc3] removed patched Proton variant(s) left by "
            "a 0.7.6 development build: %s", ", ".join(sorted(removed)),
        )
    return removed
