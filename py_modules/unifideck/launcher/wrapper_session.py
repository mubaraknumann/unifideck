"""Propagating a wrapper store's signed-in session between Wine prefixes.

py_modules/unifideck/launcher/wrapper_session.py

A *wrapper store* runs a vendor client inside the prefix
(``launcher/wrapper_stores.py``), and that client keeps its session **inside
the prefix it ran in**. Unifideck gives every game its own prefix, so a
session earned once has to be moved: pushed *out* to a prefix before its
client starts, and pulled *back* after that client exits, because the vendor
rotates the token on use and the copy every other prefix holds goes
server-stale the moment it does.

Ubisoft learned this the hard way and solved it privately under
``stores/ubisoft/session/``. Battle.net then copied Ubisoft's three prefix
tiers but none of the lifecycle, and shipped the same bug: measured on this
Deck on 2026-08-11, ``.bnet-auth`` and ``.template`` were byte-identical and
frozen at 08:57, while the game prefix's client had rewritten every session
file at 21:15 — twelve hours of rotation that never came back. The user saw
``BLZBNTBGS80000023`` ("Your login session has expired") on every install and
launch.

So this is shared rather than copied, for the reason ``prefix_placement``
states: the same question asked separately in two places is how these stores
break. Adding a store is a :class:`SessionSpec` row.

This module is the **behaviour** half. What a session consists of, per store,
lives next door in ``wrapper_session_specs`` — including the measured
Battle.net layout and, most importantly, the fact that its login token is a
**Wine registry key** rather than a file. Getting that wrong is why a first
attempt shipped a session the server answered with
``ERROR_TOKEN_NOT_FOUND (49)``: the files arrived and the token did not.
``launcher/wine_registry`` moves the keys.

Stdlib-only, and deliberately **outside** the ``proton`` package for the same
reason ``wrapper_stores`` is: it is imported both from
``launcher/proton/handlers/`` and from ``stores/``, and reaching it through
``proton`` pulls in ``proton/__init__`` -> handlers -> ``types/context``, a
cycle. Runs under the SYSTEM python (3.10-3.14), not Decky's bundled 3.11.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from unifideck.launcher import wine_registry, wrapper_locale, wrapper_prefs
from unifideck.launcher.proton.infrastructure.prefix_layout import (
    resolve_drive_c,
)
from unifideck.launcher.wrapper_session_specs import (
    GAMES_DIR_NAME,
    SPECS,
    PrefsSpec,
    SessionSpec,
    read_gaclientid,
    spec_for,
)

logger = logging.getLogger(__name__)

# Re-exported so callers keep one import for the whole facility; the split
# is about where the declarations are maintained, not about the API.
__all__ = [
    "GAMES_DIR_NAME",
    "SPECS",
    "PrefsSpec",
    "SessionSpec",
    "auth_prefix",
    "capture",
    "fingerprint",
    "has_session",
    "inject",
    "prefix_index_path",
    "purge",
    "read_gaclientid",
    "resolve_drive_c",
    "spec_for",
    "template_prefix",
    "write_prefix_index",
]

def prefix_index_path() -> Path:
    """Where the auth/template prefix index lives.

    Written by each wrapper store's backend on init and read here. The
    launcher cannot reach the backend's config manager, and ``prefixes_dir``
    is user-configurable, so a path we are never told is a path we can never
    use — the same reason Battle.net's family codes are written to its id map.

    Resolved on every call rather than once at import. A module-level constant
    is captured before pytest's autouse fixture redirects ``HOME``, and the
    first run of the suite duly wrote pytest temp paths into the real user's
    data directory — the same leak ``tests/conftest.py`` was written to stop
    for ``frontend_bridge.EVENTS_FILE``. Reading the environment at call time
    is both correct and cheap.
    """
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "unifideck" / "wrapper_prefixes.json"


def write_prefix_index(store: str, *, auth: Path, template: Path) -> None:
    """Record where ``store``'s auth and template prefixes live.

    Merges rather than replaces: one file serves every wrapper store, and
    each store writes only its own row on init.

    Paths only. This used to carry the plugin's UI locale as well, so the
    launcher could seed the vendor client's language from it, which
    duplicated a question ``utils.locale.get_unifideck_locale`` already
    answers correctly inside the launcher process (see PR #422). The copy
    could go stale between backend starts, and on 2026-08-22 the file was
    found absent on a working install, which silently reverted every
    locale-dependent behaviour to English. ``wrapper_locale.plugin_locale``
    asks the resolver instead.
    """
    path = prefix_index_path()
    index: dict[str, dict[str, str]] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            index = {k: v for k, v in raw.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        index = {}
    index[store] = {"auth": str(auth), "template": str(template)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("[wrapper_session] cannot write the prefix index: %s", exc)


def _read_prefix_index(store: str) -> dict[str, str]:
    try:
        raw = json.loads(prefix_index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    row = raw.get(store) if isinstance(raw, dict) else None
    return {k: str(v) for k, v in row.items()} if isinstance(row, dict) else {}


def auth_prefix(store: str) -> Path | None:
    """``store``'s auth prefix, as recorded by its backend."""
    recorded = _read_prefix_index(store).get("auth")
    return Path(recorded) if recorded else None


def template_prefix(store: str) -> Path | None:
    """``store``'s template prefix, as recorded by its backend."""
    recorded = _read_prefix_index(store).get("template")
    return Path(recorded) if recorded else None


# ── reading a prefix's session ─────────────────────────────────────────────


def _walk(path: Path) -> list[Path]:
    """Every file under ``path``, skipping the games directory."""
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    found: list[Path] = []
    for entry in path.rglob("*"):
        if GAMES_DIR_NAME in entry.parts:
            continue
        if entry.is_file():
            found.append(entry)
    return found


def _members(spec: SessionSpec, prefix: Path) -> list[Path]:
    """Every session file present in ``prefix``."""
    drive_c = resolve_drive_c(prefix)
    if drive_c is None:
        return []
    found: list[Path] = []
    for target in spec.expand(drive_c, spec.files + spec.trees):
        found.extend(_walk(target))
    return found


def has_session(spec: SessionSpec, prefix: Path) -> bool:
    """True when ``prefix`` holds evidence of a signed-in session.

    Keyed on ``spec.evidence`` rather than on any session file being present,
    because material that merely travels with a session can outlive it: the
    Battle.net licence ledger and its cached battle tag both survive a
    sign-out, which is why ``battlenet/store.py`` needs a marker file to tell
    "signed in" from "signed out but remembered".

    When a store keeps its token in the registry, that key is required too —
    the files alone are not a session, which is the mistake that produced
    ``ERROR_TOKEN_NOT_FOUND (49)``.
    """
    if spec.registry_keys and not wine_registry.has_sections(
        prefix, spec.registry_keys,
    ):
        return False
    drive_c = resolve_drive_c(prefix)
    if drive_c is None:
        return False
    targets = spec.expand(drive_c, spec.evidence or spec.files)
    return any(t.is_file() and t.stat().st_size > 0 for t in targets)


def _evidence_members(spec: SessionSpec, prefix: Path) -> list[Path]:
    """The files that *are* the session, as opposed to travelling with it."""
    drive_c = resolve_drive_c(prefix)
    if drive_c is None:
        return []
    found: list[Path] = []
    for target in spec.expand(drive_c, spec.evidence or spec.files):
        found.extend(_walk(target))
    return found


def fingerprint(spec: SessionSpec, prefix: Path) -> tuple[float, int]:
    """``(newest mtime, total size)`` of ``prefix``'s **credential** material.

    The ordering key for every copy in this module: "which of these two
    prefixes holds the newer session".

    Deliberately narrower than what gets copied. Material that merely travels
    with a session is excluded, because its mtime answers a different
    question: Battle.net's ``CachedData.db`` is licence and telemetry state
    that the client rewrites on its own schedule, and including it made an
    auth prefix whose ledger had just been written look *newer* than a game
    prefix holding a freshly rotated token — so the capture was skipped and
    the token was lost, which is the whole bug this module exists to fix.
    """
    newest = 0.0
    total = 0
    for member in _evidence_members(spec, prefix):
        try:
            stat = member.stat()
        except OSError:
            continue
        newest = max(newest, stat.st_mtime)
        total += stat.st_size
    if spec.registry_keys:
        # Wine's own last-write time for the token key, in seconds. Preferred
        # over the registry file's mtime, which moves for unrelated keys.
        newest = max(newest, float(
            wine_registry.newest_stamp(prefix, spec.registry_keys),
        ))
    return (newest, total)


def _identities_agree(spec: SessionSpec, src: Path, dst: Path) -> bool:
    """False only when both identities are readable and differ.

    An unreadable identity is not treated as a mismatch: a prefix mid-clone
    has no config yet, and refusing every copy because we could not look
    would strand the session we are trying to deliver.
    """
    if spec.identity is None:
        return True
    src_id = spec.identity(src)
    dst_id = spec.identity(dst)
    if src_id is None or dst_id is None:
        return True
    if src_id == dst_id:
        return True
    logger.warning(
        "[wrapper_session] %s: identity mismatch %s -> %s, refusing to copy "
        "(the session is bound to the client instance that minted it)",
        spec.store, src.name, dst.name,
    )
    return False


# ── moving it ──────────────────────────────────────────────────────────────


def _copy_one(src: Path, dst: Path) -> bool:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    except OSError as exc:
        logger.warning("[wrapper_session] copy failed %s: %s", src.name, exc)
        return False
    return True


def _copy_member(src_root: Path, dst_root: Path, member: Path) -> bool:
    """Copy one member, refusing to replace real content with nothing.

    A client killed mid-write leaves a truncated file behind, and the material
    that travels with a session includes state the store reads for other
    purposes — Battle.net's ``CachedData.db`` is its licence ledger, and
    ``is_available()`` keys on it. Letting a zero-length copy land there would
    report the whole store as signed out. Cheap, and it costs nothing in the
    normal case: a genuinely empty source is never worth propagating.
    """
    destination = dst_root / member.relative_to(src_root)
    try:
        if member.stat().st_size == 0 and destination.is_file() and (
            destination.stat().st_size > 0
        ):
            logger.info(
                "[wrapper_session] skipping empty %s — keeping the existing copy",
                member.name,
            )
            return False
    except OSError:
        return False
    return _copy_one(member, destination)


def _copy_session(
    spec: SessionSpec, src: Path, dst: Path, *, dst_busy: bool,
) -> int:
    """Copy a session from ``src`` into ``dst``. Returns the member count.

    Additive: nothing in the destination is deleted. The game lives inside
    these prefixes, and a stale cookie the vendor client will overwrite
    itself is a far better outcome than a delete pass that reaches too far.

    The registry sections go last and are all-or-nothing. If they cannot be
    written — a live wineserver owns the registry and would discard the write
    — the whole copy reports failure rather than leaving the destination with
    the session's *files* and someone else's token, which is precisely the
    inconsistent state that produced ``ERROR_TOKEN_NOT_FOUND (49)``.
    """
    src_c = resolve_drive_c(src)
    dst_c = resolve_drive_c(dst)
    if src_c is None or dst_c is None:
        return 0
    if spec.registry_keys:
        sections = wine_registry.read_sections(src, spec.registry_keys)
        if not sections:
            logger.warning(
                "[wrapper_session] %s: no token in %s's registry — not copying",
                spec.store, Path(src).name,
            )
            return 0
        if not wine_registry.registry_is_writable(dst, int(dst_busy)):
            return 0
        if not wine_registry.merge_sections(dst, sections):
            return 0
    return sum(
        1
        for member in _members(spec, src)
        if _copy_member(src_c, dst_c, member)
    )


def inject(
    spec: SessionSpec, source: Path, target: Path, *, target_busy: bool = False,
) -> bool:
    """Push ``source``'s session into ``target`` before its client starts.

    This is what makes every client start from the live session instead of
    from whatever snapshot its prefix was cloned with — the reason a prefix
    that has sat idle for a month still opens signed in.

    Refused when the source has no session (there is nothing to deliver, and
    overwriting a target that *does* have one would sign the user out), and
    when the source is not newer than the target (a client that just rotated
    its own token must not be reset to an older copy).

    The return value is about the *session*. The launcher settings ride along
    on the way past, under their own guards in ``wrapper_prefs``, and a store
    can perfectly well have settings to carry and no session to inject.
    """
    if Path(source).resolve() == Path(target).resolve():
        return False
    if not _identities_agree(spec, Path(source), Path(target)):
        return False
    # Seed the launcher's language from the plugin locale whenever that locale
    # has changed, so the client follows the language selector rather than
    # staying at whatever was resolving on the very first launch. Runs before
    # the prefs merge, which is what carries the seed on to the target.
    wrapper_locale.ensure_locale_seeded(spec, source)
    wrapper_prefs.merge(spec, source, target, target_busy=target_busy)
    if not has_session(spec, source):
        logger.info(
            "[wrapper_session] %s: no session in %s — nothing to inject",
            spec.store, Path(source).name,
        )
        return False
    if has_session(spec, target) and fingerprint(spec, source) <= fingerprint(
        spec, target,
    ):
        logger.info(
            "[wrapper_session] %s: %s already holds a session at least as new "
            "— leaving it", spec.store, Path(target).name,
        )
        return False
    copied = _copy_session(
        spec, Path(source), Path(target), dst_busy=target_busy,
    )
    if not copied:
        logger.warning(
            "[wrapper_session] %s: could not inject a session into %s",
            spec.store, Path(target).name,
        )
        return False
    logger.info(
        "[wrapper_session] %s: injected %d session file(s) into %s",
        spec.store, copied, Path(target).name,
    )
    return True


def capture(
    spec: SessionSpec, source: Path, auth: Path, *, auth_busy: bool = False,
) -> bool:
    """Pull the session ``source``'s client rotated back to the auth prefix.

    The auth prefix is the single source of truth, so this is the only write
    direction that matters after a play or install session. The template is
    **not** a target: it is a golden image that changes only on an explicit
    sign-in or sign-out, and every game prefix re-injects from auth anyway,
    so auth alone is a sufficient fresh source. Same invariant Ubisoft's
    session facade states.

    Must be called only after the client has exited. A vendor client flushes
    its rotated token on shutdown — which is why Battle.net's teardown
    SIGTERMs before it SIGKILLs — so capturing while it is still running can
    read a torn vault.

    As in :func:`inject`, the launcher settings ride along under their own
    guards, and this is the leg that makes a setting changed inside one game's
    client reach the others: it lands in auth here, and each launch injects.
    """
    if Path(source).resolve() == Path(auth).resolve():
        return False
    if not _identities_agree(spec, Path(source), Path(auth)):
        return False
    wrapper_prefs.merge(spec, source, auth, target_busy=auth_busy)
    if not has_session(spec, source):
        logger.info(
            "[wrapper_session] %s: %s holds no session — not capturing "
            "(a signed-out prefix must never overwrite auth)",
            spec.store, Path(source).name,
        )
        return False
    source_fp = fingerprint(spec, source)
    auth_fp = fingerprint(spec, auth)
    if source_fp <= auth_fp:
        # Logged, not silent. This is the branch a lost rotation lands in — the
        # client was killed before it could flush its token, so the source has
        # nothing newer to give — and it used to return here without a word.
        # The symptom (auth frozen on an old token, the client asking for a
        # password days later) was then invisible in the logs; only comparing
        # on-disk stamps across prefixes revealed it.
        logger.info(
            "[wrapper_session] %s: %s holds nothing newer than auth "
            "(source=%.0f auth=%.0f) — not capturing",
            spec.store, Path(source).name, source_fp[0], auth_fp[0],
        )
        return False
    copied = _copy_session(spec, Path(source), Path(auth), dst_busy=auth_busy)
    if not copied:
        logger.warning(
            "[wrapper_session] %s: could not capture the session from %s",
            spec.store, Path(source).name,
        )
        return False
    logger.info(
        "[wrapper_session] %s: captured %d rotated session file(s) from %s "
        "→ auth refreshed", spec.store, copied, Path(source).name,
    )
    return True


def purge(spec: SessionSpec, prefix: Path) -> int:
    """Delete ``prefix``'s session material. Returns the count removed.

    For sign-out. Without it, signing out of the store leaves every game
    prefix holding a working session, so the next launch silently signs the
    user back in. Only session material is touched — never the game.

    The registry token goes too. Removing the files but leaving the key is not
    a sign-out at all: the token *is* the registry key, so the client would
    log straight back in.
    """
    removed = 0
    if spec.registry_keys:
        removed += wine_registry.purge_sections(prefix, spec.registry_keys)
    for member in _members(spec, Path(prefix)):
        try:
            member.unlink()
            removed += 1
        except OSError as exc:
            logger.warning(
                "[wrapper_session] could not remove %s: %s", member.name, exc,
            )
    if removed:
        logger.info(
            "[wrapper_session] %s: purged %d session file(s) from %s",
            spec.store, removed, Path(prefix).name,
        )
    return removed
