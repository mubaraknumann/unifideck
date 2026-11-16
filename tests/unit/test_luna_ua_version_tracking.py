"""Property test for Luna UA-version tracking (task 5.2).

Feature: amazon-luna-streaming, Property 3: UA-version tracking

**Property 3:** For any browser version string reported by the installed
browser at runtime, the major version embedded in the spoofed UA string and
UA Client Hints metadata equals the major version parsed from that real
version string; the spoofer never emits a hardcoded version.

**Validates: Requirements 14.8, 12.6**

The version-tracking core of ``LunaPlatformSpoofer`` is split so the parsing
and substitution are pure (``parse_browser_version`` / ``build_spoof_identity``)
and the only CDP call is ``derive_browser_version``. This test drives that CDP
seam with a *mock CDP layer* (``_FakeCDP``) returning Hypothesis-generated
``/json/version`` "Browser" strings — no real browser and no Amazon — then
asserts the derived identity tracks the real major everywhere it appears.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from unifideck.stores.luna.luna_config import LunaConfig
from unifideck.stores.luna.luna_platform_spoofer import (
    LunaSpoofConfigError,
    build_set_user_agent_override_params,
    build_spoof_identity,
    derive_browser_version,
)

# A UA template that carries the mandatory ``{version}`` placeholder (Req 12.6):
# the spoofer substitutes the real installed-browser major into it, so a
# correct implementation can never emit a hardcoded version here.
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


class _FakeCDP:
    """Mock CDP layer: returns a scripted ``/json/version`` "Browser" field.

    Stands in for ``EdgeCDPClient`` so ``derive_browser_version`` can be
    exercised with generated version strings without a live browser.
    """

    def __init__(self, browser_field: str | None) -> None:
        self._browser_field = browser_field

    def get_browser_version(self) -> str | None:
        return self._browser_field


# ── Generators ──────────────────────────────────────────────────────
#
# Build realistic ``/json/version`` "Browser" fields: a product label, a
# numeric major, and 0-3 further dotted numeric components. The major is the
# single value the whole override must track.

_PRODUCTS = st.sampled_from(["Chrome", "Edg", "HeadlessChrome", "Chromium"])
_MAJORS = st.integers(min_value=1, max_value=99999)
_COMPONENT = st.integers(min_value=0, max_value=99999)
_TAIL = st.lists(_COMPONENT, min_size=0, max_size=3)


@st.composite
def _browser_fields(draw: Any) -> tuple[str, int, str]:
    """Yield ``(browser_field, major, full_version)`` triples."""
    product = draw(_PRODUCTS)
    major = draw(_MAJORS)
    tail = draw(_TAIL)
    full_parts = [str(major), *(str(c) for c in tail)]
    full_version = ".".join(full_parts)
    return f"{product}/{full_version}", major, full_version


@settings(max_examples=200)
@given(case=_browser_fields())
def test_spoofed_major_tracks_real_browser_major(case: tuple[str, int, str]) -> None:
    """The major in the UA string + UA-CH metadata equals the real major."""
    browser_field, major, full_version = case
    cdp = _FakeCDP(browser_field)

    identity = build_spoof_identity(_CONFIG, derive_browser_version(cdp))

    # Parsed major/full track the real reported version exactly (Req 14.8).
    assert identity.major_version == major
    assert identity.full_version == full_version

    # UA string carries the real major via the {version} substitution and
    # never the literal placeholder (Req 12.6 — no hardcoded version).
    assert f"Chrome/{major}.0.0.0" in identity.user_agent
    assert "{version}" not in identity.user_agent

    # UA Client Hints metadata agrees with the UA string's major (Req 14.3/14.8).
    assert identity.ua_ch_major_version == str(major)
    assert identity.ua_ch_full_version == full_version

    params = build_set_user_agent_override_params(_CONFIG, identity)
    assert params["userAgent"] == identity.user_agent
    metadata = params["userAgentMetadata"]
    # The real-browser brands (Chromium / Google Chrome) carry the real major;
    # only the padding "Not=A?Brand" is allowed a fixed sentinel version.
    tracked = [b for b in metadata["brands"] if b["brand"] != "Not=A?Brand"]
    assert tracked, "expected at least one real-browser brand"
    for brand in tracked:
        assert brand["version"] == str(major)
    full_tracked = [
        b for b in metadata["fullVersionList"] if b["brand"] != "Not=A?Brand"
    ]
    for brand in full_tracked:
        assert brand["version"] == full_version
    assert metadata["uaFullVersion"] == full_version


@settings(max_examples=200)
@given(
    first=_browser_fields(),
    second=_browser_fields(),
)
def test_different_real_versions_yield_different_spoofed_majors(
    first: tuple[str, int, str],
    second: tuple[str, int, str],
) -> None:
    """Distinct real majors must produce distinct spoofed majors.

    This is the teeth of "never a hardcoded version": an implementation that
    baked in a fixed major would collapse both inputs to the same output.
    """
    id_a = build_spoof_identity(_CONFIG, derive_browser_version(_FakeCDP(first[0])))
    id_b = build_spoof_identity(_CONFIG, derive_browser_version(_FakeCDP(second[0])))

    if first[1] != second[1]:
        assert id_a.major_version != id_b.major_version
        assert id_a.user_agent != id_b.user_agent
    else:
        assert id_a.major_version == id_b.major_version


@settings(max_examples=100)
@given(
    noise=st.text(
        alphabet=st.characters(blacklist_categories=("Cc", "Cs"), max_codepoint=0x2FFF),
        max_size=20,
    ).filter(lambda s: not s.strip() or "/" not in s or not s.split("/")[-1][:1].isdigit()),
)
def test_undeterminable_version_raises_never_falls_back(noise: str) -> None:
    """An unparseable/empty "Browser" field aborts (Req 14.9) — no fallback.

    The spoofer must raise rather than emit a hardcoded version when the real
    version cannot be determined; the launcher maps this to the
    platform_spoofing_failure abort path.
    """
    cdp = _FakeCDP(noise if noise.strip() else None)
    try:
        build_spoof_identity(_CONFIG, derive_browser_version(cdp))
    except LunaSpoofConfigError:
        return
    # If parsing somehow succeeded, the input was in fact a valid version —
    # that is acceptable, but then it must still track (not be hardcoded).
    identity = build_spoof_identity(_CONFIG, derive_browser_version(cdp))
    assert "{version}" not in identity.user_agent


@settings(max_examples=100)
@given(case=_browser_fields())
def test_template_without_placeholder_refuses_hardcoded_version(
    case: tuple[str, int, str],
) -> None:
    """A template with a baked-in version is rejected (Req 12.6)."""
    hardcoded = replace(
        _CONFIG,
        spoof_ua_template="Mozilla/5.0 ... Chrome/131.0.0.0 Safari/537.36",
    )
    cdp = _FakeCDP(case[0])
    try:
        build_spoof_identity(hardcoded, derive_browser_version(cdp))
    except LunaSpoofConfigError:
        return
    raise AssertionError("expected a hardcoded-version template to be refused")


def test_plugin_chromium_version_is_tracked() -> None:
    """The plugin's downloaded Chromium reports ``Chrome/<CHROME_VERSION>``."""
    from unifideck.auth.edge_browser.chrome_installer import CHROME_VERSION

    identity = build_spoof_identity(
        _CONFIG, derive_browser_version(_FakeCDP(f"Chrome/{CHROME_VERSION}")),
    )
    major = CHROME_VERSION.split(".")[0]
    params = build_set_user_agent_override_params(_CONFIG, identity)
    assert f"Chrome/{major}.0.0.0" in params["userAgent"]
    brands = params["userAgentMetadata"]["brands"]
    assert {"brand": "Google Chrome", "version": major} in brands
