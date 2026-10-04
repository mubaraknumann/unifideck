"""support_bundle/scrub.py — Content redaction, layer B.

The deny list is the guarantee; this is the net. Nothing in today's
logs matches these patterns (verified on a live device: zero hits for
JWTs, bearer headers or OAuth codes), so this exists for the day a
store CLI or Chromium starts echoing a token into stderr.

Designed around one tension: over-redaction destroys the diagnostic
value we are collecting the logs for. So:

* the ``qs`` and ``kv`` rules keep the **key** and replace only the
  value, leaving ``?code=<REDACTED>&state=abc123`` — which still shows
  the OAuth leg completed and which state round-tripped;
* non-secret OAuth parameters (``state``, ``scope``, ``redirect_uri``,
  ``client_id``, ``nonce``, ``error``) are deliberately not matched.
  They are the entire diagnostic payload of a failed login;
* absolute paths, including ``/home/<user>/...``, are preserved
  verbatim. Path resolution is most of what these bundles are for, and
  a username is not a credential;
* the riskiest rule (``blob``) is opt-in and used by exactly one
  source: the Chromium stderr log.

One rule is here for privacy rather than for secrets. ``ipv4`` masks
public addresses, because Steam's console log records peer addresses
from multiplayer sessions — sometimes other people's — and a bundle is
headed for a public support channel. Loopback and RFC1918 ranges are
kept: ``127.0.0.1`` is the CDP endpoint and LAN addresses matter when
diagnosing network problems.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_REDACTED = "<REDACTED>"


def _mask_value(match: re.Match[str]) -> str:
    """Mask a ``key: value`` pair, preserving the value's quoting.

    Quote-awareness is not cosmetic. The JSON and JSONL profiles run
    these text rules over the *serialised* output as a backstop, so
    replacing a quoted value with a bare token produced structurally
    invalid JSON — a bundle whose state files no longer parse.
    """
    key, value = match.group(1), match.group(2)
    if value[:1] in ("'", '"'):
        quote = value[0]
        return f"{key}{quote}{_REDACTED}{quote}"
    return f"{key}{_REDACTED}"


# Ordered rule table. Order matters: ``jwt`` runs before ``blob`` so a
# token is labelled as a token rather than as an anonymous blob.
_Replacement = str | Callable[[re.Match[str]], str]
_RULES: tuple[tuple[str, re.Pattern[str], _Replacement], ...] = (
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
        "<jwt:REDACTED>",
    ),
    (
        "bearer",
        re.compile(r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
        r"\1 " + _REDACTED,
    ),
    (
        "qs",
        re.compile(
            r"([?&#](?:access_token|refresh_token|id_token|code|client_secret"
            r"|authorization_code|session_state|password|api_key|apikey"
            r"|exchange_code|signature|sig|device_code|user_code|otc)=)[^&\s\"'<>]+",
            re.I,
        ),
        r"\1" + _REDACTED,
    ),
    (
        "kv",
        re.compile(
            r"([\"']?[A-Za-z_.]*(?:access_token|refresh_token|id_token|secret"
            r"|password|passwd|api[_-]?key|credential|session[_-]?id"
            r"|auth[_-]?code|bearer|device[_-]?code|user[_-]?code)[\"']?\s*[:=]\s*)"
            r"(\"[^\"]*\"|'[^']*'|[^\s,;}&]+)",
            re.I,
        ),
        _mask_value,
    ),
    (
        "cookie",
        re.compile(r"^(\s*(?:set-)?cookie)\s*:\s*.*$", re.I | re.M),
        r"\1: " + _REDACTED,
    ),
    (
        # Requires an alphabetic TLD. A looser version matched
        # `<steam-id>@152.57.138.108` in Steam's networking log 75 times
        # on one capture - not an address, and it masked the harmless
        # half while leaving the IP in place.
        "email",
        re.compile(r"\b[\w.+-]{1,64}@((?:[\w-]+\.)+[A-Za-z]{2,})\b"),
        r"<user>@\1",
    ),
    (
        # Public IPv4 addresses. Steam's console log records peer
        # addresses from multiplayer sessions, which are sometimes other
        # people's, and a bundle is headed for a public channel.
        # Loopback and RFC1918 ranges are kept: 127.0.0.1 is the CDP
        # endpoint and LAN addresses matter for network diagnostics.
        "ipv4",
        re.compile(
            r"\b(?!127\.)(?!10\.)(?!192\.168\.)(?!172\.(?:1[6-9]|2\d|3[01])\.)"
            r"(?!0\.0\.0\.0)(\d{1,3})\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
        ),
        r"\1.x.x.x",
    ),
    (
        # Last-resort catch for an *unlabelled* opaque token in browser
        # stderr. Requires genuine high entropy: lower- and upper-case
        # letters AND a digit in a 40+ character run.
        #
        # The three lookaheads are the whole point. A length-only rule
        # redacted `maxDynamicUniformBuffersPerPipelineLayout` out of a
        # real Vulkan warning - six false positives and no true ones on
        # a live device. Requiring a digit spares camelCase identifiers;
        # requiring upper-case spares lower-case hex, so SHA sums and
        # GUIDs (both load-bearing diagnostics) survive too.
        "blob",
        re.compile(
            r"\b(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])"
            r"(?=[A-Za-z0-9_-]*\d)([A-Za-z0-9_-]{4})[A-Za-z0-9_-]{36,}\b",
        ),
        r"\1...<REDACTED>",
    ),
)

_STANDARD = ("jwt", "bearer", "qs", "kv", "cookie", "email", "ipv4")
# Rules that match on *shape* rather than on a keyword, so the cheap
# literal gate below cannot decide whether they apply. They run on
# every line.
_SHAPE_RULES = frozenset({"blob", "ipv4"})
_AGGRESSIVE = (*_STANDARD, "blob")

_PROFILE_RULES: dict[str, tuple[str, ...]] = {
    "text": _STANDARD,
    "text_aggressive": _AGGRESSIVE,
    "json": _STANDARD,
    "jsonl": _STANDARD,
}

# Lines dropped wholesale under the aggressive profile. Substituting
# inside a URL that Chromium wrapped across lines could leave a usable
# fragment behind, so the safe move is to drop the line and say so.
_DROP_LINE = re.compile(
    r"(?i)(access_token|refresh_token|id_token|[?&]code=|client_secret"
    r"|authorization:|set-cookie)",
)
_DROP_MARKER = "<line dropped by Capture Logs: auth-URL prefilter>"


def profile_rules() -> dict[str, list[str]]:
    """Return the rule names per profile, for the manifest."""
    return {name: list(rules) for name, rules in _PROFILE_RULES.items()}


# Cheap literal gate. A line with none of these substrings cannot match
# any rule, so it can skip the whole battery. This is a pure
# optimisation and must stay a strict superset of what the rules match:
# every rule's trigger words appear here, plus "@" for the email rule
# and "eyJ" for the base64 JWT header.
_TRIGGER = re.compile(
    r"(?i)token|secret|password|passwd|cookie|bearer|basic |credential"
    r"|api[_-]?key|apikey|auth|session|signature|sig=|eyJ|code=|@|key"
    r"|device[_-]?code|user[_-]?code|otc=",
)


def _apply_rules(text: str, names: tuple[str, ...]) -> tuple[str, int]:
    """Run the named rules over ``text``, returning the hit count.

    Gated line-by-line: running six regexes with lookaheads over every
    byte cost 3.6 seconds per 4 MB, which made collecting Steam's logs
    a ~19-second button press. Almost no log line contains anything
    resembling a credential, so a single cheap literal scan decides
    which lines are worth the expensive passes.

    The shape-based rules (blob, ipv4) are the exception - they match
    on form rather than on any keyword, so they run on every line.
    """
    rules = [item for item in _RULES if item[0] in names]
    if not rules:
        return text, 0
    unconditional = [item for item in rules if item[0] in _SHAPE_RULES]
    gated = [item for item in rules if item[0] not in _SHAPE_RULES]
    total = 0
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        candidate = line
        if gated and _TRIGGER.search(candidate):
            for _name, pattern, replacement in gated:
                candidate, hits = pattern.subn(replacement, candidate)
                total += hits
        for _name, pattern, replacement in unconditional:
            candidate, hits = pattern.subn(replacement, candidate)
            total += hits
        out.append(candidate)
    return "".join(out), total


def _prefilter(text: str) -> tuple[str, int]:
    """Drop whole lines carrying auth markers (aggressive profile)."""
    kept: list[str] = []
    dropped = 0
    for line in text.splitlines(keepends=True):
        if _DROP_LINE.search(line):
            kept.append(_DROP_MARKER + "\n")
            dropped += 1
        else:
            kept.append(line)
    return "".join(kept), dropped


def scrub_text(data: bytes, profile: str) -> tuple[bytes, int, int]:
    """Scrub a text stream. Returns ``(bytes, redactions, dropped)``.

    Decodes with ``errors="replace"`` rather than ``surrogateescape``
    so binary noise becomes visible replacement characters instead of
    round-tripping back into the archive.
    """
    text = data.decode("utf-8", errors="replace")
    dropped = 0
    if profile == "text_aggressive":
        text, dropped = _prefilter(text)
    text, hits = _apply_rules(text, _PROFILE_RULES.get(profile, _STANDARD))
    return text.encode("utf-8"), hits, dropped


def _is_sensitive_key(key: Any) -> bool:
    """True when a key name marks its value as a credential."""
    from unifideck.security.redaction import _SENSITIVE_KEY_PATTERNS

    try:
        normalised = str(key).lower()
    except Exception:
        return True
    return any(pattern in normalised for pattern in _SENSITIVE_KEY_PATTERNS)


def _redact_obj(obj: Any) -> Any:
    """Key-based redaction over a parsed JSON document.

    Shares the sensitive-key pattern list with the audit redactor so
    the two cannot drift on what counts as a credential, but
    deliberately does **not** reuse ``redact_for_audit`` itself.

    That helper truncates any string over 256 characters, which is
    correct for bounded audit entries and destructive here: on a real
    capture it cut an install's ``error_message`` mid-sentence, losing
    "Not enough available disk space! 43.45 GiB < 66.38 GiB" - the
    single most useful line in the file. A diagnostics bundle keeps
    long values.

    Recurses into lists as well as dicts, which the shared helper does
    not, so a token nested inside an array is still caught.
    """
    if isinstance(obj, dict):
        return {
            key: _REDACTED if _is_sensitive_key(key) else _redact_obj(value)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_obj(item) for item in obj]
    return obj


def scrub_json(data: bytes) -> tuple[bytes, int, int]:
    """Scrub a whole JSON document, falling back to text rules.

    The serialised result is passed through the text rules as a
    backstop, which is what actually closes the nested-list hole for
    any shape the key-based redactor cannot reach.
    """
    try:
        parsed = json.loads(data.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return scrub_text(data, "text")
    rendered = json.dumps(_redact_obj(parsed), indent=2, sort_keys=True)
    scrubbed, hits, _ = scrub_text(rendered.encode("utf-8"), "text")
    return scrubbed, hits, 0


def scrub_jsonl(data: bytes) -> tuple[bytes, int, int]:
    """Scrub a JSONL stream line by line.

    A line that fails to parse is scrubbed as text rather than dropped
    — a corrupt line is itself a diagnostic, and these files are
    written by an append-only bridge that can be interrupted.
    """
    out: list[str] = []
    total = 0
    for line in data.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        rendered, hits = _scrub_jsonl_line(line)
        out.append(rendered)
        total += hits
    return ("\n".join(out) + "\n").encode("utf-8"), total, 0


def _scrub_jsonl_line(line: str) -> tuple[str, int]:
    """Scrub one JSONL line, tolerating unparseable input."""
    try:
        parsed = json.loads(line)
    except ValueError:
        text, hits = _apply_rules(line, _STANDARD)
        return text, hits
    rendered = json.dumps(_redact_obj(parsed), sort_keys=True)
    return _apply_rules(rendered, _STANDARD)


def apply_profile(data: bytes, profile: str) -> tuple[bytes, int, int]:
    """Dispatch to the right scrubber. Never raises."""
    try:
        if profile == "none":
            return data, 0, 0
        if profile == "json":
            return scrub_json(data)
        if profile == "jsonl":
            return scrub_jsonl(data)
        return scrub_text(data, profile)
    except Exception:
        logger.exception("[support_bundle] scrub failed, dropping content")
        return b"<content omitted: redaction failed>\n", 0, 0
