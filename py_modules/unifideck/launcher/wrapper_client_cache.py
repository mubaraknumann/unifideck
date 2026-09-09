"""Carry a vendor client's expensive-to-rebuild cache back to the template.

py_modules/unifideck/launcher/wrapper_client_cache.py

Sibling of :mod:`wrapper_session`, and the same shape of problem one step
along: that module moves what proves *who* the user is, this one moves what a
client would otherwise have to re-download to be *usable*. Both exist because
a per-game prefix is an rsync clone of ``.template`` and everything the clone
learns dies with it.

The measured case. Battle.net's Agent runs one exclusive operation at a time,
and on a fresh prefix it makes its own self-update that operation, so the
game's download waits behind it. That costs two seconds when the local content
store already holds the build's tagged content, and 45 minutes when it does
not. The user sees a bar reading "Queued" and cancels; cancelling deletes the
prefix; the next attempt re-downloads the same bytes from zero. Three attempts,
three restarts, no progress ever made.

Nothing here prevents that first download. What it prevents is paying for it
more than once.

**Replace, never merge, and the size is not the signal.** The obvious design
was "copy when the source holds more", and measuring it killed that outright:
after Battle.net's Agent finished updating, its store was 5.4 MB against the
template's 6.9 MB. The Agent *compacts* on completion (``[casc] Starting
Compaction``), discarding content the new tag set does not need. So the smaller
store is the correct one, and the bigger one is stale. Size cannot tell them
apart, and an additive merge would be actively dangerous: these stores are an
``indices/`` directory describing archives under ``data/``, so blending two
generations leaves an index pointing at archives that are not there. The whole
tree is swapped, or nothing is.

**The generation string is what decides.** The caller passes an opaque token
identifying what the source store satisfies, for Battle.net the Agent's TACT
tag query, e.g. ``Volatile Windows KR? acct-IND? geoip-IN?``, and it is
recorded in the marker. A capture is skipped when the template already carries
that generation. Opaque on purpose: this module has no business parsing a
vendor's tags, only comparing them.

**Content stores only, never installed programs.** ``client_cache`` must never
name an extracted binary tree: a half-applied client copied into ``.template``
would be inherited by every prefix created afterwards, turning a slow install
into a permanently broken store. Battle.net's entry is ``Agent/data`` and
deliberately not the ``Agent/<build>/`` beside it.

**Quiescence is the caller's call, not this module's.** Copying a content store
while its writer is mid-flight can capture a torn index. Only the store knows
when its client is done. For Battle.net that is
``agent_status.self_update_finished``, so the decision stays there. Same
reasoning that keeps ``holds_game`` out of ``stores/shared/prefix_placement``.

Stdlib-only; runs under the SYSTEM python (3.10-3.14) like everything in
``launcher/``. Best-effort throughout: this runs beside a cleanup the user is
waiting on and must never be the reason it does not happen.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from unifideck.launcher.proton.infrastructure.prefix_layout import (
    resolve_drive_c,
)

from .wrapper_session_specs import SessionSpec

logger = logging.getLogger(__name__)

#: Stamped on the template after a capture, holding the generation string the
#: captured caches satisfy. Versioned so a capture that turns out to be a bad
#: idea can be invalidated by bumping the constant rather than by asking users
#: to rebuild a prefix: the ``.v2`` self-heal idiom
#: ``stores/battlenet/prefix/tweaks`` and the GOG registry fix both use.
CACHE_MARKER = ".unifideck_client_cache.v1"

#: Prefix on a marker whose caches are a resumable part-download rather than a
#: finished one. **Partial captures are the point, not a concession.** The
#: cancel path is where this pays: a user cancels precisely because the wait
#: looked broken, and if only completed downloads were ever kept then the one
#: case that actually loops (cancel, retry, cancel) would keep nothing and
#: restart from zero every time. A content store interrupted by a dead client
#: is the ordinary resumable state its own downloader is built to continue
#: from; what it must never be is *mistaken for a finished one*, hence the
#: tag, which keeps a later complete capture from being skipped as duplicate.
_PARTIAL = "partial:"

#: Suffixes for the two-rename swap. Beside the destination, so both renames
#: are same-filesystem even when the game prefix lives on removable storage.
_STAGING = ".unifideck-new"
_RETIRED = ".unifideck-old"


def read_generation(template_prefix: Path) -> str | None:
    """The generation this template's caches were last captured for."""
    try:
        return (Path(template_prefix) / CACHE_MARKER).read_text(
            encoding="utf-8",
        ).strip() or None
    except OSError:
        return None


def _swap_in(src: Path, dst: Path) -> bool:
    """Replace ``dst`` with a copy of ``src``. True when ``dst`` now is it.

    Copy to a sibling, then two renames, then delete the old, so a failure
    at any point leaves either the previous tree or the new one in place, and
    never a half-written store the next prefix would clone.
    """
    staging = dst.with_name(dst.name + _STAGING)
    retired = dst.with_name(dst.name + _RETIRED)
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(retired, ignore_errors=True)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, staging, symlinks=True)
        if dst.exists():
            dst.rename(retired)
        staging.rename(dst)
    except (OSError, shutil.Error) as exc:
        logger.debug("[client_cache] swap %s -> %s failed: %s", src, dst, exc)
        # Put back whatever we moved aside before giving up.
        if retired.is_dir() and not dst.exists():
            retired.rename(dst)
        shutil.rmtree(staging, ignore_errors=True)
        return False
    shutil.rmtree(retired, ignore_errors=True)
    return True


def _worth_capturing(template: Path, generation: str, complete: bool) -> bool:
    """Whether this capture improves on what the template already holds.

    Only a *complete* capture of the same generation makes another one
    pointless. Anything else is an advance: a complete store supersedes a
    partial one, and any store for the current generation supersedes one built
    for a generation the account no longer uses, however finished that was.
    """
    held = read_generation(template)
    if held is None:
        return True
    if held == generation:
        return False
    if held == _PARTIAL + generation:
        # A partial of this generation is only worth replacing with the
        # finished article; another partial would copy the same bytes back.
        return complete
    return True


def capture_client_cache(
    spec: SessionSpec,
    source_prefix: Path,
    template_prefix: Path,
    generation: str,
    *,
    complete: bool,
) -> int:
    """Replace ``spec``'s client caches in the template with the source's.

    ``generation`` identifies what the source's caches satisfy and ``complete``
    whether they finished downloading it. Returns how many trees were swapped:
    zero when the store declares none, the template already holds this
    generation finished, or either prefix is unreadable. Never raises: every
    caller runs on a path where the real work is something else.

    The caller is responsible for only calling this once the cache's writer has
    stopped; see the module docstring.
    """
    if not spec.client_cache or not generation:
        return 0
    if not _worth_capturing(Path(template_prefix), generation, complete):
        return 0
    try:
        return _capture_all(
            spec, Path(source_prefix), Path(template_prefix), generation, complete,
        )
    except Exception:
        logger.debug(
            "[client_cache] capture from %s failed", source_prefix, exc_info=True,
        )
        return 0


def _capture_all(
    spec: SessionSpec,
    source: Path,
    template: Path,
    generation: str,
    complete: bool,
) -> int:
    """The body of :func:`capture_client_cache`, minus the safety net."""
    src_root = resolve_drive_c(source)
    dst_root = resolve_drive_c(template)
    if src_root is None or dst_root is None:
        return 0
    captured = 0
    for relative in spec.client_cache:
        src = src_root / relative
        if src.is_dir() and _swap_in(src, dst_root / relative):
            captured += 1
    if captured:
        _mark(template, generation, complete)
        logger.info(
            "[client_cache] template %s now carries %d %s cache(s) for %r",
            template.name,
            captured,
            "complete" if complete else "resumable",
            generation,
        )
    return captured


def _mark(template: Path, generation: str, complete: bool) -> None:
    """Record which generation the template's caches now satisfy."""
    value = generation if complete else _PARTIAL + generation
    try:
        (Path(template) / CACHE_MARKER).write_text(value, encoding="utf-8")
    except OSError as exc:
        logger.debug("[client_cache] cannot mark %s: %s", template, exc)
