"""Warcraft III: Reforged's crypt32 check — tell the user, never patch Proton.

Warcraft III: Reforged 3.0 calls ``CertCreateCertificateChainEngine()`` with
an 88-byte ``CERT_CHAIN_ENGINE_CONFIG`` and Wine 11.0's ``crypt32`` rejects
it, so the game reports a VPN error. GE-Proton11-7 carries the upstream fix.

0.7.6 development builds shipped a prebuilt patched ``crypt32.dll`` for
GE-Proton11-6 and built a hardlinked Proton variant around it. That binary
validated Blizzard's certificate chain and could not be rebuilt from source
here, so it was dropped. What is pinned now:

* the capability probe that recognises a fixed Proton;
* a warning toast, and no ``PROTONPATH`` rewrite, on an unfixed one;
* old variants are removed without touching the Proton they linked into;
* the bundled DLLs are gone from the repository.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.launcher.proton.fixes import wc3_crypt32
from unifideck.launcher.proton.handlers import battlenet_wc3

_REPO = Path(__file__).resolve().parents[2]


def _make_proton(root: Path, build: str, *, fixed: bool = False) -> Path:
    """A minimal Proton tree; ``fixed=True`` mimics GE-Proton11-7's crypt32."""
    proton = root / build
    wine = proton / "files" / "lib" / "wine"
    (wine / "x86_64-windows").mkdir(parents=True)
    (proton / "version").write_text(f"1787951532 {build}\n", encoding="utf-8")
    stock = b"stock 64-bit crypt32" + (b" dwExclusiveFlags %lx" if fixed else b"")
    (wine / "x86_64-windows" / "crypt32.dll").write_bytes(stock)
    return proton


@pytest.fixture
def variants_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "variants"
    monkeypatch.setattr(wc3_crypt32, "variants_root", lambda: root)
    return root


@pytest.fixture
def toasts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        battlenet_wc3, "launcher_toast",
        lambda key, **kw: sent.append({"key": key, **kw}),
    )
    monkeypatch.setattr(battlenet_wc3, "resolve_title", lambda _k: "Warcraft III")
    return sent


def _plan(proton: Path | None) -> Any:
    env = {"PROTONPATH": str(proton)} if proton else {}
    return SimpleNamespace(env=env, context=SimpleNamespace(game_key="battlenet:W3"))


class TestAffectedTitle:
    def test_only_warcraft_iii(self) -> None:
        assert wc3_crypt32.is_affected_title("W3")
        for family in ("WoW", "Pro", "OSI", "D3", "", "w3"):
            assert not wc3_crypt32.is_affected_title(family)
        assert not wc3_crypt32.is_affected_title(None)


class TestCapabilityProbe:
    def test_unfixed_and_fixed_builds(self, tmp_path: Path) -> None:
        assert not wc3_crypt32.proton_has_crypt32_fix(
            _make_proton(tmp_path, "GE-Proton11-6"),
        )
        assert wc3_crypt32.proton_has_crypt32_fix(
            _make_proton(tmp_path, "GE-Proton11-7", fixed=True),
        )

    def test_a_proton_without_crypt32_is_unfixed(self, tmp_path: Path) -> None:
        assert not wc3_crypt32.proton_has_crypt32_fix(tmp_path / "missing")


class TestLaunchCheck:
    def test_unfixed_proton_warns_and_is_left_selected(
        self, tmp_path: Path, variants_root: Path, toasts: list[dict[str, Any]],
    ) -> None:
        proton = _make_proton(tmp_path, "GE-Proton11-6")
        plan = _plan(proton)
        battlenet_wc3.apply_wc3_crypt32_fix(plan, "W3")
        assert plan.env["PROTONPATH"] == str(proton)  # never redirected
        assert [t["key"] for t in toasts] == ["toasts.launcher.wc3ProtonUnsupportedMessage"]
        assert toasts[0]["severity"] == "error"

    def test_fixed_proton_is_silent(
        self, tmp_path: Path, variants_root: Path, toasts: list[dict[str, Any]],
    ) -> None:
        proton = _make_proton(tmp_path, "GE-Proton11-7", fixed=True)
        battlenet_wc3.apply_wc3_crypt32_fix(_plan(proton), "W3")
        assert toasts == []

    def test_other_titles_are_untouched(
        self, tmp_path: Path, variants_root: Path, toasts: list[dict[str, Any]],
    ) -> None:
        battlenet_wc3.apply_wc3_crypt32_fix(_plan(_make_proton(tmp_path, "X")), "WoW")
        assert toasts == []

    def test_no_protonpath_is_not_fatal(
        self, variants_root: Path, toasts: list[dict[str, Any]],
    ) -> None:
        battlenet_wc3.apply_wc3_crypt32_fix(_plan(None), "W3")
        assert toasts == []


class TestStaleVariantCleanup:
    def _old_variant(self, variants_root: Path, proton: Path) -> Path:
        """What a 0.7.6 development build left: a hardlink tree plus our marker."""
        variant = variants_root / f"{proton.name}{wc3_crypt32.VARIANT_SUFFIX}"
        target = variant / "files" / "lib" / "wine" / "x86_64-windows"
        target.mkdir(parents=True)
        os.link(
            proton / "files" / "lib" / "wine" / "x86_64-windows" / "crypt32.dll",
            target / "kernel32-link.dll",
        )
        (variant / wc3_crypt32.MARKER_NAME).write_text("stamp", encoding="utf-8")
        return variant

    def test_removes_old_variants_without_touching_the_real_proton(
        self, tmp_path: Path, variants_root: Path,
    ) -> None:
        proton = _make_proton(tmp_path / "tools", "GE-Proton11-6")
        original = proton / "files" / "lib" / "wine" / "x86_64-windows" / "crypt32.dll"
        before = original.read_bytes()
        variant = self._old_variant(variants_root, proton)

        assert wc3_crypt32.remove_stale_variants() == [variant.name]
        assert not variant.exists()
        assert original.read_bytes() == before

    def test_leaves_anything_that_is_not_ours(self, variants_root: Path) -> None:
        unmarked = variants_root / "GE-Proton11-6-wc3fix"
        unmarked.mkdir(parents=True)
        other = variants_root / "something-else"
        other.mkdir()
        (other / wc3_crypt32.MARKER_NAME).write_text("x", encoding="utf-8")

        assert wc3_crypt32.remove_stale_variants() == []
        assert unmarked.is_dir() and other.is_dir()

    def test_a_missing_root_is_fine(self, variants_root: Path) -> None:
        assert wc3_crypt32.remove_stale_variants() == []

    def test_runs_on_a_warcraft_iii_launch(
        self, tmp_path: Path, variants_root: Path, toasts: list[dict[str, Any]],
    ) -> None:
        proton = _make_proton(tmp_path / "tools", "GE-Proton11-7", fixed=True)
        variant = self._old_variant(variants_root, proton)
        battlenet_wc3.apply_wc3_crypt32_fix(_plan(proton), "W3")
        assert not variant.exists()


def test_no_patched_crypt32_is_shipped() -> None:
    assert not (_REPO / "bin" / "stubs" / "wc3fix").exists()
