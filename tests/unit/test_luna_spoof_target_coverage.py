"""Property test for Luna platform-spoof target coverage (task 5.4).

Feature: amazon-luna-streaming, Property 1: Spoof target coverage

**Property 1:** For any set of browser targets attached during a Luna session —
regardless of count, type (page or service worker), or whether the target
existed at attach time or was created afterward — the ``LunaPlatformSpoofer``
applies the full platform override (UA string, UA Client Hints metadata with
platform=Windows, ``navigator.platform``, WebGL renderer) to that target
**before** the target is resumed from its waiting-for-debugger state.

**Validates: Requirements 14.1, 14.2, 14.3, 14.5, 14.6, 14.7**

The auto-attach loop (``LunaPlatformSpoofer.run_attach``) is driven against a
*mock CDP layer* (``_MockBrowserCdpSession``) standing in for the browser-level
``BrowserCdpSession`` — no real browser and no Amazon. The mock emits a
Hypothesis-generated set of targets of varying count, type, and *creation
order*: some exist at attach time, others appear only after the browser-level
``Target.setAutoAttach`` has been sent (modelling CDP auto-attach of
later-created targets). Every command the spoofer sends is recorded per
``sessionId`` with its order, so the test can assert, for *every* target, that
the full override landed and that the resume (``Runtime.runIfWaitingForDebugger``)
came strictly last — i.e. no target is ever left unspoofed.

≥100 iterations (Hypothesis ``max_examples=200``).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from unifideck.stores.luna.luna_config import LunaConfig
from unifideck.stores.luna.luna_platform_spoofer import (
    LunaPlatformSpoofer,
    build_platform_override_script,
    build_set_user_agent_override_params,
    build_spoof_identity,
    derive_browser_version,
)

# A UA template carrying the mandatory ``{version}`` placeholder (Req 12.6) so
# a correct spoofer substitutes a runtime version rather than a hardcoded one.
_UA_TEMPLATE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"
)

_CONFIG = LunaConfig(
    luna_host="luna.amazon.de",
    allowed_stream_hosts=["luna.amazon.de"],
    signin_path="/signin",
    player_path="/player",
    profile_dir="/home/deck/.local/share/unifideck/luna-profile",
    browser_game_cdp_flavour="browser_game",
    spoof_ua_template=_UA_TEMPLATE,
    spoof_uach_platform="Windows",
    spoof_navigator_platform="Win32",
    spoof_webgl_renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX, Direct3D11)",
)

# The real installed-browser version the mock reports for the one derive call.
_BROWSER_FIELD = "Chrome/150.0.7871.186"

# The CDP commands that together make up the "full platform override" the
# property requires on every target, in the order they must precede resume.
_UA_OVERRIDE = "Emulation.setUserAgentOverride"
_ADD_SCRIPT = "Page.addScriptToEvaluateOnNewDocument"
_RESUME = "Runtime.runIfWaitingForDebugger"

#: The ``Page.*`` methods a non-``page`` target rejects with ``-32601``. The
#: spoofer must tolerate the rejection without aborting the whole attach.
_PAGE_METHODS = {"Page.enable", _ADD_SCRIPT}


class _FakeEdgeCDP:
    """Mock ``EdgeCDPClient`` for the one real version derive in ``run_attach``.

    ``run_attach`` is called directly with a session, so only
    ``derive_browser_version`` touches this; it just answers ``/json/version``.
    """

    def get_browser_version(self) -> str | None:
        return _BROWSER_FIELD


class _MockBrowserCdpSession:
    """Mock browser-level ``BrowserCdpSession`` emitting a scripted target set.

    ``initial`` targets are already attached when ``attached_targets`` is first
    consumed; ``late`` targets are revealed only *after* the browser-level
    ``Target.setAutoAttach`` command has been observed — modelling CDP
    auto-attaching targets created after attach. Every ``send`` is recorded as
    ``(session_id, method)`` in call order so per-target ordering is checkable.
    """

    def __init__(
        self,
        initial: list[tuple[str, dict[str, Any]]],
        late: list[tuple[str, dict[str, Any]]],
    ) -> None:
        self._initial = initial
        self._late = late
        self._auto_attach_sent = False
        self.calls: list[tuple[str | None, str]] = []
        # Map each session id to its target type so the mock can reject the
        # Page domain on non-page targets exactly as a real browser does.
        self._types = {
            sid: str(info.get("type", "?"))
            for sid, info in [*initial, *late]
        }

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((session_id, method))
        if method == "Target.setAutoAttach":
            # Browser-level enable: only now do later-created targets appear.
            self._auto_attach_sent = True
            return {}
        # The CDP ``Page`` domain only exists on real ``page`` targets. On a
        # ``background_page``/``service_worker``/``worker``/``other`` target
        # the real browser rejects ``Page.*`` with ``-32601`` ("wasn't
        # found"); mirror that so the spoofer's tolerance is exercised.
        if method in _PAGE_METHODS and self._types.get(session_id) != "page":
            raise RuntimeError(
                f"{method} failed: {{'code': -32601, 'message': "
                f'"\'{method}\' wasn\'t found"}}}}',
            )
        return {}

    async def attached_targets(
        self,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        # Pre-existing targets are seen first…
        for session_id, info in self._initial:
            yield session_id, info
        # …then targets that only materialise once auto-attach is on. The
        # spoofer must have sent setAutoAttach before consuming these, which
        # run_attach does (it is the first thing it sends).
        assert self._auto_attach_sent, (
            "late targets must not be delivered before Target.setAutoAttach"
        )
        for session_id, info in self._late:
            yield session_id, info


# ── Generators ──────────────────────────────────────────────────────
#
# A target is (session_id, type). Types span the kinds CDP auto-attach can
# surface — page, iframe, service/shared worker, other — so coverage must not
# depend on the target type. Session ids are unique per generated set.

_TARGET_TYPES = st.sampled_from(
    ["page", "iframe", "service_worker", "shared_worker", "worker", "other"],
)


@st.composite
def _target_sets(draw: Any) -> tuple[
    list[tuple[str, dict[str, Any]]], list[tuple[str, dict[str, Any]]],
]:
    """Yield ``(initial_targets, late_targets)`` with unique session ids."""
    count = draw(st.integers(min_value=0, max_value=10))
    types = [draw(_TARGET_TYPES) for _ in range(count)]
    # Split the set into "existed at attach" and "created later" at a random
    # cut so creation order (pre/post auto-attach) varies across examples.
    cut = draw(st.integers(min_value=0, max_value=count))
    targets = [
        (f"session-{i}", {"targetId": f"t{i}", "type": t})
        for i, t in enumerate(types)
    ]
    return targets[:cut], targets[cut:]


def _assert_target_fully_spoofed(
    session_id: str, target_type: str, methods: list[str],
) -> None:
    """Assert one target got its override, resumed, all strictly before resume.

    *Every* target — regardless of type — must receive the UA/UA-CH override
    and be resumed exactly once, with the UA override preceding the resume.
    A real ``page`` target must additionally get the pre-document script
    (``Page.addScriptToEvaluateOnNewDocument``) before resume. A non-``page``
    target rejects the ``Page`` domain with ``-32601``; the spoofer tolerates
    that and must still resume it (never leaving it paused) — so the script is
    absent but the resume is not.
    """
    assert _UA_OVERRIDE in methods, (
        f"target {session_id} never received {_UA_OVERRIDE}: {methods}"
    )
    assert _RESUME in methods, (
        f"target {session_id} was never resumed: {methods}"
    )
    resume_idx = methods.index(_RESUME)
    # The UA override must be applied *before* the target is resumed (Req 14.1).
    assert methods.index(_UA_OVERRIDE) < resume_idx, (
        f"target {session_id} resumed before UA override: {methods}"
    )
    if target_type == "page":
        # A page target gets the full override incl. the pre-document script,
        # before resume (the property-1 ordering invariant for page targets).
        assert _ADD_SCRIPT in methods, (
            f"page target {session_id} never received {_ADD_SCRIPT}: {methods}"
        )
        assert methods.index(_ADD_SCRIPT) < resume_idx, (
            f"page target {session_id} resumed before platform script: {methods}"
        )
    else:
        # A non-page target rejected the Page domain, so the script never
        # landed — but the UA override and the resume still must have.
        assert _ADD_SCRIPT not in methods, (
            f"non-page target {session_id} unexpectedly scripted: {methods}"
        )
    # Resume happens exactly once and is the last override-related command.
    assert methods.count(_RESUME) == 1, (
        f"target {session_id} resumed {methods.count(_RESUME)} times: {methods}"
    )
    assert resume_idx == len(methods) - 1, (
        f"target {session_id} sent commands after resume: {methods}"
    )


@settings(max_examples=200, deadline=None)
@given(sets=_target_sets())
def test_every_target_fully_spoofed_before_resume(
    sets: tuple[
        list[tuple[str, dict[str, Any]]], list[tuple[str, dict[str, Any]]],
    ],
) -> None:
    """Every attached target is overridden and resumed, even if non-page.

    ``run_attach`` must complete WITHOUT raising even when non-``page`` targets
    reject the ``Page`` domain with ``-32601``. Every target still receives the
    UA/UA-CH override and is resumed; every real ``page`` target additionally
    gets the pre-document script before resume.
    """
    initial, late = sets
    session = _MockBrowserCdpSession(initial, late)
    spoofer = LunaPlatformSpoofer(_CONFIG)
    identity = build_spoof_identity(_CONFIG, derive_browser_version(_FakeEdgeCDP()))

    # Must not raise despite non-page targets rejecting the Page domain.
    asyncio.run(spoofer.run_attach(session, identity))

    types = {sid: str(info.get("type", "?")) for sid, info in [*initial, *late]}
    all_targets = [sid for sid, _ in initial] + [sid for sid, _ in late]

    # Browser-level auto-attach was enabled exactly once, on the browser
    # session itself (session_id is None), and before any per-target work.
    auto_attach_calls = [
        i for i, (sid, m) in enumerate(session.calls)
        if m == "Target.setAutoAttach"
    ]
    assert len(auto_attach_calls) == 1, session.calls
    assert session.calls[auto_attach_calls[0]][0] is None, (
        "Target.setAutoAttach must be sent at browser level (session_id=None)"
    )

    # Group every recorded command by the target session it addressed.
    per_target: dict[str, list[str]] = {sid: [] for sid in all_targets}
    for sid, method in session.calls:
        if sid in per_target:
            per_target[sid].append(method)

    # No target left unspoofed/paused: each got its override before resume.
    assert set(per_target) == set(all_targets)
    for sid in all_targets:
        _assert_target_fully_spoofed(sid, types[sid], per_target[sid])


@settings(max_examples=50, deadline=None)
@given(sets=_target_sets())
def test_override_payloads_carry_the_configured_windows_identity(
    sets: tuple[
        list[tuple[str, dict[str, Any]]], list[tuple[str, dict[str, Any]]],
    ],
) -> None:
    """The "full override" is the real Windows payload, not an empty send.

    Guards against a spoofer that calls the right methods with inert params:
    the UA override must carry ``platform=Windows`` + a UA string, and the
    injected script must set ``navigator.platform`` and the WebGL renderer.
    """
    identity = build_spoof_identity(_CONFIG, derive_browser_version(_FakeEdgeCDP()))

    ua_params = build_set_user_agent_override_params(_CONFIG, identity)
    assert ua_params["userAgent"] == identity.user_agent
    assert ua_params["userAgentMetadata"]["platform"] == "Windows"

    script = build_platform_override_script(_CONFIG)
    assert _CONFIG.spoof_navigator_platform in script
    assert _CONFIG.spoof_webgl_renderer in script
    assert "navigator" in script and "platform" in script


def test_background_page_rejecting_page_domain_does_not_abort_attach() -> None:
    """Regression: the exact observed on-device failure must now succeed.

    A real Steam Deck sign-in attached one ``page`` target (the Luna page,
    which the override reached) plus one Chrome ``background_page`` target. The
    ``background_page`` rejected ``Page.enable`` with ``-32601`` ("'Page.enable'
    wasn't found"); the spoofer turned that into a ``RuntimeError`` that aborted
    the whole ``run_attach`` and failed the entire sign-in. The fix tolerates
    the domain-not-found rejection: ``run_attach`` must complete without raising
    and BOTH targets must end up resumed (a paused ``background_page`` can hang
    the browser).
    """
    initial = [
        ("session-page", {"targetId": "t-page", "type": "page"}),
        ("session-bg", {"targetId": "t-bg", "type": "background_page"}),
    ]
    session = _MockBrowserCdpSession(initial, [])
    spoofer = LunaPlatformSpoofer(_CONFIG)
    identity = build_spoof_identity(_CONFIG, derive_browser_version(_FakeEdgeCDP()))

    # The whole point of the fix: this attach must NOT raise.
    asyncio.run(spoofer.run_attach(session, identity))

    per_target: dict[str, list[str]] = {"session-page": [], "session-bg": []}
    for sid, method in session.calls:
        if sid in per_target:
            per_target[sid].append(method)

    # The real page got the full override (UA + script) before resume.
    _assert_target_fully_spoofed("session-page", "page", per_target["session-page"])
    # The background_page got the UA override and — crucially — was resumed,
    # despite rejecting the Page domain.
    _assert_target_fully_spoofed(
        "session-bg", "background_page", per_target["session-bg"],
    )
    assert _RESUME in per_target["session-bg"], "background_page was left paused"
    assert _ADD_SCRIPT not in per_target["session-bg"]
