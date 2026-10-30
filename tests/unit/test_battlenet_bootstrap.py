"""Battle.net client bootstrap: fetch the installer, run it, tweak the prefix.

Nothing is bundled — the client is downloaded at runtime from Blizzard's
own installer URL, the same shape as Ubisoft's UPC bootstrap. These tests
pin the failure modes that would otherwise present as a hang rather than an
error:

  * no display environment (the headless Decky env) — a Wine process
    without DISPLAY hangs instead of failing,
  * no 32-bit Vulkan — the client is PE32 i386 and its installer freezes at
    roughly 25% with no message,
  * a truncated download — an error page cached as if it were the stub.

The 32-bit Vulkan case is **not** a refusal any more, and the tests below
pin that. It used to be: a filename-based probe reported "missing" on a
machine that had the driver, and the user was left with no client at all.
Now every verdict installs — ``ABSENT`` merely arms a stall watchdog — and
the failure is reported only after an attempt that actually stopped moving.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from unifideck.stores.battlenet import paths
from unifideck.stores.battlenet.prefix import client_install as ci
from unifideck.stores.battlenet.prefix import stall_watchdog as sw
from unifideck.utils.vulkan import Vulkan32, Vulkan32Report


class _Resolver:
    """Stand-in for WineEnvResolver."""

    def __init__(self, *, umu: str | None = "/bin/umu-run", display: bool = True) -> None:
        self._umu = umu
        self._display = display
        self.built: dict[str, str] | None = None

    def find_umu_run(self) -> str | None:
        return self._umu

    def build_env(self, prefix: Any, gameid: str, **_kw: Any) -> dict[str, str]:
        env = {"WINEPREFIX": str(prefix), "GAMEID": gameid, "STORE": "battlenet"}
        if self._display:
            env["DISPLAY"] = ":0"
        self.built = env
        return env


def _install_client(prefix: Path) -> None:
    d = prefix / "drive_c" / paths.CLIENT_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / paths.CLIENT_EXE).write_bytes(b"MZ")
    (d / paths.LAUNCHER_EXE).write_bytes(b"MZ")
    # The versioned payload the shim loads. Without it the prefix is
    # the shape an interrupted install leaves and no client can start.
    build = d / "Battle.net.17651"
    build.mkdir(exist_ok=True)
    (build / paths.CLIENT_DLL).write_bytes(b"MZ")


def _verdict(monkeypatch: pytest.MonkeyPatch, verdict: Vulkan32) -> None:
    """Force the host 32-bit Vulkan verdict the gate will see."""
    monkeypatch.setattr(
        ci, "detect_32bit_vulkan", lambda: Vulkan32Report(verdict, [], []),
    )


# --------------------------------------------------------------------------
# installer caching
# --------------------------------------------------------------------------


def test_a_valid_cached_installer_is_reused(tmp_path: Path) -> None:
    cached = tmp_path / "Battle.net-Setup.exe"
    cached.write_bytes(b"x" * (ci.MIN_INSTALLER_BYTES + 1))
    result = asyncio.run(ci.ensure_installer("https://example.invalid", cached))
    assert result == cached


def test_a_truncated_cached_installer_is_re_downloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached error page must not be treated as the installer."""
    cached = tmp_path / "Battle.net-Setup.exe"
    cached.write_bytes(b"<html>404</html>")
    calls: list[str] = []

    def fake_download(url: str, dest: Path) -> bool:
        calls.append(url)
        dest.write_bytes(b"x" * (ci.MIN_INSTALLER_BYTES + 1))
        return True

    monkeypatch.setattr(ci, "_download_sync", fake_download)
    assert asyncio.run(ci.ensure_installer("https://example.invalid", cached)) == cached
    assert calls == ["https://example.invalid"]


def test_a_failed_download_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ci, "_download_sync", lambda url, dest: False)
    assert asyncio.run(
        ci.ensure_installer("https://example.invalid", tmp_path / "x.exe"),
    ) is None


def test_a_short_response_is_discarded_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = tmp_path / "Battle.net-Setup.exe"

    class _Resp:
        def read(self, *_a: object) -> bytes:
            return b""

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    def fake_urlopen(*_a: object, **_k: object) -> _Resp:
        return _Resp()

    monkeypatch.setattr(ci.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ci.shutil, "copyfileobj", lambda src, dst: dst.write(b"tiny"))
    assert ci._download_sync("https://example.invalid", dest) is False
    assert not dest.exists()


# --------------------------------------------------------------------------
# preconditions that would otherwise hang
# --------------------------------------------------------------------------


def test_refuses_to_run_without_a_display(tmp_path: Path) -> None:
    """Headless Decky: a Wine process with no DISPLAY hangs, not errors."""
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"MZ")
    ok = asyncio.run(
        ci.run_silent_install(installer, tmp_path / "pfx", _Resolver(display=False)),
    )
    assert ok.installed is False


def test_refuses_to_run_without_umu(tmp_path: Path) -> None:
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"MZ")
    ok = asyncio.run(
        ci.run_silent_install(installer, tmp_path / "pfx", _Resolver(umu=None)),
    )
    assert ok.installed is False


@pytest.mark.parametrize(
    "verdict", [Vulkan32.ABSENT, Vulkan32.UNKNOWN, Vulkan32.PRESENT],
)
def test_no_vulkan_verdict_blocks_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: Vulkan32,
) -> None:
    """The regression: a probe that said "no" cost a user their client.

    A CachyOS host with a working 32-bit RADV driver was told it had none
    and the install was refused before the installer URL was even
    contacted. Every verdict now reaches the installer — including the one
    that means "I could not tell".
    """
    attempted: list[float | None] = []

    async def fake_install(
        _installer: Path, target: Path, _resolver: Any, *,
        stall_timeout: float | None = None, proton_path: str | None = None,
    ) -> ci.InstallOutcome:
        attempted.append(stall_timeout)
        _install_client(target)
        return ci.InstallOutcome(installed=True)

    _verdict(monkeypatch, verdict)
    monkeypatch.setattr(ci, "run_silent_install", fake_install)
    monkeypatch.setattr(
        ci, "ensure_installer",
        lambda url, cache: asyncio.sleep(0, result=Path("/tmp/setup.exe")),
    )
    result = asyncio.run(
        ci.bootstrap_client(
            tmp_path / "pfx",
            installer_url="https://example.invalid",
            installer_cache=tmp_path / "x.exe",
            resolver=_Resolver(),
        ),
    )
    assert result.success is True
    assert len(attempted) == 1, "the installer must run whatever the verdict"


def test_only_a_proven_absence_arms_the_stall_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shorter leash is for hosts we *know* lack the driver, not for doubt."""
    leashes: list[float | None] = []

    async def fake_install(
        _installer: Path, target: Path, _resolver: Any, *,
        stall_timeout: float | None = None, proton_path: str | None = None,
    ) -> ci.InstallOutcome:
        leashes.append(stall_timeout)
        _install_client(target)
        return ci.InstallOutcome(installed=True)

    monkeypatch.setattr(ci, "run_silent_install", fake_install)
    monkeypatch.setattr(
        ci, "ensure_installer",
        lambda url, cache: asyncio.sleep(0, result=Path("/tmp/setup.exe")),
    )
    for index, verdict in enumerate(
        (Vulkan32.PRESENT, Vulkan32.UNKNOWN, Vulkan32.ABSENT),
    ):
        _verdict(monkeypatch, verdict)
        asyncio.run(
            ci.bootstrap_client(
                tmp_path / f"pfx{index}",
                installer_url="https://example.invalid",
                installer_cache=tmp_path / "x.exe",
                resolver=_Resolver(),
            ),
        )
    assert leashes == [None, None, ci.NO_VULKAN_STALL_SECONDS]


def test_a_proven_absence_warns_before_it_tries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user is told what to install, while the attempt still happens."""
    warnings: list[int] = []

    async def fake_install(
        _installer: Path, target: Path, _resolver: Any, *,
        stall_timeout: float | None = None, proton_path: str | None = None,
    ) -> ci.InstallOutcome:
        _install_client(target)
        return ci.InstallOutcome(installed=True)

    _verdict(monkeypatch, Vulkan32.ABSENT)
    monkeypatch.setattr(ci, "run_silent_install", fake_install)
    monkeypatch.setattr(
        ci, "ensure_installer",
        lambda url, cache: asyncio.sleep(0, result=Path("/tmp/setup.exe")),
    )
    asyncio.run(
        ci.bootstrap_client(
            tmp_path / "pfx",
            installer_url="https://example.invalid",
            installer_cache=tmp_path / "x.exe",
            resolver=_Resolver(),
            on_warning=lambda: warnings.append(1),
        ),
    )
    assert warnings == [1]


def test_a_stalled_install_is_reported_as_the_vulkan_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only *after* an attempt that stopped moving — never instead of one."""

    async def fake_install(
        _installer: Path, _target: Path, _resolver: Any, *,
        stall_timeout: float | None = None, proton_path: str | None = None,
    ) -> ci.InstallOutcome:
        return ci.InstallOutcome(installed=False, stalled=True)

    _verdict(monkeypatch, Vulkan32.ABSENT)
    monkeypatch.setattr(ci, "run_silent_install", fake_install)
    monkeypatch.setattr(
        ci, "ensure_installer",
        lambda url, cache: asyncio.sleep(0, result=Path("/tmp/setup.exe")),
    )
    result = asyncio.run(
        ci.bootstrap_client(
            tmp_path / "pfx",
            installer_url="https://example.invalid",
            installer_cache=tmp_path / "x.exe",
            resolver=_Resolver(),
        ),
    )
    assert result.success is False
    assert result.error_code == "missing_32bit_vulkan"


def test_a_plain_install_failure_is_not_blamed_on_vulkan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_install(
        _installer: Path, _target: Path, _resolver: Any, *,
        stall_timeout: float | None = None, proton_path: str | None = None,
    ) -> ci.InstallOutcome:
        return ci.InstallOutcome(installed=False)

    _verdict(monkeypatch, Vulkan32.PRESENT)
    monkeypatch.setattr(ci, "run_silent_install", fake_install)
    monkeypatch.setattr(
        ci, "ensure_installer",
        lambda url, cache: asyncio.sleep(0, result=Path("/tmp/setup.exe")),
    )
    result = asyncio.run(
        ci.bootstrap_client(
            tmp_path / "pfx",
            installer_url="https://example.invalid",
            installer_cache=tmp_path / "x.exe",
            resolver=_Resolver(),
        ),
    )
    assert result.error_code == "client_install_failed"


# --------------------------------------------------------------------------
# bootstrap outcomes
# --------------------------------------------------------------------------


def test_an_existing_client_short_circuits(tmp_path: Path) -> None:
    prefix = tmp_path / "pfx"
    _install_client(prefix)
    result = asyncio.run(
        ci.bootstrap_client(
            prefix,
            installer_url="https://example.invalid",
            installer_cache=tmp_path / "x.exe",
            resolver=_Resolver(),
        ),
    )
    assert result.success is True


def test_an_existing_client_still_gets_its_tweaks(tmp_path: Path) -> None:
    """A prefix from an older plugin version must self-heal its settings.

    Note what this does *not* prove: that anything calls it. For the whole life
    of this code no game prefix ever reached here, because the launcher only
    bootstraps a prefix whose client exe is missing and every game prefix is
    cloned from a template that already has one. See
    :func:`test_ensure_tweaks_reaches_a_prefix_the_bootstrap_never_will`.
    """
    from unifideck.stores.battlenet.prefix import tweaks

    prefix = tmp_path / "pfx"
    _install_client(prefix)
    assert tweaks.tweaks_applied(prefix) is False
    asyncio.run(
        ci.bootstrap_client(
            prefix,
            installer_url="https://example.invalid",
            installer_cache=tmp_path / "x.exe",
            resolver=_Resolver(),
        ),
    )
    assert tweaks.tweaks_applied(prefix) is True


class _TweakPlan:
    def __init__(self, prefix: Path) -> None:
        self.prefix_path = prefix


def test_ensure_tweaks_reaches_a_prefix_the_bootstrap_never_will(
    tmp_path: Path,
) -> None:
    """The launch-path entry point, for a prefix that arrived pre-populated.

    Measured on-device: no ``Battle.net.config`` in the auth prefix, the
    template or a game prefix carried ``HardwareAcceleration``, and no prefix
    carried the tweak marker - so the hardware-acceleration workaround was not
    in force anywhere, on any prefix, ever.
    """
    from unifideck.launcher.proton.handlers import battlenet_bootstrap as bootstrap
    from unifideck.stores.battlenet.prefix import tweaks

    prefix = tmp_path / "pfx"
    _install_client(prefix)

    assert asyncio.run(bootstrap.ensure_tweaks(_TweakPlan(prefix))) is True
    assert tweaks.tweaks_applied(prefix) is True
    config = paths.client_config(prefix)
    assert config is not None
    assert '"HardwareAcceleration": "false"' in config.read_text()


def test_ensure_tweaks_is_marker_gated(tmp_path: Path) -> None:
    """One stat on the normal path: it runs before every client start."""
    from unifideck.launcher.proton.handlers import battlenet_bootstrap as bootstrap

    prefix = tmp_path / "pfx"
    _install_client(prefix)
    plan = _TweakPlan(prefix)

    assert asyncio.run(bootstrap.ensure_tweaks(plan)) is True
    assert asyncio.run(bootstrap.ensure_tweaks(plan)) is False


def test_ensure_tweaks_never_fails_a_launch(tmp_path: Path) -> None:
    """A prefix with no drive_c yet must be a quiet False, not an exception."""
    from unifideck.launcher.proton.handlers import battlenet_bootstrap as bootstrap

    assert asyncio.run(
        bootstrap.ensure_tweaks(_TweakPlan(tmp_path / "never-initialised")),
    ) is False


def test_a_failed_download_surfaces_a_structured_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verdict(monkeypatch, Vulkan32.PRESENT)
    monkeypatch.setattr(ci, "_download_sync", lambda url, dest: False)
    result = asyncio.run(
        ci.bootstrap_client(
            tmp_path / "pfx",
            installer_url="https://example.invalid",
            installer_cache=tmp_path / "x.exe",
            resolver=_Resolver(),
        ),
    )
    assert result.success is False
    assert result.error_code == "installer_download_failed"


def test_install_success_is_judged_by_the_filesystem_not_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stub has been seen exiting non-zero after a successful install."""
    prefix = tmp_path / "pfx"

    async def fake_install(
        _installer: Path, target: Path, _resolver: Any, *,
        stall_timeout: float | None = None, proton_path: str | None = None,
    ) -> ci.InstallOutcome:
        _install_client(target)
        return ci.InstallOutcome(installed=paths.client_installed(target))

    _verdict(monkeypatch, Vulkan32.PRESENT)
    monkeypatch.setattr(ci, "run_silent_install", fake_install)
    monkeypatch.setattr(
        ci, "ensure_installer",
        lambda url, cache: asyncio.sleep(0, result=Path("/tmp/setup.exe")),
    )
    result = asyncio.run(
        ci.bootstrap_client(
            prefix,
            installer_url="https://example.invalid",
            installer_cache=tmp_path / "x.exe",
            resolver=_Resolver(),
        ),
    )
    assert result.success is True
    assert paths.client_installed(prefix)


# --------------------------------------------------------------------------
# tweaks
# --------------------------------------------------------------------------


def test_hardware_acceleration_is_off_before_the_installer_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stub is CEF too, so the setting has to land first, not after.

    This is the one change that shrinks the 32-bit Vulkan dependency rather
    than measuring it: without hardware acceleration the client renders its
    login view in software, which is also the long-standing Wine fix for a
    spinner with no login buttons.
    """
    import json

    from unifideck.stores.battlenet.prefix import tweaks

    prefix = tmp_path / "pfx"
    (prefix / "drive_c").mkdir(parents=True)
    config_at_install: list[object] = []

    async def fake_install(
        _installer: Path, target: Path, _resolver: Any, *,
        stall_timeout: float | None = None, proton_path: str | None = None,
    ) -> ci.InstallOutcome:
        path = target / "drive_c" / tweaks.CONFIG_RELATIVE
        config_at_install.append(
            json.loads(path.read_text())["Client"]["HardwareAcceleration"]
            if path.exists() else None,
        )
        _install_client(target)
        return ci.InstallOutcome(installed=True)

    _verdict(monkeypatch, Vulkan32.PRESENT)
    monkeypatch.setattr(ci, "run_silent_install", fake_install)
    monkeypatch.setattr(
        ci, "ensure_installer",
        lambda url, cache: asyncio.sleep(0, result=Path("/tmp/setup.exe")),
    )
    asyncio.run(
        ci.bootstrap_client(
            prefix,
            installer_url="https://example.invalid",
            installer_cache=tmp_path / "x.exe",
            resolver=_Resolver(),
        ),
    )
    assert config_at_install == ["false"], "installer ran before the config was seeded"


# --------------------------------------------------------------------------
# the stall watchdog measures progress, not the clock
# --------------------------------------------------------------------------


class _FakeProc:
    """Just enough process to be killed."""

    def __init__(self) -> None:
        self.killed = False

    def kill(self) -> None:
        self.killed = True


@pytest.mark.asyncio
async def test_an_installer_writing_nothing_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = tmp_path / "pfx"
    prefix.mkdir()
    monkeypatch.setattr(sw, "STALL_POLL_SECONDS", 0.01)
    proc = _FakeProc()

    await sw.stall_watchdog(prefix, proc, 0.03)  # type: ignore[arg-type]

    assert proc.killed is True


@pytest.mark.asyncio
async def test_a_slow_but_growing_install_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole reason the leash is a stall timer and not a wall clock.

    A flat 300s cap would kill this install; it is making progress, just
    slowly — which is what a poor connection looks like, and is not what
    the 32-bit Vulkan freeze looks like.
    """
    prefix = tmp_path / "pfx"
    prefix.mkdir()
    monkeypatch.setattr(sw, "STALL_POLL_SECONDS", 0.01)
    proc = _FakeProc()

    async def grow() -> None:
        for index in range(12):
            (prefix / f"chunk{index}").write_bytes(b"x" * (index + 1))
            await asyncio.sleep(0.01)

    watchdog = asyncio.ensure_future(
        sw.stall_watchdog(prefix, proc, 0.03),  # type: ignore[arg-type]
    )
    await grow()
    watchdog.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await watchdog

    assert proc.killed is False


@pytest.mark.asyncio
async def test_a_killed_installer_is_reported_as_stalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring behind the error code: watchdog fired, so blame the stall."""
    prefix = tmp_path / "pfx"
    prefix.mkdir()
    monkeypatch.setattr(sw, "STALL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(ci.paths, "client_installed", lambda _p: False)

    class _Proc:
        returncode = -9

        def __init__(self) -> None:
            self._dead = asyncio.Event()

        def kill(self) -> None:
            self._dead.set()

        async def communicate(self) -> tuple[bytes, bytes]:
            await self._dead.wait()
            return b"", b""

    outcome = await ci._await_installer(_Proc(), prefix, 0.03)  # type: ignore[arg-type]

    assert outcome.installed is False
    assert outcome.stalled is True


@pytest.mark.asyncio
async def test_a_normal_failure_is_not_reported_as_stalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = tmp_path / "pfx"
    prefix.mkdir()
    monkeypatch.setattr(sw, "STALL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(ci.paths, "client_installed", lambda _p: False)

    class _Proc:
        returncode = 1

        def kill(self) -> None:
            return None

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    outcome = await ci._await_installer(_Proc(), prefix, 0.03)  # type: ignore[arg-type]

    assert outcome.installed is False
    assert outcome.stalled is False


def test_client_config_merge_preserves_saved_account(tmp_path: Path) -> None:
    """Clobbering the config would silently sign the user out."""
    import json

    from unifideck.stores.battlenet.prefix import tweaks

    prefix = tmp_path / "pfx"
    drive_c = prefix / "drive_c"
    cfg = drive_c / tweaks.CONFIG_RELATIVE
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"Client": {"SavedAccountNames": "me@example.com"}}))

    assert tweaks.write_client_config(drive_c) is True
    data = json.loads(cfg.read_text())
    assert data["Client"]["SavedAccountNames"] == "me@example.com"
    assert data["Client"]["HardwareAcceleration"] == "false"


# ── the installer must not open a wizard ──────────────────────────


def test_installer_args_preanswer_the_blocking_screens() -> None:
    """Launched bare, the bootstrapper waits on the language screen.

    Measured on-device three times in a row — Blizzard's own setup log
    stopped at ``Bootstrapper State: STATE_SELECT_LANGUAGE`` / ``Active
    screen changed: language`` and never moved, while the user saw a Sign In
    button that did nothing. With these arguments the same installer
    reported ``locale=enUS`` and went straight to STATE_CHECK_ENVIRONMENT →
    STATE_UPDATE_BOOTSTRAPPER → downloading.

    Pinned as a test because the failure is invisible: the install simply
    never finishes, and nothing in our own logs says why.
    """
    from unifideck.stores.battlenet.prefix.client_install import installer_args

    args = installer_args()
    assert any(
        a.startswith("--lang=") for a in args
    ), "language screen would block"
    assert any(
        a.startswith("--installpath=") for a in args
    ), "install-path screen would block"


@pytest.mark.parametrize(
    ("ui_locale", "expected"),
    [
        # Shipped by the client, so the bootstrapper gets the user's language
        # and, with it, a region that matches their account more often.
        ("de-DE", "--lang=deDE"),
        ("ko-KR", "--lang=koKR"),
        ("pt-BR", "--lang=ptBR"),
        # Also shipped, and previously missing from the map for no reason —
        # ``strings battle.net.dll`` (2026-08-23) lists both among the 22 the
        # client loads from ``languages.xml``. Turkish and Arabic users were
        # getting an English wizard and a US content warm-up.
        ("tr-TR", "--lang=trTR"),
        ("ar-SA", "--lang=arSA"),
        # Genuinely not shipped: the client has no Ukrainian. Passing a locale
        # it does not know risks the wizard stalling on its language screen,
        # invisibly, for the full 30-minute timeout.
        ("uk-UA", "--lang=enUS"),
        ("nl-NL", "--lang=enUS"),
        # No preference expressed.
        ("auto", "--lang=enUS"),
        ("", "--lang=enUS"),
    ],
)
def test_the_installer_language_follows_the_plugin_locale(
    monkeypatch: pytest.MonkeyPatch, ui_locale: str, expected: str,
) -> None:
    """The language is the user's; the install path is still pinned.

    Pinning the language to ``enUS`` was not merely an English wizard. The
    bootstrapper derives its region from the locale and warms the Agent's
    content store for it before login, so every non-US account threw that
    warm-up away and paid a 45-minute re-download on first install.
    """
    from unifideck.launcher import wrapper_locale
    from unifideck.stores.battlenet.prefix.client_install import installer_args

    # State the resolved locale rather than letting the resolver run: without
    # this the test machine's own language decides the result, which is how
    # two of these used to pass for the wrong reason.
    monkeypatch.setattr(wrapper_locale, "_RESOLVE_ATTEMPTED", True)
    monkeypatch.setattr(wrapper_locale, "_RESOLVED_LOCALE", ui_locale or None)

    args = installer_args()

    assert expected in args
    # The install path is hashed into Battle.net.config's section name, so it
    # must be identical in every prefix and must not follow the locale.
    assert any(a.startswith("--installpath=") for a in args)
    assert sum(a.startswith("--lang=") for a in args) == 1


@pytest.mark.asyncio
async def test_the_installer_is_invoked_with_those_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the wiring, not just the constant."""
    from unifideck.stores.battlenet.prefix import client_install as ci

    seen: list[tuple[str, ...]] = []

    class _Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def _fake_exec(*argv: str, **_kw: object) -> _Proc:
        seen.append(argv)
        return _Proc()

    monkeypatch.setattr(ci.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(ci.paths, "client_installed", lambda _p: True)

    class _Resolver:
        @staticmethod
        def find_umu_run() -> str:
            return "/bin/umu-run"

        @staticmethod
        def build_env(_prefix: object, _gameid: str, **_kw: Any) -> dict[str, str]:
            return {"DISPLAY": ":0"}

    ok = await ci.run_silent_install(
        tmp_path / "Battle.net-Setup.exe", tmp_path / "pfx", _Resolver(),
    )

    assert ok.installed is True
    assert seen, "installer was never spawned"
    for arg in ci.installer_args():
        assert arg in seen[0], f"{arg} missing from the installer command"


# ── the install lives behind RunGame, not on the auth RPC ─────────


@pytest.mark.asyncio
async def test_start_auth_does_not_install_and_returns_pending(
    tmp_path: Path,
) -> None:
    """The regression that produced a Sign In button doing nothing.

    ``AuthDispatcher.kickAndLaunch`` awaits this RPC *before* it RunGame-s
    the auth shortcut. Installing here blocked that call on a wizard with no
    gamescope session to render into, so the RPC never returned, the shortcut
    never launched, and ``bin/unifideck-launcher`` never ran at all.

    It must therefore return promptly, and it must return ``pending`` — that
    is the flag the dispatcher reads to decide to launch the shortcut.
    """
    from unifideck.stores.battlenet.store import BattlenetStore

    class _Bus:
        async def emit(self, *_a: object, **_k: object) -> None:
            return None

    class _Cache:
        def get(self, *_a: object, **_k: object) -> None:
            return None

    class _Config:
        def __init__(self, root: Path) -> None:
            self._v = {
                "data_dir": str(root),
                "prefixes_dir": str(root / "prefixes"),
                "installer_cache_dir": str(root / "cache"),
            }

        def get(self, key: str, default: object = None) -> object:
            return self._v if key == "stores.battlenet" else default

    store = BattlenetStore(
        _Bus(), _Cache(), plugin_dir="/plugin", config=_Config(tmp_path),
    )
    # No auth prefix at all — the "fresh install / after full cleanup" case.
    result = await store.start_auth()

    assert result.success is True, "a missing client must not fail the RPC"
    assert result.metadata["pending"] is True, "dispatcher needs this to RunGame"
    assert result.metadata["needs_bootstrap"] is True
    assert not (tmp_path / "prefixes" / ".bnet-auth").exists(), (
        "start_auth must not build the prefix — the launcher does"
    )


def test_the_launcher_owns_the_client_install() -> None:
    """The install helper must be reachable from the launcher handler."""
    from unifideck.launcher.proton.handlers import battlenet as h

    assert callable(h.bootstrap.install_client)


# --------------------------------------------------------------------------
# the tweak is read back, not merely written
# --------------------------------------------------------------------------


def test_the_written_config_is_confirmed_by_reading_it_back(tmp_path: Path) -> None:
    """Distinguishing "written and ignored" from "never written" took a
    whole round trip with a tester. One stat settles it in the log."""
    from unifideck.stores.battlenet.prefix import tweaks

    drive_c = tmp_path / "drive_c"
    assert tweaks.write_client_config(drive_c) is True

    written = json.loads(
        (drive_c / tweaks.CONFIG_RELATIVE).read_text(encoding="utf-8"),
    )
    assert written["Client"]["HardwareAcceleration"] == "false"


def test_a_config_that_does_not_keep_our_settings_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent write that does not stick is the failure mode being closed."""
    from unifideck.stores.battlenet.prefix import tweaks

    drive_c = tmp_path / "drive_c"
    # Something else rewrites the file between our write and our read.
    monkeypatch.setattr(
        tweaks, "_load_config", lambda _p: {"Client": {"HardwareAcceleration": "true"}},
    )
    assert tweaks.write_client_config(drive_c) is False


def test_the_user_login_state_is_preserved_by_the_merge(tmp_path: Path) -> None:
    """Clobbering the file would sign the user out of a prefix they had
    already authenticated."""
    from unifideck.stores.battlenet.prefix import tweaks

    drive_c = tmp_path / "drive_c"
    path = drive_c / tweaks.CONFIG_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"Client": {"SavedAccountNames": "someone@example.invalid"}}),
        encoding="utf-8",
    )

    assert tweaks.write_client_config(drive_c) is True

    kept = json.loads(path.read_text(encoding="utf-8"))["Client"]
    assert kept["SavedAccountNames"] == "someone@example.invalid"
    assert kept["HardwareAcceleration"] == "false"
