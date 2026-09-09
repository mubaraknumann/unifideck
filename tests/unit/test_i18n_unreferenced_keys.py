"""Guard test — checks 3 and 4 of ``scripts/check_orphan_keys.py``.

The checker had two directions, both code → locale: "used in code but not
declared" and "declared in en-US but missing elsewhere". Nothing ran
locale → code, so a string that was written and translated into all 16
locales with no delivery path passed cleanly. That is the single most
repeated defect in the 2026-08 architecture audit:

* §1.1.2 — ``TOAST_NOTIFICATION``: "the i18n strings existed and were
  translated in all 16 locales the whole time; only the delivery channel
  was dead."
* §1.3 — the three ``SYNC_SKIPPED`` reasons that would have told a Game
  Pass user why their library vanished; translated, referenced by nothing.
* §3.2 — four ``errors.download.*`` codes, including a ``lockConflict``
  that exactly fit a known Epic failure.
* ``toasts.storeError`` and ``microsoft.subscriptionDetected``, both
  deleted with their events.

Every one would have been reported by this check on the day it was added.

What is pinned here:

1. the real repo passes, and the grandfathered count is printed so the
   baseline cannot grow quietly;
2. a newly-dead key fails — the case that matters, since the baseline
   makes the existing backlog non-blocking;
3. a key reached only from **Python** is not reported, because the backend
   sends ``i18n_key`` strings and a key can be live without appearing in
   any ``.tsx`` file. This is the false-positive class that would get the
   gate switched off;
4. runtime-composed keys (``t(`errors.download.${code}`)``) are not
   reported;
5. the baseline is shrink-only and self-cleaning;
6. **check 4**, the other missing direction: an ``i18n_key=`` the *backend*
   names with no string behind it. ``ExitCode.user_message_key`` mapped
   eight such keys — the inverse case, where the delivery existed and the
   strings never did.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def _script() -> Path | None:
    from tests.unit._repo_root import find_repo_file

    return find_repo_file("scripts/check_orphan_keys.py")


@pytest.fixture(scope="module")
def script_path() -> Path:
    p = _script()
    if p is None:
        pytest.skip("scripts/check_orphan_keys.py not found")
    return p


@pytest.fixture
def mod(script_path: Path):
    """Fresh instance so a monkeypatched root cannot leak between tests."""
    spec = importlib.util.spec_from_file_location("_cok_under_test", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── the real tree ────────────────────────────────────────────────
def test_the_repo_passes_and_prints_its_grandfathered_count(
    script_path: Path,
) -> None:
    out = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True, text=True, check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "grandfathered as unreferenced" in out.stdout, (
        "the baseline count must print on every clean run — an allowlist "
        "nobody sees is how it grows"
    )


def test_no_baseline_key_is_reachable_from_python(mod) -> None:
    """The false-positive class that would get this gate switched off.

    The backend emits ``i18n_key`` strings, so ``py_modules/`` is part of
    the haystack. If it were not, ~85 live keys would be reported as dead
    and the whole check would be dismissed as noise.
    """
    baseline = set(mod._load_unused_baseline())
    if not baseline:
        pytest.skip("no baseline in this checkout")
    root = mod.REPO_ROOT / "py_modules" / "unifideck"
    named_in_python = set()
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for key in baseline:
            if key in text:
                named_in_python.add(key)
    assert named_in_python == set(), (
        f"these keys ARE named in Python but are baselined as unreachable: "
        f"{sorted(named_in_python)}"
    )


def test_every_baseline_key_is_still_declared(mod) -> None:
    """A stale baseline row silently widens the gate."""
    baseline = set(mod._load_unused_baseline())
    if not baseline:
        pytest.skip("no baseline in this checkout")
    en = mod.LOCALES_DIR / "en-US.json"
    declared = set(
        mod.flatten_json(json.loads(en.read_text(encoding="utf-8"))),
    )
    stale = sorted(baseline - declared)
    assert stale == [], (
        f"baseline names keys that no longer exist: {stale} — the run prints "
        f"a cleanup reminder for these; drop them"
    )


# ── the detector itself ──────────────────────────────────────────
def _isolate(mod, tmp_path: Path, monkeypatch, files: dict[str, str]) -> None:
    """Point the module at a synthetic tree containing only *files*."""
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)


def test_a_key_nothing_references_is_reported(mod, tmp_path, monkeypatch) -> None:
    _isolate(mod, tmp_path, monkeypatch, {"src/App.tsx": 't("used.one")\n'})
    found = mod.find_unreferenced_keys({"used.one", "dead.two"})
    assert found == ["dead.two"]


def test_a_key_referenced_only_from_python_is_not_reported(
    mod, tmp_path, monkeypatch,
) -> None:
    """A live backend toast that no .tsx file mentions."""
    _isolate(
        mod, tmp_path, monkeypatch,
        {"py_modules/unifideck/s.py": 'emit(i18n_key="toasts.fromBackend")\n'},
    )
    assert mod.find_unreferenced_keys({"toasts.fromBackend"}) == []


def test_a_runtime_composed_key_is_not_reported(mod, tmp_path, monkeypatch) -> None:
    """``t(`errors.download.${code}`)`` reaches every child of the prefix."""
    _isolate(
        mod, tmp_path, monkeypatch,
        {"src/e.ts": "const k = `errors.download.${code}`;\n"
                     "const map = { diskSpace: 1 };\n"},
    )
    assert mod.find_unreferenced_keys({"errors.download.diskSpace"}) == []


def test_a_key_named_in_bin_is_not_reported(mod, tmp_path, monkeypatch) -> None:
    """``bin/`` holds the launcher, which also names keys."""
    _isolate(
        mod, tmp_path, monkeypatch,
        {"bin/unifideck-launcher": 'key = "launcher.exe"\n'},
    )
    assert mod.find_unreferenced_keys({"launcher.exe"}) == []


def test_locale_files_are_not_their_own_haystack(
    mod, tmp_path, monkeypatch,
) -> None:
    """The declaration must not count as a reference.

    Without this exclusion every key would vouch for itself and the check
    would report nothing — the same self-vouching failure that let 14 dead
    RPCs through ``find_dead_rpc`` until ``rpc-routes.ts`` was excluded
    from its own haystack (audit §1.2).
    """
    _isolate(
        mod, tmp_path, monkeypatch,
        {"src/i18n/locales/en-US.json": '{"dead": {"key": "text"}}\n'},
    )
    assert mod.find_unreferenced_keys({"dead.key"}) == ["dead.key"]


# ── check 4: backend-named keys must have a string ──────────────
def test_every_backend_named_key_has_a_string(mod) -> None:
    """The direction neither check 1 nor check 2 covered.

    Check 1 scans ``t("key")`` in ``src/``; check 2 compares locales against
    en-US. A key the **backend** names was checked from neither side, and
    there are 48 literal ``i18n_key=`` arguments in ``py_modules/``.

    ``ExitCode.user_message_key`` mapped nine exit codes to
    ``toasts.launcher.*`` keys of which **eight were never written into any
    locale** — the inverse of the audit's usual finding, where the strings
    existed and the delivery was dead. i18next prints a missing key
    verbatim, so wiring it would have shown users the key name.
    """
    declared = set(
        mod.flatten_json(
            json.loads((mod.LOCALES_DIR / "en-US.json").read_text(encoding="utf-8")),
        ),
    )
    missing = mod.find_backend_keys_without_a_string(declared)
    assert missing == [], (
        f"backend names i18n keys with no string behind them: {missing}"
    )


def test_check4_reports_a_key_with_no_string(mod, tmp_path, monkeypatch) -> None:
    """The check must bite, not return [] forever."""
    src = tmp_path / "py_modules" / "unifideck" / "svc.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        'emit_stage(bus, i18n_key="toasts.launcher.neverWritten")\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    found = mod.find_backend_keys_without_a_string({"toasts.launcher.real"})
    assert [k for k, _ in found] == ["toasts.launcher.neverWritten"]


def test_check4_accepts_a_key_that_exists(mod, tmp_path, monkeypatch) -> None:
    src = tmp_path / "py_modules" / "unifideck" / "svc.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        'emit_stage(bus, i18n_key="toasts.launcher.real")\n', encoding="utf-8",
    )
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    assert mod.find_backend_keys_without_a_string({"toasts.launcher.real"}) == []


def test_check4_ignores_config_keys_and_filenames(mod, tmp_path, monkeypatch) -> None:
    """The false-positive class that decided the check's scope.

    A first cut matched every ``"a.b.c"`` literal and reported nine
    non-i18n strings — ``sync.cooldown_seconds``, ``library.json``,
    ``launcher.exe`` and friends. Scoping to the two kwargs removes them
    without an allowlist.
    """
    src = tmp_path / "py_modules" / "unifideck" / "svc.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        'cfg.get("sync.cooldown_seconds")\n'
        'path = "library.json"\n'
        'find("launcher.exe")\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    assert mod.find_backend_keys_without_a_string(set()) == []
