"""Guard test — bundled-binary versions must agree across all three sources.

A bundled store CLI is pinned in three places that nothing links together:

1. ``package.json`` ``remote_binary[]`` — URL + ``sha256hash``. Decky reads
   this at install time and verifies the download against the hash.
2. ``build-plugin.sh`` ``<TOOL>_URL`` — the same URLs, hardcoded a second
   time because ``prebuild_binaries()`` runs before any JSON parsing is
   available to it. Its own header comment says the two "must stay in sync"
   and points at this test.
3. ``core/binaries/binary_signatures.py`` ``_KNOWN_HASHES`` — the runtime
   check that the file on disk is the pinned version, whose docstring
   requires updating "IN THE SAME COMMIT as the binary update".

Three hand-maintained copies of the same fact drift the moment someone
bumps a version and updates two of them. The failure is quiet and nasty:
the build downloads one version while the manifest advertises another, so
Decky's install-time hash check rejects it, or the runtime signature check
reports a mismatch on a binary that is perfectly fine.

De-duplicating these properly is roadmap item #7. Until then this test is
the seam that makes a partial bump fail loudly, in CI, immediately.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

# Tools pinned in all three places. umu is deliberately absent: it is
# committed to the repo rather than downloaded, so it has no remote_binary
# entry and no URL in build-plugin.sh (its version lives in bin/umu/VERSION).
_TRIPLE_PINNED = ("legendary", "gogdl", "nile")


def _repo_file(relative: str) -> Path | None:
    """Locate ``relative`` in the checkout, or ``None``."""
    from tests.unit._repo_root import find_repo_file

    return find_repo_file(relative)


@pytest.fixture(scope="module")
def manifest() -> dict[str, dict[str, str]]:
    """``remote_binary`` entries from package.json, keyed by tool name."""
    path = _repo_file("package.json")
    if path is None:
        pytest.skip(
            "package.json not found in any candidate location "
            "(set UNIFIDECK_REPO_ROOT to point at the checkout root)")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("remote_binary") or []
    return {e["name"]: e for e in entries if "name" in e}


@pytest.fixture(scope="module")
def shell_urls() -> dict[str, str]:
    """``<TOOL>_URL="..."`` assignments from build-plugin.sh."""
    path = _repo_file("build-plugin.sh")
    if path is None:
        pytest.skip(
            "build-plugin.sh not found in any candidate location "
            "(set UNIFIDECK_REPO_ROOT to point at the checkout root)")
    text = path.read_text(encoding="utf-8")
    found = re.findall(
        r'^([A-Z0-9_]+)_URL="([^"]+)"', text, flags=re.MULTILINE,
    )
    return {name.lower(): url for name, url in found}


def test_every_manifest_entry_has_a_shell_url(
    manifest: dict[str, dict[str, str]], shell_urls: dict[str, str],
) -> None:
    """Every remote_binary tool is also downloadable by build-plugin.sh."""
    missing = sorted(set(manifest) - set(shell_urls))
    assert not missing, (
        f"package.json pins {missing} but build-plugin.sh has no "
        f"<TOOL>_URL for them — prebuild_binaries() will not fetch them"
    )


def test_manifest_and_shell_urls_match(
    manifest: dict[str, dict[str, str]], shell_urls: dict[str, str],
) -> None:
    """The URL in package.json is byte-identical to build-plugin.sh's."""
    drifted = {
        name: (entry["url"], shell_urls[name])
        for name, entry in sorted(manifest.items())
        if name in shell_urls and entry["url"] != shell_urls[name]
    }
    assert not drifted, (
        "package.json and build-plugin.sh disagree on a binary URL — the "
        "build would download a different version than the manifest "
        "advertises, and Decky's install-time hash check would reject it:\n"
        + "\n".join(
            f"  {n}:\n    package.json:     {a}\n    build-plugin.sh:  {b}"
            for n, (a, b) in drifted.items()
        )
    )


def test_known_hashes_match_the_manifest(
    manifest: dict[str, dict[str, str]],
) -> None:
    """``_KNOWN_HASHES`` agrees with package.json for every pinned tool.

    Both describe the same artifact: Decky verifies the download against
    the manifest, and ``verify_bundled_binary`` verifies the file on disk
    against ``_KNOWN_HASHES``. If they disagree, one of them is wrong and
    a correct binary gets reported as tampered.
    """
    from unifideck.core.binaries.binary_signatures import _KNOWN_HASHES

    mismatched = {
        name: (_KNOWN_HASHES[name], manifest[name]["sha256hash"])
        for name in _TRIPLE_PINNED
        if name in manifest
        and _KNOWN_HASHES.get(name)  # "" means intentionally undeclared
        and _KNOWN_HASHES[name] != manifest[name]["sha256hash"]
    }
    assert not mismatched, (
        "binary_signatures._KNOWN_HASHES disagrees with package.json "
        "remote_binary — bump both in the same commit:\n"
        + "\n".join(
            f"  {n}:\n    _KNOWN_HASHES: {a}\n    package.json:  {b}"
            for n, (a, b) in mismatched.items()
        )
    )


def test_archive_binary_butler_is_fully_pinned(
    manifest: dict[str, dict[str, str]], shell_urls: dict[str, str],
) -> None:
    """butler ships as an archive, so it is pinned in a different three places.

    It must NOT be a ``remote_binary`` entry: Decky writes a remote_binary
    download to ``bin/<name>`` byte for byte, which would replace the
    unpacked ``bin/butler/`` with the raw zip. Instead build-plugin.sh pins
    the zip (``BUTLER_URL`` naming a version + ``BUTLER_SHA256``) and
    ``_KNOWN_HASHES`` pins the executable that comes out of it. A bump that
    moves the URL without both hashes fails the build or the runtime check.
    """
    from unifideck.core.binaries.binary_signatures import _KNOWN_HASHES

    assert "butler" not in manifest, (
        "butler must not be a package.json remote_binary entry: Decky "
        "would overwrite bin/butler/ with the unextracted zip"
    )
    url = shell_urls.get("butler", "")
    assert re.search(r"/linux-amd64/\d+\.\d+\.\d+/archive/default$", url), (
        f"BUTLER_URL must name an exact version, never LATEST: {url!r}"
    )
    path = _repo_file("build-plugin.sh")
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert re.search(r'^BUTLER_SHA256="[0-9a-f]{64}"$', text, flags=re.MULTILINE), (
        "build-plugin.sh must pin the butler zip with BUTLER_SHA256"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", _KNOWN_HASHES.get("butler", "")), (
        "binary_signatures._KNOWN_HASHES must pin the extracted butler"
    )


def test_bundled_umu_version_is_pinned() -> None:
    """``bin/umu/VERSION`` exists and names a plausible umu version.

    umu is the one bundled tool with no manifest entry, so this file is
    the only machine-readable record of which umu shipped — the support
    bundle reports it, and it is the first question on any launch failure
    (umu <=1.4.1 fetches the runtime from a URL that is now permanently
    403, so the version alone explains a whole class of reports).
    """
    path = _repo_file("bin/umu/VERSION")
    if path is None:
        pytest.skip(
            "bin/umu/VERSION not found in any candidate location "
            "(set UNIFIDECK_REPO_ROOT to point at the checkout root)")
    version = path.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
        f"bin/umu/VERSION should hold a bare x.y.z version, got {version!r}"
    )
