#!/usr/bin/env python3
"""Validate the Unifideck architecture invariants that keep drifting.

Several silent drifts have recurred through the 0.7.x series and are worth
machine-enforcing rather than re-discovering by hand every release:

1. The RPC mixin set was documented inconsistently: at the 2026-08 audit
   ``main.py``'s docstring said "eleven", ``rpc/mixins/__init__.py``
   re-exported 13, and the docs said 18, against a class that composed 20.
   The one invariant that matters is that ``main.py``'s composed mixins and
   ``__init__.py``'s ``__all__`` agree; that is check 1, and it is the only
   place the set is stated. Check 5 keeps it that way (see below).

2. The store list drifts (docs said "five stores" long after Battle.net
   became the sixth). ``bootstrap/cache_registry._STORE_CACHES`` is the
   single code source of truth; it must match the store subdirectories
   on disk.

3. A store's ``StoreInfo.name`` must match its directory, since the registry
   auto-discovers by directory and every other check keys on that name.

   This check used to have a second arm, comparing each store's
   ``StoreInfo(uses_wine=...)`` against ``WRAPPER_STORES``. Audit §3.1 asked
   for that link; re-deriving it found ``uses_wine`` had no reader anywhere,
   so the gate was enforcing agreement on a value that could not change
   behaviour. The field is gone and ``get_store_infos`` derives
   ``client_runs_in_prefix`` from ``WRAPPER_STORES``, which makes the arm
   unfailable — a re-added literal now raises ``TypeError`` at construction.
   Check 9 replaces it with the wrapper-store link that was actually
   unguarded.

4. RPC methods accumulate with no frontend caller. The 2026-08 audit found
   29 of 102 — 28% of the surface — including a whole "DiagnosticsPanel"
   that was never built. This check *used* to be report-only and asked only
   "does the snake_case name appear anywhere in ``src/`` text", which missed
   14 of the 29: a method declared in ``rpcRoutes`` whose constant nothing
   references passed. It now asks both questions and is a hard gate.

   Opt out per method with an inline ``# no-frontend-caller: <reason>``
   comment on the ``async def`` line or the line above it. The reason lives
   next to the code rather than in an allowlist file, and the exemption
   count is printed on every run so growth is visible rather than silent.

5. A mixin count written into prose goes stale on the next mixin churn, and
   check 1 cannot see it. This happened three times in 0.7.x: audit §2.1
   found four disagreeing figures, the remediation hand-corrected them all
   to 20, and the §1.2 dead-RPC pass then deleted three empty mixins in the
   same release, making every corrected site wrong again. Correcting the
   number is the approach that failed; not writing it down is check 5.

   The set is enumerated in exactly two places, both machine-checked by
   check 1: ``main.py``'s ``class Plugin(...)`` and ``__init__.py``'s
   ``__all__``. Everywhere else must name the source rather than the figure.

   Opt out with an inline ``mixin-count-ok: <reason>`` marker on the line or
   the line above it, for a deliberate historical citation. Same convention
   as ``# no-frontend-caller:`` above and ``# unwired:`` in
   ``validate_event_wiring.py``: the reason lives next to the text.

6. A layer count written into prose has the same failure mode, and audit §2.2
   found four mutually contradictory ones, two of them saying "five" while
   enumerating six. The diagram in ``docs/architecture.md`` is the single
   enumeration. Banned like the mixin count; opt out with
   ``layer-count-ok: <reason>``.

7. A store count in prose is *verified* rather than banned, which is the one
   place these checks differ. Many live sites state a count correctly while
   explaining something, so the figure is compared against the store
   directories on disk and only a wrong one fails. Only a total claim is
   examined ("all N stores", "N store connectors"), because a count below the
   total is nearly always naming a subset. Opt out with
   ``store-count-ok: <reason>``.

8. A subpackage missing from the layer map reads as nonexistent to whoever
   plans the next change. Audit §2.5 found the tables listing 6 of 20
   ``core/`` modules and 10 of 15 ``services/`` packages, with
   ``compatibility`` (the ProtonDB path) and ``support_bundle`` (Capture
   Logs) both invisible. Check 8 asserts membership rather than trusting a
   hand-maintained table, since those drift exactly the way a count does.

9. Adding a wrapper store means adding a row in several hand-written
   dispatch maps, and a missing row fails silently rather than loudly. The
   Python-side maps are pinned by tests (``wrapper_prefix_probe._SPECS``,
   ``tests/unit/test_wrapper_store_dispatch_coverage.py``); the frontend's
   ``CLIENT_STOREFRONTS`` in ``services/store/StorefrontLauncher.ts`` is not
   reachable from pytest, so it is checked here. A wrapper store missing
   from it makes the cart button do nothing at all — no error, no toast.

10. A store can declare where its vendor client writes logs and then never
    salvage them. Audit §3.3 filed this as redundancy ("only Battle.net
    consumes ``prefix_forensics``"); re-deriving it found a complete, measured
    Ubisoft row in ``VENDOR_LOG_GLOBS`` that nothing called, so every failed
    Ubisoft install deleted UPC's own logs with the prefix — for a wrapper
    store the prefix *is* the install. This is the audit's most repeated
    defect class: the material shipped, the delivery channel was never
    built. Check 10 asks whether the store's **own package** calls
    ``preserve_vendor_logs``, so one store cannot vouch for another. Opt out
    with ``# no-vendor-salvage: <reason>``.

11. A helper promoted into ``stores/shared/`` gets copied back. Audit §3.4
    found five helpers pasted across stores; three had diverged, and one
    divergence was live — Epic's and Amazon's ``merge_install_status``
    guarded their disk-existence check on the path being truthy, so a
    record with a blank path skipped it and marked a deleted game
    installed. Promotion alone does not prevent the next copy, and a grep
    for the name finds the shared one and reads as covered. Check 11 pins
    each promoted helper to its owning module. Opt out with
    ``# intentional-divergence: <reason>``.

Checks 5 to 7 share one scanner, ``scan_prose``. Their regexes are narrow on
purpose and each carries the false positive that shaped it: a gate that fires
on correct, untouched code gets switched off rather than fixed. See the
comment above each pattern, and the parametrised false-positive tests in
``tests/unit/test_validate_architecture.py``.

Stdlib-only on purpose: the script runs in CI before dependencies are
installed and must not import the plugin (which would execute store
constructors and touch the network).

Usage::

    python3 scripts/validate_architecture.py

Exit 0 when clean, 1 on a hard mismatch.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PY = REPO_ROOT / "py_modules" / "unifideck"
SRC = REPO_ROOT / "src"

# Directories under stores/ that are not stores.
_NON_STORE_DIRS = {"shared", "__pycache__"}

# --- Checks 5-7: architecture facts written into prose ----------------------

# Everything an agent or a contributor reads to learn the architecture.
# ``main.py`` and the mixin package are included deliberately: their
# docstrings are where the original defect lived.
_PROSE_GLOBS = (
    "CLAUDE.md",
    "docs/**/*.md",
    ".claude/skills/**/*.md",
    "main.py",
    "py_modules/unifideck/**/*.py",
    "scripts/*.py",
    ".github/workflows/*.yml",
)

# ``docs/archive/`` is superseded by definition (see CLAUDE.md) and
# ``architecture-audit.md`` is the register whose job is recording the
# historical figures. Both would be pure noise.
_PROSE_EXCLUDE = ("docs/archive/", "docs/architecture-audit.md")

# Kept under the old names: the guard test addresses check 5 by these.
_MIXIN_COUNT_GLOBS = _PROSE_GLOBS
_MIXIN_COUNT_EXCLUDE = _PROSE_EXCLUDE

_MIXIN_COUNT_OK = "mixin-count-ok:"
_LAYER_COUNT_OK = "layer-count-ok:"
_STORE_COUNT_OK = "store-count-ok:"

_CARDINAL = (
    r"\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty"
)

# Matches a count written next to the word. Every historical instance took
# one of these forms (mixin-count-ok: spec, not a claim about the tree):
# "eleven mixins" / "20 RPC mixins" / "the 20 mixin surfaces"
#
# The lookbehind is load-bearing: without it "Layer-6 RPC mixins" in
# services/__init__.py reads as a count of six, because \b sits happily
# between the hyphen and the digit. It also stops "someone mixins".
#
# A figure separated from the word by other prose is NOT caught. Widening
# the window costs more in false positives than it buys, and the opt-out
# marker covers the deliberate historical citation that would need it.
_MIXIN_COUNT_RE = re.compile(
    rf"(?<![-\w])({_CARDINAL})\s+(?:rpc\s+)?mixin", re.IGNORECASE,
)

# Check 6 — the layer count. Banned outright, like the mixin count: the
# diagram in docs/architecture.md is the single enumeration, and audit §2.2
# found four mutually contradictory prose counts feeding off each other.
#
# The trailing noun is load-bearing, and is the same lesson as the mixin
# lookbehind. ``config/`` legitimately describes a "3-layer merge" (defaults,
# user, code) in seven places, and ``config_manager.py`` a "3-layer
# configuration manager" -- all true, none of them about the architecture
# stack. Requiring an architecture noun after the word separates the two
# without needing seven opt-out markers on correct code. Every historical
# violation named one of those nouns, the last of them being
# layer-count-ok: the spec of this check, quoting the shape it catches
# "the plan's five-layer model" in event_bus/__init__.py.
_LAYER_COUNT_RE = re.compile(
    rf"(?<![-\w.])({_CARDINAL})[- ]layer(?:ed)?\s+"
    r"(?:backend|architecture|stack|model|design)",
    re.IGNORECASE,
)

# Check 7 — the store count. NOT banned, verified: unlike the mixin and layer
# counts, live sites state it as part of explaining something and are
# correct, so the figure is compared against the store directories on disk
# (the same source check 2 uses) and only a WRONG one fails. That catches the
# audit §2.4 defect -- every doc said "five" for a release after Battle.net
# landed -- and keeps catching it from the other side when a seventh arrives.
#
# Only a *total* claim is checked, in the forms below, which are how every
# historical violation was written.
# store-count-ok: the spec of this check, quoting the shapes it catches
#   ("The five store connectors" / "a five-store system")
#
# The narrowing matters more than it looks: a first version matched any
# cardinal before "store" and produced 23 false positives in one run, every
# one of them a correct subset statement -- "Amazon is the one store whose
# sign-in leaves the shared Edge profile", "four stores report credential
# permissions through one channel", "Two stores need this path". A count
# below the total is nearly always naming a subset, so requiring "all" or a
# collection noun is what separates the two. With this form the tree needs no
# opt-out anywhere else, including the drift-guard skill lines that quote
# "five stores" verbatim.
_STORE_COUNT_RE = re.compile(
    rf"\ball\s+({_CARDINAL})\s+stores?\b"
    rf"|(?<![-\w.])({_CARDINAL})[- ]store\s+(?:connector|system)"
    rf"|(?<![-\w.])({_CARDINAL})-store\s+(?:setup|architecture)",
    re.IGNORECASE,
)

_CARDINAL_VALUES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20,
}


def _cardinal_to_int(text: str) -> int | None:
    """Return the integer a cardinal word or numeral denotes, else None."""
    stripped = text.strip().lower()
    if stripped.isdigit():
        return int(stripped)
    return _CARDINAL_VALUES.get(stripped)


def _fail(msg: str) -> None:
    print(f"::error::{msg}")


def parse_mixin_bases(main_path: Path) -> set[str]:
    """Return the mixin base names composed in ``class Plugin(...)``."""
    tree = ast.parse(main_path.read_text(), filename=str(main_path))
    bases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Plugin":
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id.endswith("Mixin"):
                    bases.add(base.id)
    return bases


def parse_all(mixins_init_path: Path) -> set[str]:
    """Return the names listed in ``rpc/mixins/__init__.py``'s ``__all__``."""
    tree = ast.parse(mixins_init_path.read_text(), filename=str(mixins_init_path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "__all__" in targets and isinstance(node.value, ast.List):
                return {
                    elt.value
                    for elt in node.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                }
    return set()


def parse_store_caches(cache_registry_path: Path) -> set[str]:
    """Return the store names from ``_STORE_CACHES`` in cache_registry.py."""
    text = cache_registry_path.read_text()
    match = re.search(
        r"_STORE_CACHES[^=]*=\s*\(([^)]*)\)", text, flags=re.DOTALL
    )
    if not match:
        raise SystemExit(
            f"{cache_registry_path}: could not locate _STORE_CACHES"
        )
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def discover_store_dirs(stores_path: Path) -> set[str]:
    """Return store subdirectories that contain a store module."""
    found: set[str] = set()
    for child in stores_path.iterdir():
        if not child.is_dir() or child.name in _NON_STORE_DIRS:
            continue
        has_store_module = (child / "store.py").exists() or (
            child / f"{child.name}_store.py"
        ).exists()
        if has_store_module:
            found.add(child.name)
    return found


def parse_wrapper_stores(wrapper_stores_path: Path) -> set[str]:
    """Return the contents of ``WRAPPER_STORES`` in launcher/wrapper_stores.py."""
    text = wrapper_stores_path.read_text()
    match = re.search(r"WRAPPER_STORES[^=]*=\s*frozenset\((\{[^}]*\})\)", text)
    if not match:
        raise SystemExit(
            f"{wrapper_stores_path}: could not locate WRAPPER_STORES"
        )
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def find_store_file(stores_path: Path, name: str) -> Path | None:
    """Locate the store module for a store directory name."""
    for candidate in (
        stores_path / name / f"{name}_store.py",
        stores_path / name / "store.py",
    ):
        if candidate.exists():
            return candidate
    return None


def parse_store_info(store_file: Path) -> str | None:
    """Return the ``name=`` declared in a store's ``StoreInfo(...)`` block."""
    text = store_file.read_text()
    match = re.search(r"store_info\s*=\s*StoreInfo\((.*?)\n\s*\)", text, flags=re.DOTALL)
    if not match:
        return None
    name_match = re.search(r'name\s*=\s*"([^"]+)"', match.group(1))
    return name_match.group(1) if name_match else None


def parse_client_storefronts(storefront_launcher_path: Path) -> set[str]:
    """Return the store ids keyed in ``CLIENT_STOREFRONTS``.

    The frontend's map of "stores whose shop is a tab inside their own
    Windows client" — i.e. the wrapper stores, restated in TypeScript where
    no pytest can reach it.
    """
    text = storefront_launcher_path.read_text()
    # ``.*?=\s*\{`` rather than ``[^=]*=``: the declaration's type annotation
    # is ``Partial<Record<StoreId, () => Promise<...>>>``, so a no-equals scan
    # stops inside the arrow. ``=\s*\{`` cannot match ``=>`` (no ``{`` after
    # it), so the first hit is the real assignment.
    match = re.search(
        r"const CLIENT_STOREFRONTS\b.*?=\s*\{(.*?)\n\};", text, flags=re.DOTALL
    )
    if not match:
        raise SystemExit(
            f"{storefront_launcher_path}: could not locate CLIENT_STOREFRONTS"
        )
    # Keys sit at exactly two spaces of indent; the arrow bodies below them are
    # indented four, so this cannot pick up a call argument by mistake.
    return set(re.findall(r"^ {2}(\w+):", match.group(1), flags=re.M))


NO_CALLER_RE = re.compile(r"#\s*no-frontend-caller:\s*\S")


def _has_no_caller_marker(lines: list[str], def_lineno: int) -> bool:
    """Is this ``async def`` exempted by a ``# no-frontend-caller:`` marker?

    Checks the ``def`` line itself, then walks upward through the contiguous
    run of comment lines directly above it. Walking the whole block (rather
    than a fixed one-line window) is what lets a real explanation span
    several lines — and these exemptions need explaining, so a one-liner
    limit would just push the reason somewhere it can rot.

    ``def_lineno`` is ast's 1-based line number for the ``async def``.
    """
    idx = def_lineno - 1
    if idx < 0 or idx >= len(lines):
        return False
    if NO_CALLER_RE.search(lines[idx]):
        return True
    for i in range(idx - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped.startswith("#"):
            break
        if NO_CALLER_RE.search(lines[i]):
            return True
    return False


NO_SALVAGE_RE = re.compile(r"#\s*no-vendor-salvage:\s*\S")


def parse_vendor_log_stores() -> set[str]:
    """Store keys declared in ``prefix_forensics.VENDOR_LOG_GLOBS``."""
    source = (
        PY / "stores" / "shared" / "prefix_forensics.py"
    ).read_text()
    match = re.search(
        r"VENDOR_LOG_GLOBS[^=]*=\s*\{(.*?)\n\}", source, flags=re.S,
    )
    if match is None:
        _fail("prefix_forensics.py: could not locate VENDOR_LOG_GLOBS")
        raise SystemExit(1)
    # Store keys sit at exactly four spaces of indent; the glob strings
    # inside each tuple are indented eight, so this cannot mistake a glob
    # for a key.
    return set(re.findall(r'^ {4}"(\w+)":', match.group(1), flags=re.M))


def find_unsalvaged_vendor_logs() -> set[str]:
    """Stores that declare vendor log globs but never salvage them.

    The defect class this closes is the most repeated one in the 2026-08
    audit: material written, shipped and documented, with the call site
    never built. ``VENDOR_LOG_GLOBS`` carried a full Ubisoft row — measured
    log paths, ready to use — while nothing in ``stores/ubisoft/`` called
    ``preserve_vendor_logs``, so every failed Ubisoft install deleted UPC's
    own logs along with the prefix. A grep for the globs found them and read
    as covered, which is exactly how it survived a release.

    Deliberately asks whether the *store's own package* calls it, not
    whether the symbol appears anywhere: Battle.net vouching for Ubisoft is
    the failure this is written to prevent.

    Opt out with an inline ``# no-vendor-salvage: <reason>`` marker anywhere
    in the store package, in the house style of ``# no-frontend-caller:``.
    """
    unsalvaged: set[str] = set()
    for store in parse_vendor_log_stores():
        package = PY / "stores" / store
        if not package.is_dir():
            continue
        salvages = False
        exempt = False
        for file in package.rglob("*.py"):
            text = file.read_text()
            if "preserve_vendor_logs(" in text:
                salvages = True
            if NO_SALVAGE_RE.search(text):
                exempt = True
        if not salvages and not exempt:
            unsalvaged.add(store)
    return unsalvaged


def count_exempt_vendor_salvage() -> int:
    """Count ``# no-vendor-salvage:`` markers, printed on every clean run."""
    total = 0
    for store in parse_vendor_log_stores():
        package = PY / "stores" / store
        if not package.is_dir():
            continue
        for file in package.rglob("*.py"):
            total += sum(
                1
                for line in file.read_text().splitlines()
                if NO_SALVAGE_RE.search(line)
            )
    return total


# --- Check 11: a promoted shared helper is defined exactly once ------------

#: Helpers that have been consolidated into one owning module, mapped to the
#: path (relative to ``py_modules/unifideck/``) that owns them.
#:
#: This is a hand-written list, which is normally the thing this script
#: exists to stamp out — but here the list *is* the deliverable. Audit §3.4
#: found five helpers copied across stores; three of them had silently
#: diverged, and one divergence (an install path that skipped its own
#: disk check) was a live defect. Promoting a helper into ``shared/`` does
#: nothing to stop the next store pasting its own copy back, and a grep for
#: the name finds the shared one and reads as covered. So: when a helper is
#: promoted, it gets a row here.
#:
#: A second definition is a hard failure unless its ``def`` line carries
#: ``# intentional-divergence: <reason>``. That is not a rubber stamp; it is
#: for the case where two same-named functions look like duplicates and are
#: not. There is exactly one today: Ubisoft implements the
#: ``store_injector`` hook ``_rebuild_auth_after_injection``, which the four
#: browser-auth stores get from the shared mixin, with a genuinely different
#: body — it has no browser monitor and wires a shortcut service instead.
#: §3.4 counted it as a fifth copy of the mixin's body; it never was one.
#: The same shape as §3.3's ``fix_pfx_symlink`` versus
#: ``ensure_pfx_symlink``, whose guards are inverses — that pair escapes
#: this check only because the two were given different names.
SHARED_HELPERS: dict[str, str] = {
    "merge_install_status": "stores/shared/install_status.py",
    "dir_size_bytes": "stores/shared/installed_size.py",
    # Its sparse-aware twin. Deliberately a second function rather than a flag
    # on the first: the two answer different questions and the wrong one is a
    # live defect in each direction. Apparent size sizes a *finished* install
    # (and survives btrfs compression); allocated size measures one *in
    # flight*, where a vendor client's pre-allocation makes apparent size
    # constant from the first minute.
    "dir_allocated_bytes": "stores/shared/installed_size.py",
    "_rebuild_auth_after_injection": "stores/shared/browser_auth_rebuild.py",
    "rsync_clone": "stores/shared/prefix_clone.py",
    "write_marker": "stores/shared/prefix_clone.py",
    "read_cli_user_json": "stores/shared/cli_credentials.py",
    # GOG's and Ubisoft's ``get_installed_path`` bodies were byte-identical;
    # Amazon's was the same shape on a different key. Audit register item 48.
    "install_path_from_record": "stores/shared/installed_path.py",
    # Prefix-layout primitives. Eight copies of these two existed *beside*
    # the module that already owned them — six of ``normalize_prefix_root``
    # (three renamed ``_prefix_root``, three under the canonical name in
    # ``proton/fixes/``) and two of ``resolve_drive_c``. Check 11 saw none of
    # them, because it matches by name; check 13 found them by body shape.
    # Audit register items 20 and 47.
    "normalize_prefix_root": "launcher/proton/infrastructure/prefix_layout.py",
    "resolve_drive_c": "launcher/proton/infrastructure/prefix_layout.py",
    "resolve_registry_prefix": "launcher/proton/infrastructure/prefix_layout.py",
    # ``kill_wineserver`` was tracked here too, as the consolidation of two
    # byte-identical copies in ``epic_prefix_fix`` and ``epic_registry``. Both
    # callers are gone: they invoked a Proton's ``wine`` directly and then
    # killed the wineserver out from under it, which is what corrupted the
    # shared Proton install. They now write the registry through umu and reap
    # nothing, so the helper had no callers left and was deleted rather than
    # kept as a tempting one. See ``infrastructure/setup_run.py``.
    # GOG and Microsoft each defined the same three config coercions as
    # nested closures. Check 13 caught only ``_list``; ``_s`` and ``_i`` sat
    # under its body-size floor, which is the floor's honest cost.
    "text_list": "stores/shared/config_reader.py",
    # Not a store helper, but the same drift class and the same remedy: this
    # arithmetic existed three times under three different names — here, as
    # ``compatibility/library._appid_key_candidates``, and inlined in
    # ``core/sync_queries_mixin.get_game_info``. Audit register item 20.
    "appid_candidates": "core/compat_bridge.py",
    # The generator's sibling. Twelve conversion sites were folded onto this
    # in the §1.4 pass; pinning it stops a thirteenth being written inline.
    "to_unsigned": "core/compat_bridge.py",
    # ``appid_candidates``' main consumer. Five readers of the
    # ``steam_real_appid`` namespace exist; only the two backfill services
    # shared a return contract ("a positive AppID or 0"), and those are what
    # this owns. The other three preserve the ``-1`` sentinel and are marked
    # divergent on purpose — see the module docstring. Register item 47.
    "read_positive_steam_appid": "core/steam_appid_map.py",
    # The title-match save-dir heuristic, twice. The GOG copy guarded the
    # *raw* title where the other guarded the sanitised one, so a title with
    # no ASCII alphanumerics matched the first directory it found and cloud
    # sync would have carried an unrelated folder. Register item 47.
    "find_save_dir_by_title": "services/cloud_save/path_resolver.py",
    # The two probe services each held a byte-identical copy of this, and
    # one cited the other in a comment instead of sharing it — the clearest
    # statement in the tree that a known duplicate still needs a gate to
    # become a shared module. Register item 47.
    "merge_str_list_mapping": "utils/config_helpers.py",
    # Terminal-failure toasts from the launcher subprocess. Both former
    # copies re-explained, at length, why they must ride LAUNCHER_STAGE —
    # which is the fact a third copy would be most likely to get wrong,
    # since getting it wrong is silent. Register item 47.
    "emit_launch_error_toast": "services/launcher/error_toasts.py",
    # Store timestamps of unknown flavour. Three copies — both Epic ones and
    # GOG's, which escaped check 13 by omitting a single ``.replace("Z", …)``.
    "parse_timestamp": "stores/shared/timestamps.py",
    # legendary's launcher OAuth token. Epic's achievements and sessions each
    # had their own; the two ``_refresh_token`` copies had diverged, and each
    # carried a defect the other had fixed. Register item 47.
    "_refresh_token": "stores/epic/launcher_auth.py",
}

DIVERGENCE_RE = re.compile(r"#\s*intentional-divergence:\s*\S")


def _is_marked_divergent(lines: list[str], def_index: int) -> bool:
    """Is the ``def`` at *def_index* opted out of check 11?

    The marker may sit on the ``def`` line itself or anywhere in the
    contiguous run of comment lines directly above it. Matching only one
    line up (the house style of ``# no-frontend-caller:``) was too narrow
    the moment the reason needed two lines to state — which the one real
    divergence in the tree does.
    """
    if DIVERGENCE_RE.search(lines[def_index]):
        return True
    index = def_index - 1
    while index >= 0 and lines[index].lstrip().startswith("#"):
        if DIVERGENCE_RE.search(lines[index]):
            return True
        index -= 1
    return False


#: Grandfathered body-shape duplicate groups (check 13). Shrink-only, in the
#: same spirit as the volumetry allowlists and the i18n baseline.
SHAPE_BASELINE = Path(__file__).resolve().parent / "duplicate_bodies_baseline.json"

#: Minimum non-docstring statements before a body is compared. Below this,
#: bodies collide for uninteresting reasons (a two-line getter, a guard plus
#: a return) and the check becomes noise — the failure mode that gets a gate
#: switched off rather than fixed.
_MIN_BODY_STATEMENTS = 3


class _Anonymise(ast.NodeTransformer):
    """Erase identifiers and literals, keep structure and attribute names.

    Two functions match when they *do the same thing to the same attributes*
    in the same order, whatever their parameters are called. That is the
    property check 11 could not see: it matched on the helper's **name**, so
    a copy that was merely renamed walked past it — ``appid_candidates``
    existed as ``_appid_key_candidates`` and inlined a third time
    (register item 20).

    Attribute names survive normalisation on purpose. Erasing them too would
    make every ``try: x.a() except OSError: log()`` identical, which is a
    false-positive generator; keeping them means the match says "calls
    ``mkdir`` then ``write_text`` then logs", which is a real claim.
    """

    def visit_Name(self, node: ast.Name) -> ast.Name:
        return ast.copy_location(ast.Name(id="_", ctx=node.ctx), node)

    def visit_arg(self, node: ast.arg) -> ast.arg:
        return ast.copy_location(ast.arg(arg="_", annotation=None), node)

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        return ast.copy_location(ast.Constant(value="_"), node)


def _body_signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """Normalised shape of *fn*'s body, or ``None`` if too small to compare."""
    body = [
        stmt for stmt in fn.body
        if not (
            isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
        )
    ]
    if len(body) < _MIN_BODY_STATEMENTS:
        return None
    try:
        clone = ast.parse(ast.unparse(ast.Module(body=body, type_ignores=[])))
    except (SyntaxError, ValueError):
        return None
    return ast.dump(_Anonymise().visit(clone), annotate_fields=False)


def find_duplicate_bodies() -> list[tuple[str, list[str]]]:
    """Groups of identically-shaped function bodies in different modules.

    Same-file duplicates are ignored: an overload pair or two adjacent
    variants in one module is a local style choice, not the cross-module
    drift this exists to catch.
    """
    groups: dict[str, list[str]] = {}
    for file in sorted(PY.rglob("*.py")):
        if "__pycache__" in file.parts:
            continue
        try:
            tree = ast.parse(file.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, SyntaxError):
            continue
        rel = file.relative_to(PY).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            sig = _body_signature(node)
            if sig is None:
                continue
            groups.setdefault(sig, []).append(f"{rel}::{node.name}")
    return [
        (sig, sorted(members))
        for sig, members in groups.items()
        if len({m.split("::")[0] for m in members}) > 1
    ]


def _load_shape_baseline() -> list[frozenset[str]]:
    """Grandfathered duplicate groups, as member sets.

    Sets rather than joined strings so a **subset** counts as grandfathered.
    Removing one copy from a five-way group leaves a four-way group, which is
    progress — it must not read as a new violation, or partial consolidation
    reds the gate and the honest response becomes "put the copy back".
    Adding a member still fails, which is the direction that matters.
    """
    if not SHAPE_BASELINE.is_file():
        return []
    try:
        data = json.loads(SHAPE_BASELINE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [frozenset(g) for g in data.get("groups", [])]


def report_duplicate_bodies() -> int:
    """Print check 13's verdict; return the number of hard failures."""
    baseline = _load_shape_baseline()
    found = find_duplicate_bodies()
    new = [
        members for _sig, members in found
        if not any(frozenset(members) <= row for row in baseline)
    ]
    if not new:
        if baseline:
            print(
                f"OK: no new duplicated function bodies "
                f"({len(baseline)} group(s) grandfathered; this list may only "
                f"shrink)"
            )
        else:
            print("OK: no duplicated function bodies across modules")
        return 0
    for members in new:
        _fail(
            "identical function body in "
            + str(len({m.split("::")[0] for m in members}))
            + " modules: " + ", ".join(members)
        )
    print(
        "\n  Two functions doing the same thing to the same attributes in the\n"
        "  same order, in different modules. Check 11 cannot see this: it\n"
        "  matches a shared helper by NAME, so a copy that was renamed walks\n"
        "  past it — 'appid_candidates' lived as '_appid_key_candidates' and\n"
        "  inlined a third time (register item 20).\n"
        "  Promote it to a shared module and add a SHARED_HELPERS row, or — if\n"
        "  the two genuinely must differ — add the group to\n"
        f"  {SHAPE_BASELINE.name} with a reason."
    )
    return len(new)


def find_duplicate_shared_helpers() -> list[tuple[str, str, str]]:
    """``(helper, owning module, offending file)`` for every stray copy.

    Matches a ``def``/``async def`` at any indentation, so a method on a
    class counts — that is how four of the five §3.4 duplicates were
    written.
    """
    strays: list[tuple[str, str, str]] = []
    patterns = {
        name: re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(name)}\s*\(")
        for name in SHARED_HELPERS
    }
    for file in sorted(PY.rglob("*.py")):
        rel = file.relative_to(PY).as_posix()
        lines = file.read_text().splitlines()
        for name, owner in SHARED_HELPERS.items():
            if rel == owner:
                continue
            for index, line in enumerate(lines):
                if not patterns[name].match(line):
                    continue
                if _is_marked_divergent(lines, index):
                    continue
                strays.append((name, owner, f"{rel}:{index + 1}"))
    return strays


def count_intentional_divergences() -> int:
    """Count ``# intentional-divergence:`` markers, printed on a clean run."""
    return sum(
        1
        for file in PY.rglob("*.py")
        for line in file.read_text().splitlines()
        if DIVERGENCE_RE.search(line)
    )


def report_shared_helpers() -> int:
    """Print check 11's verdict; return the number of hard failures."""
    strays = find_duplicate_shared_helpers()
    if not strays:
        marked = count_intentional_divergences()
        suffix = f" ({marked} marked divergent)" if marked else ""
        print(
            f"OK: all {len(SHARED_HELPERS)} shared helpers defined once{suffix}"
        )
        return 0
    for name, owner, where in strays:
        _fail(f"'{name}' is defined at {where}; it belongs to {owner}")
    print(
        "\n  Audit §3.4: five helpers had been copied across stores and three\n"
        "  had quietly diverged — one copy skipped its own disk check and\n"
        "  marked a deleted game installed. Import the shared one, or, if the\n"
        "  two genuinely cannot share a body, mark the def with\n"
        "  '# intentional-divergence: <reason>'."
    )
    return len(strays)


#: Marker excusing a module from check 12. Use it for a genuine entry point
#: — something a process, a script or Decky itself imports by name rather
#: than another module importing it.
ENTRY_POINT_RE = re.compile(r"#\s*entry-point:\s*\S")

#: The other check-12 opt-out: the module IS dead and we know it, but the
#: deletion is owned by an open register item. Separate from
#: ``# entry-point:`` on purpose — conflating "reached by a process" with
#: "dead, tracked elsewhere" is how an allowlist stops meaning anything.
#: The count prints on every clean run so the set cannot grow quietly.
UNIMPORTED_RE = re.compile(r"#\s*unimported:\s*\S")

#: Roots scanned for importers, in addition to ``py_modules/unifideck``
#: itself. A module imported only by a test is still dead production code,
#: so ``tests/`` is deliberately NOT in this list.
_IMPORTER_ROOTS = ("main.py", "bin", "cli", "scripts", "vulture_whitelist.py")


def _module_name(path: Path) -> str:
    """Dotted first-party module name for a file under ``py_modules/``."""
    rel = path.relative_to(PY.parent).with_suffix("")
    return ".".join(rel.parts)


def _imported_names(path: Path) -> set[str]:
    """Every module name *path* imports, absolute and relative resolved.

    Relative imports are resolved against the importing module's package,
    which is the part a naive scan gets wrong: ``from ..cloud import x``
    inside ``launcher/proton/`` must resolve to
    ``unifideck.launcher.cloud.x``, not to ``cloud.x``. A first cut of this
    check that skipped that reported ``launcher/cloud/cloud_failure.py`` and
    a dozen others as orphans while some were genuinely imported.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    pkg_parts = _module_name(path).split(".")[:-1]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                target = [*base, node.module] if node.module else base
                mod = ".".join(target)
            else:
                mod = node.module or ""
            if not mod:
                continue
            names.add(mod)
            # ``from pkg import submodule`` imports a module, not a symbol.
            for alias in node.names:
                names.add(f"{mod}.{alias.name}")
    return names


def find_unimported_modules() -> list[tuple[str, Path]]:
    """First-party modules nothing imports and nothing runs.

    Vulture is a hard gate but runs at ``min_confidence = 80``, which
    reports unused imports and variables — **not** unused functions and not
    whole unimported modules. That blind spot let two shadow packages
    (``launcher/fixes/``, ``launcher/language_setup/``) sit next to the real
    ``launcher/proton/*`` with identical module names, every file an empty
    ``# TODO: implement`` stub, for the entire life of the project. An
    import of the stub resolved, did nothing, and raised nothing.

    Audit register item 24. Opt out with ``# entry-point: <reason>``.
    """
    modules: dict[str, Path] = {}
    for path in PY.rglob("*.py"):
        if "__pycache__" in path.parts or path.name == "__init__.py":
            continue
        modules[_module_name(path)] = path

    imported: set[str] = set()
    for path in PY.rglob("*.py"):
        if "__pycache__" not in path.parts:
            imported |= _imported_names(path)
    for root in _IMPORTER_ROOTS:
        target = REPO_ROOT / root
        if target.is_file():
            imported |= _imported_names_text(target.read_text(errors="ignore"))
        elif target.is_dir():
            for path in target.rglob("*"):
                if path.is_file() and path.suffix in (".py", ".sh", ".toml"):
                    imported |= _imported_names_text(
                        path.read_text(errors="ignore"),
                    )

    orphans: list[tuple[str, Path]] = []
    for name, path in sorted(modules.items()):
        if name in imported:
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        if (
            ENTRY_POINT_RE.search(source)
            or UNIMPORTED_RE.search(source)
            or '__name__ == "__main__"' in source
        ):
            continue
        orphans.append((name, path))
    return orphans


def _imported_names_text(text: str) -> set[str]:
    """Dotted ``unifideck.*`` names mentioned anywhere in *text*.

    Deliberately textual, not AST: an entry point can reach a module by
    ``python -m unifideck.x`` in a shell script or by an importlib string,
    and neither is an import statement.
    """
    return set(re.findall(r"\bunifideck(?:\.[A-Za-z_][A-Za-z0-9_]*)+", text))


def report_unimported_modules() -> int:
    """Print check 12's verdict; return the number of hard failures."""
    orphans = find_unimported_modules()
    if not orphans:
        entry, known_dead = 0, 0
        for p in PY.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            text = p.read_text(errors="ignore")
            entry += bool(ENTRY_POINT_RE.search(text))
            known_dead += bool(UNIMPORTED_RE.search(text))
        parts = []
        if entry:
            parts.append(f"{entry} entry point(s)")
        if known_dead:
            parts.append(f"{known_dead} known-dead, tracked")
        suffix = f" ({', '.join(parts)} exempt)" if parts else ""
        print(f"OK: every first-party module is imported{suffix}")
        return 0
    for name, path in orphans:
        _fail(
            f"'{name}' is imported by nothing "
            f"({path.relative_to(REPO_ROOT)}, "
            f"{len(path.read_text(errors='ignore').splitlines())} lines)"
        )
    print(
        "\n  Vulture cannot see this class: at min_confidence 80 it reports\n"
        "  neither unused functions nor unimported modules, which is how two\n"
        "  shadow packages of empty stubs survived beside the real ones.\n"
        "  Delete the module, wire it up, or — if a process or script reaches\n"
        "  it by name — mark it '# entry-point: <reason>'."
    )
    return len(orphans)


def collect_rpc_methods(mixins_path: Path) -> set[str]:
    """Return public ``async def`` method names on ``*Mixin`` classes.

    ``@auto_wrap_rpc_methods`` wraps every public coroutine on a mixin, so
    a public ``async def`` on a ``*Mixin`` class is the RPC surface. Module
    level helpers and sync methods are not. The class filter is what does
    that work, so a helper module landing in this directory is skipped
    without an allowlist; ``cleanup_sweeps.py`` was the one such module and
    has since moved to ``core/``, where it belonged.

    Methods carrying an inline ``# no-frontend-caller: <reason>`` marker —
    on the ``async def`` line or the line directly above it — are excluded
    from the dead-RPC check. The marker is named for exactly what the check
    tests, so it stays honest whether the reason is "only the launcher
    subprocess calls this" or "dead, tracked by audit register 4a".
    """
    names: set[str] = set()
    for file in mixins_path.glob("*.py"):
        if file.name == "__init__.py":
            continue
        source = file.read_text()
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not node.name.endswith("Mixin"):
                continue
            for item in node.body:
                if (
                    not isinstance(item, ast.AsyncFunctionDef)
                    or item.name.startswith("_")
                ):
                    continue
                if _has_no_caller_marker(lines, item.lineno):
                    continue
                names.add(item.name)
    return names


def count_exempt_rpc(mixins_path: Path) -> int:
    """Count methods carrying a ``# no-frontend-caller:`` marker.

    Printed on every clean run so the exemption set cannot grow quietly —
    the failure mode of any allowlist. A rising number here is the signal
    that the gate is being worked around rather than satisfied.
    """
    total = 0
    for file in mixins_path.glob("*.py"):
        if file.name == "__init__.py":
            continue
        total += sum(
            1 for line in file.read_text().splitlines() if NO_CALLER_RE.search(line)
        )
    return total


def _route_constants() -> dict[str, str]:
    """Map ``snake_case`` RPC name → its ``rpcRoutes`` camelCase key.

    Parsed from ``src/api/rpc-routes.ts``, the single source of truth for
    the route table. A method absent from this map has no declared route.
    """
    routes = SRC / "api" / "rpc-routes.ts"
    if not routes.is_file():
        return {}
    pairs = re.findall(r"(\w+):\s*\"([a-z0-9_]+)\"", routes.read_text())
    return {snake: camel for camel, snake in pairs}


def find_dead_rpc(methods: set[str]) -> list[str]:
    """Return RPC methods with no live frontend caller.

    Two independent ways a method can be dead, and the original version of
    this check only caught the first:

    1. **Undeclared** — the name appears nowhere in ``src/`` at all.
    2. **Declared but unreferenced** — it has an ``rpcRoutes`` entry, but no
       component mentions that constant. The route table alone keeps the
       name "present" in ``src/`` text, which is exactly how 14 dead methods
       hid from the pre-2026-08 version of this check.

    ``rpc-routes.ts`` is excluded from the haystack for both questions, so a
    row in the table can never vouch for itself.
    """
    haystack = ""
    for file in sorted(SRC.rglob("*")):
        if not file.is_file() or file.suffix not in (".ts", ".tsx"):
            continue
        if file.name == "rpc-routes.ts":
            continue
        try:
            haystack += file.read_text() + "\n"
        except UnicodeDecodeError:
            continue

    routes = _route_constants()
    dead: list[str] = []
    for name in methods:
        camel = routes.get(name)
        if camel is not None:
            # Declared: the route constant must be referenced somewhere.
            if not re.search(rf"rpcRoutes\.{re.escape(camel)}\b", haystack):
                dead.append(name)
            continue
        # Undeclared: a raw quoted string is the only remaining way in.
        if not re.search(rf"[\"']{re.escape(name)}[\"']", haystack):
            dead.append(name)
    return sorted(dead)


def scan_prose(
    root: Path, pattern: re.Pattern[str], marker: str,
) -> list[tuple[str, int, re.Match[str]]]:
    """Return ``(relpath, lineno, match)`` for every hit of ``pattern``.

    Shared by checks 5, 6 and 7 so one scanner owns the file walk, the
    exclusions and the opt-out semantics. Lines carrying ``marker``, on the
    line itself or the line above, are exempt; the line-above form is what
    lets a marker sit in a comment over the line it excuses.
    """
    hits: list[tuple[str, int, re.Match[str]]] = []
    seen: set[Path] = set()
    for glob in _PROSE_GLOBS:
        for path in sorted(root.glob(glob)):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            rel = path.relative_to(root).as_posix()
            if rel.startswith(_PROSE_EXCLUDE):
                continue
            lines = path.read_text(
                encoding="utf-8", errors="replace",
            ).splitlines()
            for lineno, line in enumerate(lines, start=1):
                if marker in line:
                    continue
                if lineno >= 2 and marker in lines[lineno - 2]:
                    continue
                # finditer, not search: a line carrying two counts should
                # report both, or fixing one just re-reds the gate.
                for match in pattern.finditer(line):
                    hits.append((rel, lineno, match))
    return hits


def find_prose_mixin_counts(root: Path) -> list[tuple[str, int, str]]:
    """Return ``(relpath, lineno, matched_text)`` per prose mixin count.

    The mixin set is enumerated in ``main.py``'s ``class Plugin(...)`` and
    ``__init__.py``'s ``__all__``, which check 1 keeps in agreement. Any
    third statement of the figure is unowned by that check and goes stale on
    the next mixin churn -- see the module docstring for the three times it
    did. Lines carrying a ``mixin-count-ok:`` marker, on the line itself or
    the line above, are exempt.
    """
    return [
        (rel, lineno, match.group(0))
        for rel, lineno, match in scan_prose(
            root, _MIXIN_COUNT_RE, _MIXIN_COUNT_OK,
        )
    ]


def find_prose_layer_counts(root: Path) -> list[tuple[str, int, str]]:
    """Return ``(relpath, lineno, matched_text)`` per prose layer count.

    The layer model is drawn once, in ``docs/architecture.md``. Audit §2.2
    found four prose counts contradicting each other and the diagram, two of
    them saying "five" while enumerating six. Opt-out:
    ``layer-count-ok: <reason>``.
    """
    return [
        (rel, lineno, match.group(0))
        for rel, lineno, match in scan_prose(
            root, _LAYER_COUNT_RE, _LAYER_COUNT_OK,
        )
    ]


def find_wrong_store_counts(
    root: Path, actual: int,
) -> list[tuple[str, int, str, int]]:
    """Return ``(relpath, lineno, text, stated)`` per WRONG prose store count.

    A correct figure passes: see ``_STORE_COUNT_RE`` for why this check
    verifies rather than bans. ``actual`` comes from the store directories on
    disk. Opt-out: ``store-count-ok: <reason>``.
    """
    wrong: list[tuple[str, int, str, int]] = []
    for rel, lineno, match in scan_prose(
        root, _STORE_COUNT_RE, _STORE_COUNT_OK,
    ):
        # One group per alternative in _STORE_COUNT_RE; exactly one is set.
        captured = next((g for g in match.groups() if g), None)
        if captured is None:
            continue
        stated = _cardinal_to_int(captured)
        if stated is not None and stated != actual:
            wrong.append((rel, lineno, match.group(0), stated))
    return wrong


def find_undocumented_subpackages(
    root: Path, doc: Path,
) -> list[str]:
    """Return subpackage/module names absent from the architecture doc.

    Audit §2.5: the layer tables listed 6 of 20 ``core/`` modules and 10 of
    15 ``services/`` packages, so whole subsystems (``compatibility``, the
    ProtonDB path; ``support_bundle``, the Capture Logs path) read as
    nonexistent to anyone planning a change. Hand-maintained tables drift the
    same way a hand-maintained count does, so the membership is checked
    rather than trusted.
    """
    if not doc.is_file():
        return []
    text = doc.read_text(encoding="utf-8", errors="replace")
    expected: list[str] = []
    services = root / "py_modules" / "unifideck" / "services"
    if services.is_dir():
        expected += [
            f"services/{p.name}"
            for p in sorted(services.iterdir())
            if p.is_dir() and p.name != "__pycache__"
        ]
    for package in ("core", "event_bus"):
        pkg_dir = root / "py_modules" / "unifideck" / package
        if not pkg_dir.is_dir():
            continue
        expected += [
            f"{package}/{p.name}"
            for p in sorted(pkg_dir.glob("*.py"))
            if p.name != "__init__.py"
        ]
    # Match on the bare name: the doc's tables key on ``artwork/`` or
    # ``cache_manager.py``, not on the full path from the package root.
    return [name for name in expected if name.split("/")[-1] not in text]


# ── check 14: every skill file carries a freshness stamp ──────────────
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
STAMP_RE = re.compile(r"Last verified:\s*\d{4}-\d{2}-\d{2}")


def find_unstamped_skills() -> list[str]:
    """Skill files with no ``Last verified: YYYY-MM-DD`` line.

    A skill is what the next reader trusts instead of reading the tree, so
    one that does not say when it was last checked cannot be judged at all.
    Two files had no stamp when this was written and the register's own
    roadmap claimed stamps existed on "all skills" (audit register item 22).

    This checks *existence*, not accuracy — a machine cannot tell whether
    prose still describes the code. Existence is the half that can be
    enforced, and it is the half that was missing.
    """
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(
        str(f.relative_to(SKILLS_DIR))
        for f in SKILLS_DIR.rglob("*.md")
        if not STAMP_RE.search(f.read_text(encoding="utf-8", errors="replace"))
    )


def report_unstamped_skills() -> int:
    """Print check 14; return 1 on failure.

    Skipped silently when ``.claude/skills/`` is absent — it is gitignored,
    so CI never sees it. This check is therefore a **local** gate: it earns
    its place by running in the same command a developer already runs, not
    by blocking a pipeline that cannot observe the files.
    """
    if not SKILLS_DIR.is_dir():
        print("SKIP: .claude/skills/ not present (gitignored — CI cannot see it)")
        return 0
    unstamped = find_unstamped_skills()
    total = sum(1 for _ in SKILLS_DIR.rglob("*.md"))
    if not unstamped:
        print(f"OK: all {total} skill file(s) carry a freshness stamp")
        return 0
    _fail(f"{len(unstamped)} skill file(s) carry no 'Last verified:' stamp")
    for name in unstamped:
        print(f"    · {name}")
    print(
        "\n  A skill is read instead of the tree. Without a stamp there is no\n"
        "  way to judge whether it still describes the code. Add a line:\n"
        "  'Last verified: YYYY-MM-DD against vX.Y.Z'."
    )
    return 1


def main() -> int:
    main_path = REPO_ROOT / "main.py"
    mixins_init = PY / "rpc" / "mixins" / "__init__.py"
    mixins_dir = PY / "rpc" / "mixins"
    cache_registry = PY / "bootstrap" / "cache_registry.py"
    stores_path = PY / "stores"
    wrapper_stores = PY / "launcher" / "wrapper_stores.py"

    hard_failures = 0

    # Check 1: main.py composed mixins == __all__.
    composed = parse_mixin_bases(main_path)
    exported = parse_all(mixins_init)
    missing = composed - exported
    extra = exported - composed
    if missing or extra:
        hard_failures += 1
        _fail(
            "mixin set drift: "
            f"main.py composes {len(composed)} mixins but "
            f"rpc/mixins/__init__.py __all__ re-exports {len(exported)}"
        )
        if missing:
            _fail(f"  missing from __all__: {sorted(missing)}")
        if extra:
            _fail(f"  in __all__ but not composed: {sorted(extra)}")
    else:
        print(f"OK: {len(composed)} mixins composed == __all__")

    # Check 2: _STORE_CACHES == store directories on disk.
    canonical_stores = parse_store_caches(cache_registry)
    discovered = discover_store_dirs(stores_path)
    if canonical_stores != discovered:
        hard_failures += 1
        _fail(
            "store list drift: "
            f"_STORE_CACHES = {sorted(canonical_stores)} but disk has "
            f"{sorted(discovered)}"
        )
    else:
        print(f"OK: {len(canonical_stores)} stores agree (cache registry == disk)")

    # Check 3: StoreInfo.name == its directory name, per store.
    name_failures = 0
    for name in sorted(discovered):
        store_file = find_store_file(stores_path, name)
        if store_file is None:
            name_failures += 1
            _fail(f"store '{name}': no store module found")
            continue
        declared_name = parse_store_info(store_file)
        if declared_name is not None and declared_name != name:
            name_failures += 1
            _fail(
                f"store '{name}': StoreInfo.name = '{declared_name}' "
                f"(should match directory)"
            )
    hard_failures += name_failures
    if name_failures == 0:
        # Counted separately rather than off the running total: gating this
        # line on ``hard_failures == 0`` meant a check-1 or check-2 failure
        # silently suppressed check 3's own result.
        print(f"OK: StoreInfo.name matches its directory for all {len(discovered)} stores")

    # Check 4 (hard): dead RPC.
    methods = collect_rpc_methods(mixins_dir)
    dead = find_dead_rpc(methods)
    if dead:
        hard_failures += len(dead)
        for name in dead:
            _fail(f"RPC '{name}' has no frontend caller")
        print(
            "\n  Delete the method and its rpcRoutes row, or mark it at the\n"
            "  definition with the reason nothing in src/ calls it:\n"
            "      # no-frontend-caller: <reason>\n"
            "      async def "
            + dead[0]
            + "(self, ...)"
        )
    else:
        exempt = count_exempt_rpc(mixins_dir)
        note = f" ({exempt} exempt)" if exempt else ""
        print(
            f"OK: all {len(methods)} checked RPC methods "
            f"have a frontend caller{note}"
        )

    # Check 5 (hard): the mixin count is not restated in prose.
    prose_counts = find_prose_mixin_counts(REPO_ROOT)
    if prose_counts:
        hard_failures += len(prose_counts)
        for rel, lineno, text in prose_counts:
            _fail(
                f"{rel}:{lineno}: mixin count written into prose "
                f"({text.strip()!r}; main.py composes {len(composed)})"
            )
        print(
            "\n  The mixin set belongs in main.py's class Plugin(...) and\n"
            "  rpc/mixins/__init__.py __all__, and nowhere else. Name that\n"
            "  source instead of the figure, or, for a deliberate historical\n"
            "  citation, mark the line or the line above it:\n"
            "      mixin-count-ok: <reason>"
        )
    else:
        print("OK: no mixin count restated in prose")

    # Check 6 (hard): the layer count is not restated in prose.
    layer_counts = find_prose_layer_counts(REPO_ROOT)
    if layer_counts:
        hard_failures += len(layer_counts)
        for rel, lineno, text in layer_counts:
            _fail(
                f"{rel}:{lineno}: layer count written into prose "
                f"({text.strip()!r})"
            )
        print(
            "\n  The layer model is drawn once, in docs/architecture.md.\n"
            "  Point at that diagram instead of restating a figure, or, for\n"
            "  a deliberate historical citation, mark the line or the line\n"
            "  above it:\n"
            "      layer-count-ok: <reason>"
        )
    else:
        print("OK: no layer count restated in prose")

    # Check 7 (hard): a prose store count agrees with the tree.
    wrong_stores = find_wrong_store_counts(REPO_ROOT, len(discovered))
    if wrong_stores:
        hard_failures += len(wrong_stores)
        for rel, lineno, text, stated in wrong_stores:
            _fail(
                f"{rel}:{lineno}: store count says {stated} "
                f"({text.strip()!r}) but the tree has {len(discovered)}"
            )
        print(
            "\n  Correct the figure, or, for a deliberate historical\n"
            "  citation, mark the line or the line above it:\n"
            "      store-count-ok: <reason>"
        )
    else:
        print(f"OK: every prose store count agrees ({len(discovered)})")

    # Check 8 (hard): every subpackage appears in the architecture doc.
    arch_doc = REPO_ROOT / "docs" / "architecture.md"
    undocumented = find_undocumented_subpackages(REPO_ROOT, arch_doc)
    if undocumented:
        hard_failures += len(undocumented)
        for name in undocumented:
            _fail(f"{name} is absent from docs/architecture.md")
        print(
            "\n  A subsystem missing from the layer map reads as nonexistent\n"
            "  to whoever plans the next change. Add a row for it."
        )
    else:
        print("OK: every services/, core/ and event_bus/ module is documented")

    # Check 9 (hard): the frontend's wrapper-store map covers WRAPPER_STORES.
    wrapper_set = parse_wrapper_stores(wrapper_stores)
    storefronts = parse_client_storefronts(
        SRC / "services" / "store" / "StorefrontLauncher.ts"
    )
    if storefronts != wrapper_set:
        hard_failures += 1
        _fail(
            "wrapper storefront drift: CLIENT_STOREFRONTS = "
            f"{sorted(storefronts)} but WRAPPER_STORES = {sorted(wrapper_set)}"
        )
        print(
            "\n  A wrapper store missing from CLIENT_STOREFRONTS makes its cart\n"
            "  button do nothing — hasStorefront() returns false and the press\n"
            "  is dropped with no error and no toast. A non-wrapper store\n"
            "  present there opens a Windows client that store does not have."
        )
    else:
        print(
            f"OK: CLIENT_STOREFRONTS covers all {len(wrapper_set)} wrapper stores"
        )

    # Check 10 (hard): a store with vendor-log globs actually salvages them.
    unsalvaged = find_unsalvaged_vendor_logs()
    if unsalvaged:
        hard_failures += len(unsalvaged)
        for store in sorted(unsalvaged):
            _fail(
                f"store '{store}' has VENDOR_LOG_GLOBS but never calls "
                "preserve_vendor_logs"
            )
        print(
            "\n  For a wrapper store the prefix IS the install, so a failed\n"
            "  install deletes the vendor client's own logs — the only\n"
            "  first-hand account of why it failed. Writing the globs without\n"
            "  the call reads as covered and collects nothing: Ubisoft's row\n"
            "  sat there unused for a release. Add the call at the site that\n"
            "  removes the prefix, or opt out with '# no-vendor-salvage:'."
        )
    else:
        exempt = count_exempt_vendor_salvage()
        suffix = f" ({exempt} exempt)" if exempt else ""
        print(
            "OK: every store with vendor log globs salvages them"
            f"{suffix}"
        )

    # Check 11 (hard): a promoted shared helper is defined exactly once.
    hard_failures += report_shared_helpers()

    # Check 13 (hard): no NEW function body duplicated across modules.
    # Catches the renamed copy check 11's name matching cannot see.
    hard_failures += report_duplicate_bodies()

    # Check 12 (hard): every first-party module is imported by something.
    # Closes the vulture blind spot (min_confidence 80 reports neither
    # unused functions nor unimported modules) that let two shadow packages
    # of empty stubs live beside the real ones. Audit register item 24.
    hard_failures += report_unimported_modules()

    # Check 14 (hard, local-only): every skill file carries a freshness
    # stamp. Register item 22 — the roadmap claimed all skills had one; two
    # did not, and nothing enforced it.
    hard_failures += report_unstamped_skills()

    if hard_failures:
        print(f"\n{hard_failures} architecture invariant(s) violated")
        return 1
    print("\narchitecture invariants OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
