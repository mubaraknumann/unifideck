"""UD-026 (real cause): Epic installs must never reach legendary's SDL prompt.

The reported symptom was "Fallout 3: GOTY / Fallout: New Vegas fail to
download from Epic". 0.7.1 surfaced legendary's real error, which turned
out to be::

    args.install_tag = sdl_prompt(sdl_data, game.app_title)
      File ".../legendary/utils/cli.py", line 61, in sdl_prompt
        choices = input('Additional packs [Enter to confirm]: ')
    EOFError: EOF when reading a line

``sdl_prompt`` is the only interactive prompt in legendary's install
path that ``--yes`` does not gate, so with no usable stdin every
Selective Downloads title died instantly. It is not a Fallout bug: the
SDL title list is fetched from legendary's remote
``game_overrides.sdl_config`` (the hardcoded ``selective_dl.py`` table is
only a fallback), and it covers ~19 titles including Hogwarts Legacy and
Cyberpunk 2077.

So the installer answers the prompt up front — ``--skip-sdl`` always,
plus explicit ``--install-tag`` values for the base game, every required
tag, every non-language extra, and one language pack matched to the user.

Pure-logic tests: legendary's config dir is redirected to a tmp dir and
the SDL HTTP fetch is stubbed, so nothing here reads the developer's real
legendary state or touches the network.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from unifideck.services.download.models import classify_download_error
from unifideck.stores.epic import sdl
from unifideck.stores.epic.install import EpicInstaller, _is_prompt_crash

# --------------------------------------------------------------------------
# Fixtures / fakes
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_legendary_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Never read the developer's real ``~/.config/legendary``."""
    monkeypatch.setenv("LEGENDARY_CONFIG_DIR", str(tmp_path / "legendary"))
    monkeypatch.setattr(
        sdl, "_CACHE_DIR", str(tmp_path / "cache" / "epic_sdl"),
    )


def _write_version_json(tmp_path: Path, sdl_config: dict[str, Any]) -> None:
    """Write a legendary ``version.json`` carrying an SDL config."""
    cfg = tmp_path / "legendary"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "version.json").write_text(
        json.dumps({"data": {"game_overrides": {"sdl_config": sdl_config}}}),
    )


# Shapes taken from real api.legendary.gl payloads; inner tag names are
# representative (the API returns manifest-specific chunk/voice tags).
_LANG_ONLY = {
    "de": {"tags": ["voice_de"], "name": "Deutsch"},
    "es": {"tags": ["voice_es"], "name": "español"},
    "fr": {"tags": ["voice_fr"], "name": "français"},
    "it": {"tags": ["voice_it"], "name": "italiano"},
}
_WITH_EXTRA = {
    "hd_textures": {"tags": ["chunk10optional"], "name": "HD Textures"},
    "de": {"tags": ["voice_de"], "name": "(Additional Languages) Deutsch"},
    "fr": {"tags": ["voice_fr"], "name": "(Additional Languages) français"},
}
_FORTNITE_LIKE = {
    "__required": {"tags": ["chunk0", "chunk10"], "name": "Fortnite Core"},
    "stw": {"tags": ["chunk11"], "name": "Fortnite Save the World"},
    "hd_textures": {"tags": ["chunk10optional"], "name": "High Resolution Textures"},
    "lang_de": {"tags": ["chunk2"], "name": "(Language Pack) Deutsch"},
}
# legendary types Czech as "cr"; only the display name identifies it.
_CZECH_QUIRK = {
    "cr": {"tags": ["voice_cs"], "name": "(Language Pack) čeština"},
    "de": {"tags": ["voice_de"], "name": "(Language Pack) Deutsch"},
}


def _installer() -> EpicInstaller:
    """Build an installer with mocked collaborators (no real I/O)."""
    inst = EpicInstaller.__new__(EpicInstaller)
    inst._bus = AsyncMock()
    inst._cli_path = "/opt/plugin/bin/legendary"
    inst._library = AsyncMock()
    inst._library.invalidate_installed_cache = lambda: None
    inst._exe_resolver = AsyncMock()
    inst._default_install_root = "/games"
    inst._install_timeout = 7200
    inst._uninstall_timeout = 120
    return inst


class _FakeStdout:
    """A stream whose ``readline`` yields scripted bytes then EOF."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = [f"{ln}\n".encode() for ln in lines]

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    """Minimal stand-in for an asyncio subprocess."""

    def __init__(self, lines: list[str], returncode: int) -> None:
        self.stdout = _FakeStdout(lines)
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:  # pragma: no cover - timeout path unused here
        pass


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch, procs: list[_FakeProc],
) -> tuple[list[list[str]], list[dict[str, Any]]]:
    """Feed ``procs`` to successive spawns; record argv and kwargs."""
    seen: list[list[str]] = []
    kwargs_seen: list[dict[str, Any]] = []
    queue = list(procs)

    async def fake_exec(*cmd: str, **kw: Any) -> _FakeProc:
        seen.append(list(cmd))
        kwargs_seen.append(kw)
        return queue.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen, kwargs_seen


def _download_events(bus: AsyncMock) -> list[str]:
    """Names of every ``DOWNLOAD_*`` event the installer emitted.

    Always empty: ``DownloadWorker`` is the sole emitter of the download
    lifecycle, and the installer reports through its ``InstallResult``
    only (audit item #4).
    """
    out = []
    for call in bus.emit.await_args_list:
        args, kwargs = call
        name = args[0] if args else kwargs.get("event")
        value = getattr(name, "value", name)
        if isinstance(value, str) and value.startswith("download_"):
            out.append(value)
    return out


def _tags_after(cmd: list[str]) -> list[str]:
    """Extract the ordered ``--install-tag`` values from an argv."""
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "--install-tag"]


# --------------------------------------------------------------------------
# select_install_tags — the core policy
# --------------------------------------------------------------------------
def test_base_tag_always_first() -> None:
    # The empty tag means "every untagged file", i.e. the base game. Without
    # it legendary would install ONLY the optional packs.
    tags = sdl.select_install_tags(_LANG_ONLY, "de-DE")
    assert tags[0] == ""


def test_matches_requested_language_by_key() -> None:
    assert sdl.select_install_tags(_LANG_ONLY, "de-DE") == ["", "voice_de"]


def test_matches_requested_language_by_display_name() -> None:
    # legendary keys some packs opaquely ("lang_de"); only the display
    # name says which language it is, so matching must reach the name.
    assert sdl.select_install_tags(_FORTNITE_LIKE, "de-DE") == [
        "", "chunk0", "chunk10", "chunk11", "chunk10optional", "chunk2",
    ]


def test_matches_bare_two_letter_request() -> None:
    assert sdl.select_install_tags(_LANG_ONLY, "it") == ["", "voice_it"]


def test_region_variant_prefers_its_own_key() -> None:
    # "es-MX" must not silently land on the Spain variant.
    data = {
        "es_es": {"tags": ["voice_es_es"], "name": "español (España)"},
        "es_mx": {"tags": ["voice_es_mx"], "name": "español (México)"},
    }
    assert sdl.select_install_tags(data, "es-MX") == ["", "voice_es_mx"]


def test_falls_back_to_english_when_language_absent() -> None:
    # No English pack is offered here (English is the untagged base), so
    # the fallback must add no language pack rather than guessing one.
    assert sdl.select_install_tags(_LANG_ONLY, "ja-JP") == [""]


def test_english_request_installs_base_only() -> None:
    # English is the base game's own language, so choosing it must add no
    # pack — and must NOT fall through to some other language's pack.
    assert sdl.select_install_tags(_LANG_ONLY, "en-US") == [""]
    assert sdl.select_install_tags(_LANG_ONLY, "en") == [""]


def test_english_offered_is_selected_on_fallback() -> None:
    data = {
        "en": {"tags": ["voice_en"], "name": "(Language Pack) English"},
        "de": {"tags": ["voice_de"], "name": "(Language Pack) Deutsch"},
    }
    # Unavailable language → English, not the first listed option.
    assert sdl.select_install_tags(data, "th-TH") == ["", "voice_en"]


def test_no_requested_language_defaults_to_english() -> None:
    data = {
        "en": {"tags": ["voice_en"], "name": "English"},
        "de": {"tags": ["voice_de"], "name": "Deutsch"},
    }
    assert sdl.select_install_tags(data, None) == ["", "voice_en"]


def test_non_language_extras_are_always_included() -> None:
    tags = sdl.select_install_tags(_WITH_EXTRA, "fr-FR")
    assert "chunk10optional" in tags  # HD textures: taken unconditionally
    assert "voice_fr" in tags
    assert "voice_de" not in tags  # other languages skipped


def test_required_tags_are_preserved_with_extras() -> None:
    tags = sdl.select_install_tags(_FORTNITE_LIKE, "de-DE")
    # Required core + both non-language extras + the matched language.
    assert set(tags) == {"", "chunk0", "chunk10", "chunk11", "chunk10optional", "chunk2"}


def test_only_one_language_pack_is_ever_selected() -> None:
    many = {
        code: {"tags": [f"voice_{code}"], "name": code}
        for code in ("de", "es", "fr", "it", "ja", "ko", "pl", "ru", "pt")
    }
    tags = sdl.select_install_tags(many, "fr-FR")
    # The whole point: a Deck must not swallow nine voice packs.
    assert sum(1 for t in tags if t.startswith("voice_")) == 1


def test_explicit_option_key_is_taken_verbatim() -> None:
    # The picker lists the title's own option keys, so a user's pick must
    # not be re-matched into a different variant.
    data = {
        "es_es": {"tags": ["voice_es_es"], "name": "español (España)"},
        "es_mx": {"tags": ["voice_es_mx"], "name": "español (México)"},
    }
    assert sdl.select_install_tags(data, "es_mx") == ["", "voice_es_mx"]


def test_tags_are_deduped_and_ordered() -> None:
    # __required repeating the base tag must not duplicate it.
    data = {
        "__required": {"tags": ["", "English"], "name": "Core"},
        "de": {"tags": ["voice_de"], "name": "Deutsch"},
    }
    tags = sdl.select_install_tags(data, "de")
    assert tags == ["", "English", "voice_de"]


def test_malformed_option_is_ignored() -> None:
    data = {"de": "not-a-dict", "fr": {"tags": ["voice_fr"], "name": "français"}}
    assert sdl.select_install_tags(data, "fr") == ["", "voice_fr"]


# --------------------------------------------------------------------------
# language_options — what the picker shows
# --------------------------------------------------------------------------
def test_language_options_excludes_non_language_extras() -> None:
    opts = sdl.language_options(_WITH_EXTRA)
    assert set(opts) == {"en", "de", "fr"}  # "en" is the base-game stand-in
    assert "hd_textures" not in opts


def test_language_options_carry_legendary_display_names() -> None:
    assert sdl.language_options(_LANG_ONLY)["de"] == "Deutsch"


# --------------------------------------------------------------------------
# The base-language entry — UD-026 follow-up
# --------------------------------------------------------------------------
def test_english_is_offered_even_though_legendary_omits_it() -> None:
    # legendary lists only the *additional* packs, so English (the
    # untagged base game) appears nowhere in its config. Reported
    # symptom: the picker showed 7 languages, none of them English, and
    # pre-selected Deutsch — so an English user downloaded a German
    # voice pack by accepting the dialog.
    assert "en" not in _LANG_ONLY
    opts = sdl.language_options(_LANG_ONLY)
    assert opts["en"] == "English"


def test_base_language_is_listed_first() -> None:
    # Cosmetic but deliberate: English heads the dropdown rather than
    # being buried below the packs.
    assert next(iter(sdl.language_options(_LANG_ONLY))) == "en"


def test_base_language_selects_no_pack() -> None:
    # The synthetic entry has no row in the SDL config; contributing no
    # tag is exactly what "install the base game's language" means, and
    # it must not raise on the missing row.
    assert sdl.select_install_tags(_LANG_ONLY, "en") == [""]


def test_real_english_pack_is_not_duplicated() -> None:
    # A title that genuinely ships an English pack keeps its own entry
    # and must not gain a synthetic one alongside it.
    data = {
        "en": {"tags": ["voice_en"], "name": "(Language Pack) English"},
        "de": {"tags": ["voice_de"], "name": "(Language Pack) Deutsch"},
    }
    opts = sdl.language_options(data)
    assert set(opts) == {"en", "de"}
    assert opts["en"] == "(Language Pack) English"
    # ...and choosing it still downloads that real pack.
    assert sdl.select_install_tags(data, "en-US") == ["", "voice_en"]


def test_extras_only_title_offers_no_language_choice() -> None:
    # An HD-textures-only title has nothing to decide, so no picker and
    # no stray English entry.
    data = {"hd_textures": {"tags": ["chunk10optional"], "name": "HD Textures"}}
    assert sdl.language_options(data) == {}
    assert sdl.select_install_tags(data, "en-US") == ["", "chunk10optional"]


def test_czech_quirk_key_is_still_classified_as_a_language() -> None:
    # "cr" is not a language code in any standard — legendary's typo for
    # Czech. Only the display name identifies it, so classification must
    # fall through to the name, or Czech voice data would be treated as a
    # bonus extra and downloaded for everyone.
    opts = sdl.language_options(_CZECH_QUIRK)
    assert set(opts) == {"en", "cr", "de"}  # "en" is the base-game stand-in
    assert sdl.select_install_tags(_CZECH_QUIRK, "cs-CZ") == ["", "voice_cs"]


def test_required_block_is_not_offered_as_a_choice() -> None:
    assert "__required" not in sdl.language_options(_FORTNITE_LIKE)


# --------------------------------------------------------------------------
# SDL title detection — legendary's remote list, not the hardcoded table
# --------------------------------------------------------------------------
def test_sdl_app_names_read_from_legendary_version_json(tmp_path: Path) -> None:
    _write_version_json(tmp_path, {"Ginger": None, "fa4240e5": None})
    assert sdl.sdl_app_names() == {"Ginger", "fa4240e5"}


def test_missing_version_json_yields_no_sdl_titles() -> None:
    # A fresh install has no cached version file; that must degrade to
    # "no SDL titles" rather than raising.
    assert sdl.sdl_app_names() == set()


def test_sdl_key_matches_by_prefix(tmp_path: Path) -> None:
    _write_version_json(tmp_path, {"Fortnite": None})
    assert sdl._sdl_key_for("FortniteBrEditor") == "Fortnite"


def test_sdl_key_ignores_mac_entries(tmp_path: Path) -> None:
    _write_version_json(tmp_path, {"Fortnite_Mac": None})
    assert sdl._sdl_key_for("Fortnite_Mac") is None


def test_sdl_key_prefers_longest_match(tmp_path: Path) -> None:
    _write_version_json(tmp_path, {"Fort": None, "Fortnite": None})
    assert sdl._sdl_key_for("Fortnite") == "Fortnite"


def test_non_sdl_title_has_no_key(tmp_path: Path) -> None:
    _write_version_json(tmp_path, {"Ginger": None})
    assert sdl._sdl_key_for("SomeOtherGame") is None


# --------------------------------------------------------------------------
# fetch_sdl_data — network + cache degradation
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_non_sdl_title_never_fetches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _write_version_json(tmp_path, {"Ginger": None})

    async def boom(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("must not hit the network for a non-SDL title")

    monkeypatch.setattr(sdl, "_get_sdl_json", boom)
    assert await sdl.fetch_sdl_data("PlainGame") is None


@pytest.mark.asyncio
async def test_non_sdl_title_says_so_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The "no language picker" decision must be visible in a bundle.

    A tester who saw the picker on Fallout: New Vegas reported "GTA V /
    RDR2 / BioShock give no language options" as a bug. The code was
    right — none of them is an SDL title — but the support bundle held
    no evidence either way, because this path returned silently.
    """
    _write_version_json(tmp_path, {"Ginger": None})
    with caplog.at_level(logging.INFO, logger=sdl.logger.name):
        assert await sdl.fetch_sdl_data("PlainGame") is None
        assert await sdl.resolve_install_tags("PlainGame", "en-US") == []
        assert await sdl.resolve_language_options("PlainGame") == {}
    assert "PlainGame is not a Selective Downloads title" in caplog.text


@pytest.mark.asyncio
async def test_fetch_caches_for_later_offline_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _write_version_json(tmp_path, {"Ginger": None})

    async def ok(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return _LANG_ONLY

    monkeypatch.setattr(sdl, "_get_sdl_json", ok)
    assert await sdl.fetch_sdl_data("Ginger") == _LANG_ONLY

    # Same title, network now down → the cached copy keeps it installable.
    async def down(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(sdl, "_get_sdl_json", down)
    assert await sdl.fetch_sdl_data("Ginger") == _LANG_ONLY


@pytest.mark.asyncio
async def test_fetch_failure_without_cache_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _write_version_json(tmp_path, {"Ginger": None})

    async def down(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(sdl, "_get_sdl_json", down)
    # None, not an exception: the caller falls back to --skip-sdl.
    assert await sdl.fetch_sdl_data("Ginger") is None


@pytest.mark.asyncio
async def test_resolve_install_tags_empty_for_unresolvable_title(
    tmp_path: Path,
) -> None:
    assert await sdl.resolve_install_tags("Whatever", "de-DE") == []


# --------------------------------------------------------------------------
# _build_install_cmd — the flags that actually fix the bug
# --------------------------------------------------------------------------
def test_skip_sdl_is_always_passed() -> None:
    # The belt: inert on a non-SDL title, but guarantees legendary can
    # never reach sdl_prompt even if tag resolution came up empty.
    cmd = _installer()._build_install_cmd("/games", "abc", with_dlc=True)
    assert "--skip-sdl" in cmd
    assert _tags_after(cmd) == []


def test_resolved_tags_become_repeated_install_tag_flags() -> None:
    cmd = _installer()._build_install_cmd(
        "/games", "abc", with_dlc=True,
        install_tags=["", "chunk0", "voice_de"],
    )
    assert _tags_after(cmd) == ["", "chunk0", "voice_de"]


def test_install_tags_do_not_disturb_dlc_flags() -> None:
    cmd = _installer()._build_install_cmd(
        "/games", "abc", with_dlc=False, install_tags=["", "voice_de"],
    )
    assert "--skip-dlcs" in cmd
    assert "--with-dlcs" not in cmd


# --------------------------------------------------------------------------
# End-to-end: no prompt reachable, no wasted retry
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_install_passes_resolved_tags_and_closes_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _write_version_json(tmp_path, {"Ginger": None})

    async def ok(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return _LANG_ONLY

    monkeypatch.setattr(sdl, "_get_sdl_json", ok)
    inst = _installer()
    from unifideck.core.types import InstallResult
    inst._finalize_install = AsyncMock(
        return_value=InstallResult(success=True, store="epic", game_id="Ginger"),
    )
    # install_game verifies legendary's own installed.json before
    # reporting success (legendary answers a refusal with exit 0 — see
    # test_epic_phantom_install.py), so a mocked rc=0 must leave the row.
    (tmp_path / "legendary").mkdir(parents=True, exist_ok=True)
    (tmp_path / "legendary" / "installed.json").write_text(
        json.dumps({"Ginger": {"install_path": "/games/Ginger"}}),
    )
    seen, kwargs_seen = _patch_subprocess(
        monkeypatch, [_FakeProc(["Progress: 100.0%"], returncode=0)],
    )

    result = await inst.install_game(
        "Ginger", base_path=str(tmp_path / "games"), language="fr-FR",
    )

    assert result.success
    assert _tags_after(seen[0]) == ["", "voice_fr"]
    # stdin must be closed, so a future prompt errors instead of hanging
    # the whole download queue on an invisible question.
    assert kwargs_seen[0]["stdin"] is asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_prompt_crash_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    traceback_tail = (
        "args.install_tag = sdl_prompt(sdl_data, game.app_title) | "
        "EOFError: EOF when reading a line"
    )
    seen, _ = _patch_subprocess(
        monkeypatch, [_FakeProc([traceback_tail], returncode=1)],
    )
    inst = _installer()

    result = await inst.install_game("g", base_path=str(tmp_path))

    assert not result.success
    # Only ONE attempt: the DLC fallback would hit the same prompt, so
    # retrying just doubled the user's wait before the same failure.
    assert len(seen) == 1
    # The failure travels back as the InstallResult, not as an event of the
    # installer's own — the worker emits the single DOWNLOAD_FAILED.
    assert _download_events(inst._bus) == []
    assert "EOFError" in result.error


def test_prompt_crash_detection() -> None:
    assert _is_prompt_crash("EOFError: EOF when reading a line")
    assert _is_prompt_crash("in sdl_prompt | choices = input(...)")
    assert not _is_prompt_crash("[cli] ERROR: Failed to download DLC xyz")


def test_prompt_crash_is_classified_distinctly() -> None:
    err = "legendary_exit_1: in sdl_prompt | EOFError: EOF when reading a line"
    assert classify_download_error(Exception(err)) == "cli_prompt_blocked"
