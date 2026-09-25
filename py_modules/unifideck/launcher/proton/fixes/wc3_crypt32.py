"""launcher/proton/fixes/wc3_crypt32.py — Warcraft III: Reforged login fix.

Runs inside the out-of-process launcher, under the SYSTEM python (3.10 to
3.14), so this is stdlib-only and must not import the plugin backend.

**The bug is upstream Wine, not ours.** The September 2026 Warcraft III:
Reforged 3.0 expansion (measured on-device at ``3.0.0.24268``) ships a
``ClientSdk.dll`` that calls ``CertCreateCertificateChainEngine()`` with the
current 88-byte ``CERT_CHAIN_ENGINE_CONFIG``. Wine 11.0's ``crypt32``
accepts only the two older layouts — 64 bytes and 80 bytes — and rejects
anything else with ``E_INVALIDARG``. The game cannot build a certificate
chain engine, so it treats Blizzard's perfectly valid TLS as untrusted and
reports it to the user as::

    There was an error in handling the request. Please check your VPN

which sends every reporter chasing a network problem that does not exist.
The launch pipeline is healthy throughout: the client comes up,
``--exec=launch W3`` is issued, the game process appears and reaches its
menu. Upstream fixed it in **Wine 11.6** (April 2026, commits ``02bb0a34``,
``eef8e97d``, ``c7cc9be8``), but every shipping Proton is still on the Wine
11.0 stable branch, so no Proton selection avoids it.

**The patched DLL must go inside the Proton build. This was measured.**

The first attempt dropped the rebuilt ``crypt32.dll`` beside the game's exe
and preferred it with ``crypt32=n,b``, copying the ``ICU_NATIVE_DLLS``
pattern. It loads — and then the game dies instantly::

    ACCESS_VIOLATION (Failed to read address 0x0000000000000040)
        CRYPT32.dll <- ClientSdk.dll <- ClientSdk.dll

Wine's ``crypt32`` is a **split PE/unix DLL**: the PE half in
``lib/wine/x86_64-windows/`` is paired with ``lib/wine/x86_64-unix/
crypt32.so``, and Wine only wires up that unix half for a DLL it loads as
*builtin* from its own directory. Loaded as ``native`` from the game folder
the unix call dispatcher stays null, and the first call through it faults on
a tiny address. So the ICU trick cannot generalise to any split PE/unix
builtin, which is exactly why the community fix rebuilds a whole Proton.

**What we do instead.** Build a private Proton variant out of **hardlinks**
to the real one and replace only the two ``crypt32.dll`` files in it, then
point ``PROTONPATH`` at that variant for this one title. The upstream
installer ``cp -a``s ~1.5 GB; the hardlink tree costs ~1.7 MB of real disk
and about a third of a second, because every other file is shared with the
original inode.

The one rule that makes it safe: **unlink before writing**. Copying over a
hardlink writes *through* it into the real Proton build, corrupting
``crypt32`` for every game and every prefix on the device. :func:`_swap_dll`
unlinks first, every time.

**Only GE-Proton11-6 is patched.** The bundled DLLs were built against that
exact Proton, and a ``crypt32.dll`` has to match the ``crypt32.so`` it is
paired with. Pairing it with a different build's unix half is untested and
would fault the same way the app-directory attempt did, so an unsupported
build is reported to the user rather than guessed at.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# The ``--exec`` family code for Warcraft III. Matched on the family rather
# than the uid because the uid namespace is Blizzard's and unrelated: see
# ``battlenet_client.resolve_family``.
WC3_FAMILY = "W3"

# Proton builds the bundled DLLs were compiled against. This may only grow
# when a DLL is actually rebuilt and tested against the new build — see the
# module docstring on why a mismatched pairing is not safe to assume.
SUPPORTED_PROTON_BUILDS: frozenset[str] = frozenset({"GE-Proton11-6"})

VARIANT_SUFFIX = "-wc3fix"

# Records which source build and which DLLs the variant was made from, so a
# Proton update or a bundled-DLL bump rebuilds it instead of going stale.
MARKER_NAME = ".unifideck_wc3fix"

_BUNDLE_PARTS = ("bin", "stubs", "wc3fix")
_DLL_NAME = "crypt32.dll"

# Present in any crypt32 carrying the upstream backport, and in ours; absent
# from an unfixed one. See :func:`proton_has_crypt32_fix`.
_FIX_MARKER = b"dwExclusiveFlags"

# Wine's PE directory name -> our bundle's directory name. Identical today;
# kept explicit so the mapping is visible rather than assumed.
_ARCH_DIRS: dict[str, str] = {
    "x86_64-windows": "x86_64-windows",
    "i386-windows": "i386-windows",
}


def is_affected_title(family: str | None) -> bool:
    """Whether this Battle.net family needs the cert-chain workaround."""
    return family == WC3_FAMILY


def proton_has_crypt32_fix(proton_dir: Path) -> bool:
    """Whether this Proton's own ``crypt32`` already accepts 88 bytes.

    Asked of the DLL rather than of a version number, because that is the
    thing that actually matters and it retires this whole workaround on its
    own the moment Proton catches up — no allowlist to maintain.

    The probe is the backport's own trace strings. The three upstream
    commits add ``TRACE``/``FIXME`` lines naming ``dwExclusiveFlags``, the
    struct field whose absence was the bug, so a ``crypt32`` carrying the
    fix carries the string and one without it does not. Measured
    2026-09-17: GE-Proton11-6 has none, **GE-Proton11-7 has it** (it picked
    the commits up), and our own patched DLL has it.
    """
    probe = proton_dir / "files" / "lib" / "wine" / "x86_64-windows" / _DLL_NAME
    try:
        return _FIX_MARKER in probe.read_bytes()
    except OSError:
        return False


def variants_root() -> Path:
    """Where our patched Proton variants live.

    Deliberately NOT ``compatibilitytools.d``: this is an implementation
    detail of one game's launch, not a tool the user should be offered in
    Steam's compatibility dropdown. We set ``PROTONPATH`` ourselves, so
    Steam never needs to know it exists.
    """
    return Path("~/.local/share/unifideck/proton").expanduser()


def proton_version_stamp(proton_dir: Path) -> str | None:
    """The raw contents of Proton's own ``version`` file.

    Both fields matter. The build name says whether our DLL was compiled
    against this Proton; the build timestamp beside it says whether the
    *bytes* are the ones we copied from. Keying the variant on the name
    alone let a Proton rebuilt under the same name keep a stale variant.
    """
    try:
        raw = (proton_dir / "version").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return raw or None


def proton_build_name(proton_dir: Path) -> str | None:
    """The build name Proton records in its own ``version`` file.

    Read from the file rather than taken from the directory name, which a
    user can rename freely.
    """
    # "1787951532 GE-Proton11-6" — timestamp first, build name last.
    raw = proton_version_stamp(proton_dir)
    return raw.split()[-1] if raw else None


def is_supported_proton(proton_dir: Path) -> bool:
    """Whether the bundled DLLs were built against this Proton."""
    return proton_build_name(proton_dir) in SUPPORTED_PROTON_BUILDS


def bundled_dll(plugin_dir: Path, arch_dir: str) -> Path | None:
    """The bundled patched DLL for one architecture, or None if absent."""
    candidate = plugin_dir.joinpath(*_BUNDLE_PARTS, arch_dir, _DLL_NAME)
    return candidate if candidate.is_file() else None


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _expected_marker(proton_dir: Path, plugin_dir: Path) -> str | None:
    """Identity of the variant we would build right now.

    Covers the source build *and* both bundled DLLs, so bumping either one
    invalidates an existing variant.
    """
    stamp = proton_version_stamp(proton_dir)
    if stamp is None:
        return None
    parts = [stamp]
    for arch in sorted(_ARCH_DIRS):
        source = bundled_dll(plugin_dir, _ARCH_DIRS[arch])
        if source is None:
            return None
        digest = _digest(source)
        if digest is None:
            return None
        parts.append(f"{arch}:{digest}")
    return "\n".join(parts)


def ensure_patched_proton(plugin_dir: Path, proton_dir: Path) -> Path | None:
    """A Proton variant carrying the patched ``crypt32.dll``, or None.

    None means "not applicable or not possible" — an unsupported Proton
    build, or missing bundled DLLs. The caller tells the user; it must never
    silently fall through, because the game then shows the VPN error.
    """
    if not is_supported_proton(proton_dir):
        logger.warning(
            "[launcher.proton.wc3] %s is not a Proton build the bundled "
            "crypt32.dll was made for (need one of: %s)",
            proton_build_name(proton_dir) or proton_dir.name,
            ", ".join(sorted(SUPPORTED_PROTON_BUILDS)),
        )
        return None

    expected = _expected_marker(proton_dir, plugin_dir)
    if expected is None:
        logger.warning(
            "[launcher.proton.wc3] no bundled crypt32.dll to patch with",
        )
        return None

    variant = variants_root() / f"{proton_dir.name}{VARIANT_SUFFIX}"
    if _variant_is_current(variant, expected):
        logger.info("[launcher.proton.wc3] reusing patched Proton at %s", variant)
        return variant

    return _build_variant(plugin_dir, proton_dir, variant, expected)


def _variant_is_current(variant: Path, expected: str) -> bool:
    """Whether an existing variant was built from exactly these inputs."""
    if not (variant / "proton").exists():
        return False
    try:
        return (variant / MARKER_NAME).read_text(encoding="utf-8") == expected
    except OSError:
        return False


def _build_variant(
    plugin_dir: Path, proton_dir: Path, variant: Path, expected: str,
) -> Path | None:
    """Hardlink the Proton tree, then replace its ``crypt32.dll`` files."""
    guard = variants_root().resolve()
    # Refuse to touch anything outside our own directory. This function
    # deletes its destination, so a wrong path here would be expensive.
    if guard not in variant.resolve().parents:
        logger.error(
            "[launcher.proton.wc3] refusing to build outside %s", guard,
        )
        return None

    logger.info(
        "[launcher.proton.wc3] building patched Proton %s from %s",
        variant.name, proton_dir,
    )
    try:
        shutil.rmtree(variant, ignore_errors=True)
        _hardlink_tree(proton_dir, variant)
        for wine_arch, bundle_arch in _ARCH_DIRS.items():
            source = bundled_dll(plugin_dir, bundle_arch)
            if source is None:
                raise OSError(f"no bundled DLL for {bundle_arch}")
            _swap_dll(source, variant / "files" / "lib" / "wine" / wine_arch / _DLL_NAME)
        (variant / MARKER_NAME).write_text(expected, encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "[launcher.proton.wc3] could not build %s: %s", variant, exc,
        )
        shutil.rmtree(variant, ignore_errors=True)
        return None
    logger.info("[launcher.proton.wc3] patched Proton ready at %s", variant)
    return variant


def _hardlink_tree(source: Path, dest: Path) -> None:
    """Copy a directory tree as hardlinks, preserving symlinks.

    Every regular file becomes a second name for the *same inode*, so the
    tree costs almost no disk. That is only safe because :func:`_swap_dll`
    unlinks before writing; anything that opens one of these paths for
    writing would modify the original Proton build.
    """
    for root, dirs, files in os.walk(source, followlinks=False):
        here = Path(root)
        target_dir = dest / here.relative_to(source)
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copystat(here, target_dir)
        # os.walk lists symlinked directories in `dirs` and (followlinks
        # False) does not descend into them, so they must be recreated as
        # symlinks here or they would be silently dropped.
        for name in list(dirs):
            entry = here / name
            if entry.is_symlink():
                os.symlink(os.readlink(entry), target_dir / name)
                dirs.remove(name)
        for name in files:
            entry = here / name
            if entry.is_symlink():
                os.symlink(os.readlink(entry), target_dir / name)
            else:
                os.link(entry, target_dir / name)


def _swap_dll(source: Path, target: Path) -> None:
    """Replace a hardlinked file with a real copy of ``source``.

    **Unlink first.** ``target`` shares its inode with the real Proton
    build, so copying onto it in place would write through the hardlink and
    replace ``crypt32.dll`` for every game and prefix on the device.
    """
    target.unlink(missing_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o644)
    logger.info("[launcher.proton.wc3] patched %s", target)
