"""Pure spoof-payload construction for the Luna platform spoofer.

The spoofed OS identity (UA string + UA Client Hints metadata +
``navigator.platform`` + WebGL renderer) is built here from the resolved
:class:`~unifideck.stores.luna.luna_config.LunaConfig` and the runtime-derived
:class:`SpoofIdentity`. Everything in this module is pure and deterministic —
no browser, no I/O — so the version parsing, the override payloads and the
platform-probe extraction can be unit/property tested without a live browser
(Req 14.3-14.9, 12.6). The one impure helper, :func:`derive_browser_version`,
only reads ``/json/version`` through the already-abstracted ``EdgeCDPClient``
and hands the raw string to the pure :func:`build_spoof_identity`.

The browser-level auto-attach loop and the CDP websocket live in the
``spoofer`` and ``session`` sibling modules; this module carries no attach
state of its own.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
    from unifideck.stores.luna.luna_config import LunaConfig

#: Placeholder in ``LunaConfig.spoof_ua_template`` substituted at runtime
#: with the real installed-browser major version (Req 12.6, 14.8).
_VERSION_PLACEHOLDER = "{version}"

#: A ``/json/version`` "Browser" field is ``<Product>/<major>.<minor>...``;
#: capture the product label and the leading numeric major version. Covers
#: ``Chrome/150.0.7871.186``, ``Edg/128.0.0.0``, ``HeadlessChrome/150.x``.
_BROWSER_FIELD_RE = re.compile(r"^(?P<product>[^/\s]+)/(?P<major>\d+)(?:\.\d+)*")


#: The CDP JSON-RPC error code returned when a method/domain does not exist
#: on a target (``-32601``, "method not found"). The ``Page`` domain is absent
#: on ``background_page``/``service_worker``/``worker``/``other`` targets, so
#: ``Page.enable`` on them fails with this code; the spoofer tolerates it and
#: applies the UA override only (the whole-session-aborting bug otherwise).
_CDP_METHOD_NOT_FOUND = -32601

#: Idle window for the auto-attach event loop (``attached_targets``).
#: ``Target.setAutoAttach`` delivers one ``attachedToTarget`` burst for the
#: open targets, then the browser websocket goes silent. The loop ends after
#: this long without a new target so ``run_attach`` returns instead of
#: blocking forever (an on-device hang). Comfortably longer than the
#: sub-second gap between a burst's events, short enough to keep the whole
#: attach quick.
_ATTACH_IDLE_TIMEOUT_S = 2.0


def _is_domain_not_found(exc: Exception) -> bool:
    """Whether ``exc`` is a CDP "domain/method not found" error (``-32601``).

    :meth:`_AiohttpBrowserCdpSession.send` wraps a CDP error payload into a
    :class:`RuntimeError` whose text embeds the ``repr`` of the error dict
    (``{'code': -32601, 'message': "'Page.enable' wasn't found"}``). Match on
    the ``-32601`` code or the characteristic "wasn't found" message so the
    guard is robust to the exact string form, and does **not** match unrelated
    errors (only this specific not-found case is tolerated).
    """
    text = str(exc)
    return str(_CDP_METHOD_NOT_FOUND) in text or "wasn't found" in text


class LunaSpoofConfigError(Exception):
    """A spoofing-configuration failure that names the undetermined version.

    Raised when the real installed browser version cannot be derived or
    parsed (Req 14.9). The launcher maps this to the
    ``platform_spoofing_failure`` abort path (tasks 5.3 / 7.4): the Luna
    launch must not proceed with a hardcoded or missing version.

    ``reported`` carries the raw ``/json/version`` "Browser" value that
    could not be interpreted (or an empty string when nothing was
    reported) so the aborting log can name it.
    """

    def __init__(self, reported: str, message: str) -> None:
        """Build the error carrying the raw reported (undetermined) value."""
        super().__init__(message)
        self.reported = reported


@dataclass(frozen=True)
class SpoofIdentity:
    """The runtime-derived identity substituted into every target override.

    All version fields track the real installed browser (Req 14.8); none
    is hardcoded. ``major_version`` is the integer major parsed from the
    real browser; ``full_version`` is the full dotted version string as
    reported. ``user_agent`` is ``spoof_ua_template`` with its
    ``{version}`` placeholder substituted. ``ua_ch_full_version`` and
    ``ua_ch_major_version`` are the UA Client Hints metadata version
    fields (``fullVersionList`` entries / ``uaFullVersion`` and the brand
    ``version`` respectively).
    """

    major_version: int
    full_version: str
    user_agent: str
    ua_ch_major_version: str
    ua_ch_full_version: str


def parse_browser_version(browser_field: str | None) -> tuple[int, str]:
    """Parse a ``/json/version`` "Browser" field into (major, full_version).

    ``browser_field`` looks like ``"Chrome/150.0.7871.186"`` or
    ``"Edg/128.0.0.0"``. Returns the integer major version and the full
    dotted version string (``"150.0.7871.186"``).

    Pure and deterministic — no I/O — so it can be exercised without a
    live browser (property test 5.2). Raises :class:`LunaSpoofConfigError`
    naming the offending value when the field is empty or malformed
    (Req 14.9), so the launcher never falls back to a hardcoded version.
    """
    raw = (browser_field or "").strip()
    if not raw:
        raise LunaSpoofConfigError(
            "",
            "cannot determine installed browser version: /json/version "
            "reported no 'Browser' field",
        )
    match = _BROWSER_FIELD_RE.match(raw)
    if not match:
        raise LunaSpoofConfigError(
            raw,
            "cannot determine installed browser version: unrecognised "
            f"'Browser' value {raw!r}",
        )
    full_version = raw[match.start("major"):]
    return int(match.group("major")), full_version


def build_spoof_identity(config: LunaConfig, browser_field: str | None) -> SpoofIdentity:
    """Build the :class:`SpoofIdentity` from the config template and real version.

    Substitutes the real installed-browser major version into
    ``config.spoof_ua_template`` (its ``{version}`` placeholder) and
    derives the UA Client Hints version fields from the same real version
    (Req 14.8). Pure and deterministic given its inputs.

    Raises :class:`LunaSpoofConfigError` when the version cannot be parsed
    (via :func:`parse_browser_version`) or when the template carries no
    ``{version}`` placeholder — a template with a baked-in version would
    emit a hardcoded version, which Req 12.6/14.8 forbid.
    """
    major, full_version = parse_browser_version(browser_field)
    template = config.spoof_ua_template
    if _VERSION_PLACEHOLDER not in template:
        raise LunaSpoofConfigError(
            full_version,
            "spoof_ua_template lacks the "
            f"{_VERSION_PLACEHOLDER!r} placeholder; refusing to emit a "
            "hardcoded browser version",
        )
    user_agent = template.replace(_VERSION_PLACEHOLDER, str(major))
    return SpoofIdentity(
        major_version=major,
        full_version=full_version,
        user_agent=user_agent,
        ua_ch_major_version=str(major),
        ua_ch_full_version=full_version,
    )


def derive_browser_version(cdp: EdgeCDPClient) -> str:
    """Fetch the real installed-browser version string over CDP.

    Reuses :meth:`EdgeCDPClient.get_browser_version` (the ``/json/version``
    "Browser" field) rather than hand-rolling an HTTP call. This is the
    only part of version tracking that talks to a live browser; the
    returned string is handed to the pure :func:`build_spoof_identity`.

    Raises :class:`LunaSpoofConfigError` when the browser does not report a
    version so the launcher can abort (Req 14.9).
    """
    browser_field = cdp.get_browser_version()
    if not browser_field:
        raise LunaSpoofConfigError(
            "",
            "cannot determine installed browser version: CDP "
            "/json/version did not report a 'Browser' field",
        )
    return browser_field


# ── Pure override-payload builders (no I/O; unit/property testable) ──
#
# These construct the exact CDP parameters applied to every attached
# target. They take only the resolved config + runtime identity so they
# are deterministic and can be exercised without a browser (property 1's
# "full override" is these two payloads).

#: WebGL ``getParameter`` enum for the unmasked renderer string. The
#: ``WEBGL_debug_renderer_info`` extension exposes the real GPU name under
#: this numeric constant; Luna reads it to sniff a Linux GPU, so the
#: injected getter must answer the configured Windows renderer for it
#: (Req 14.6, 14.7).
_UNMASKED_RENDERER_WEBGL = 37446

#: The platform version reported in the UA Client Hints metadata. Luna
#: only gates on ``platform == "Windows"`` (Req 14.4); a plausible Windows
#: platform version keeps the metadata internally consistent.
_WINDOWS_PLATFORM_VERSION = "10.0.0"


def build_user_agent_metadata(
    config: LunaConfig, identity: SpoofIdentity,
) -> dict[str, Any]:
    """Build the ``userAgentMetadata`` for ``Emulation.setUserAgentOverride``.

    The metadata carries ``platform`` (the configured Windows value), the
    brand/version list derived from the real installed-browser major
    version (so UA Client Hints agree with the UA string — Req 14.3, 14.8),
    and a Windows ``platformVersion``. Pure given its inputs.

    This is the layer that defeats
    ``navigator.userAgentData.getHighEntropyValues(['platform'])`` (F3) —
    the UA string alone is not enough.
    """
    major = identity.ua_ch_major_version
    full = identity.ua_ch_full_version
    brands = [
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
        {"brand": "Not=A?Brand", "version": "99"},
    ]
    full_version_list = [
        {"brand": "Chromium", "version": full},
        {"brand": "Google Chrome", "version": full},
        {"brand": "Not=A?Brand", "version": "99.0.0.0"},
    ]
    return {
        "platform": config.spoof_uach_platform,
        "platformVersion": _WINDOWS_PLATFORM_VERSION,
        "architecture": "x86",
        "bitness": "64",
        "model": "",
        "mobile": False,
        "brands": brands,
        "fullVersionList": full_version_list,
        "uaFullVersion": full,
    }


def build_set_user_agent_override_params(
    config: LunaConfig, identity: SpoofIdentity,
) -> dict[str, Any]:
    """Build the ``Emulation.setUserAgentOverride`` params for a target.

    Carries both the spoofed UA string (version-tracked, Req 14.3/14.8) and
    the full ``userAgentMetadata`` (platform=Windows, brand/version list,
    platformVersion — Req 14.4). Pure given its inputs.
    """
    return {
        "userAgent": identity.user_agent,
        "userAgentMetadata": build_user_agent_metadata(config, identity),
    }


def build_platform_override_script(config: LunaConfig) -> str:
    """Build the JS injected before any page script via ``addScriptToEvaluateOnNewDocument``.

    Overrides ``navigator.platform`` to the configured Windows string
    (Req 14.5) and the WebGL ``UNMASKED_RENDERER_WEBGL`` (37446) getter to
    the configured Windows renderer (Req 14.6, 14.7), so a page cannot read
    back a Linux platform or GPU. Runs before any page script because it is
    installed via ``Page.addScriptToEvaluateOnNewDocument`` (and evaluated
    once for the already-loaded document).

    Pure given the config; the returned source embeds the config values via
    ``json.dumps`` so arbitrary renderer/platform strings are safely quoted.
    """
    navigator_platform = json.dumps(config.spoof_navigator_platform)
    webgl_renderer = json.dumps(config.spoof_webgl_renderer)
    return f"""
(() => {{
  try {{
    Object.defineProperty(navigator, 'platform', {{
      get: () => {navigator_platform},
      configurable: true,
    }});
  }} catch (e) {{}}
  const UNMASKED_RENDERER_WEBGL = {_UNMASKED_RENDERER_WEBGL};
  const spoofRenderer = {webgl_renderer};
  const patch = (proto) => {{
    if (!proto || !proto.getParameter) return;
    const original = proto.getParameter;
    proto.getParameter = function (parameter) {{
      if (parameter === UNMASKED_RENDERER_WEBGL) {{
        return spoofRenderer;
      }}
      return original.apply(this, arguments);
    }};
  }};
  try {{ patch(window.WebGLRenderingContext && window.WebGLRenderingContext.prototype); }} catch (e) {{}}
  try {{ patch(window.WebGL2RenderingContext && window.WebGL2RenderingContext.prototype); }} catch (e) {{}}
}})();
""".strip()


#: The JS expression evaluated by :meth:`LunaPlatformSpoofer.verify` after
#: the detail page loads and before the play-click (Req 14.11). It is the
#: exact signal Luna reads to gate Linux (F3): the high-entropy
#: ``platform`` from UA Client Hints, not the UA string. ``getHighEntropyValues``
#: returns a Promise, so the ``Runtime.evaluate`` call must set
#: ``awaitPromise`` and ``returnByValue``.
_VERIFY_PLATFORM_EXPRESSION = (
    "navigator.userAgentData"
    ".getHighEntropyValues(['platform'])"
    ".then(v => v.platform)"
)


def extract_platform_from_evaluate(result: dict[str, Any]) -> str:
    """Extract the reported platform from a ``Runtime.evaluate`` result.

    ``result`` is the payload returned by evaluating
    :data:`_VERIFY_PLATFORM_EXPRESSION` with ``awaitPromise`` and
    ``returnByValue``; its shape is
    ``{"result": {"type": "string", "value": "Windows"}}``. A thrown
    promise / evaluation error carries ``exceptionDetails`` instead.

    Pure and deterministic — no I/O — so the nested-shape handling is
    unit-testable without a browser. Returns the platform string (e.g.
    ``"Windows"`` on a spoofed session, ``"Linux"`` when the override did
    not take). Raises :class:`LunaSpoofConfigError` when the evaluation
    threw, reported no value, or returned a non-string, so the launch flow
    treats an unreadable probe as a spoofing failure rather than silently
    passing.
    """
    exception = result.get("exceptionDetails")
    if exception:
        raise LunaSpoofConfigError(
            "",
            "platform verification failed: getHighEntropyValues raised "
            f"{exception!r}",
        )
    inner = result.get("result")
    if not isinstance(inner, dict) or "value" not in inner:
        raise LunaSpoofConfigError(
            "",
            "platform verification failed: getHighEntropyValues returned "
            f"no value (result {result!r})",
        )
    value = inner.get("value")
    if not isinstance(value, str):
        raise LunaSpoofConfigError(
            "" if value is None else str(value),
            "platform verification failed: getHighEntropyValues returned a "
            f"non-string platform {value!r}",
        )
    return value
