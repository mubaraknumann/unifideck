"""Tests for the Capture Logs content scrubber.

Two halves, and the second matters as much as the first:

1. credentials get masked;
2. **the diagnostic content survives.** Over-redaction would quietly
   destroy the value of the very logs we are collecting, so the
   preservation tests are not decoration — they are the constraint that
   keeps the rules honest.
"""
from __future__ import annotations

import json

import pytest

from unifideck.services.support_bundle import scrub

_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    ".dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
)


def _text(raw: str, profile: str = "text") -> str:
    """Scrub a string and hand back the result as a string."""
    out, _hits, _dropped = scrub.apply_profile(raw.encode("utf-8"), profile)
    return out.decode("utf-8")


# ── masking ───────────────────────────────────────────────────────
def test_jwt_is_masked() -> None:
    result = _text(f"auth failed with token {_JWT} for user")
    assert _JWT not in result
    assert "<jwt:REDACTED>" in result
    assert "auth failed" in result


def test_bearer_header_keeps_its_scheme() -> None:
    result = _text("Authorization: Bearer abcdef1234567890xyz")
    assert "abcdef1234567890xyz" not in result
    assert "Bearer" in result, "the scheme is diagnostic, the value is not"


def test_oauth_query_parameters_keep_their_keys() -> None:
    """The key names are the diagnostic; only values are secret."""
    result = _text(
        "redirect https://host/cb?code=SECRETVALUE123&state=abc123"
        "&scope=basic&client_id=public-app",
    )
    assert "SECRETVALUE123" not in result
    assert "code=<REDACTED>" in result
    # These are not secrets and they are the whole payload of a failed
    # login, so they must survive untouched.
    assert "state=abc123" in result
    assert "scope=basic" in result
    assert "client_id=public-app" in result


def test_device_sign_in_codes_are_masked() -> None:
    """The device-code sign-in puts a live code in a URL and in JSON bodies."""
    result = _text(
        "open https://www.microsoft.com/link?otc=9W2495MG\n"
        '{"device_code": "DAQABAAEAAAD", "user_code": "9W2495MG"}\n',
    )
    assert "9W2495MG" not in result
    assert "DAQABAAEAAAD" not in result
    assert "otc=<REDACTED>" in result


def test_key_value_assignments_are_masked() -> None:
    result = _text(
        'settings {"access_token": "abc123secret", "refresh_token": "r-9988"}\n'
        "password=hunter2\n",
    )
    assert "abc123secret" not in result
    assert "r-9988" not in result
    assert "hunter2" not in result
    assert "access_token" in result


def test_cookie_headers_are_masked() -> None:
    result = _text("Set-Cookie: session=abcdef; Path=/")
    assert "abcdef" not in result
    assert "Set-Cookie" in result


def test_email_local_part_is_masked_domain_kept() -> None:
    result = _text("signed in as player.one+tag@example.com ok")
    assert "player.one+tag" not in result
    assert "<user>@example.com" in result


def test_email_rule_does_not_fire_on_an_ip_address() -> None:
    """Regression: `<id>@<ip>` matched 75 times in one real capture.

    Steam's networking log writes ``<steam-id>@<peer-ip>:<port>``. The
    old pattern accepted a numeric "TLD", so it masked the harmless
    identity and left the address in place - backwards on both counts.
    """
    result = _text("Connected 76561198@152.57.138.108:36618 Ping: 1048ms")
    assert "<user>@" not in result
    assert "76561198@" in result


def test_public_ip_addresses_are_masked() -> None:
    """A bundle goes to a public channel; peer IPs are not ours to share."""
    result = _text("Connected 76561198@152.57.138.108:36618 qual 67")
    assert "152.57.138.108" not in result
    assert "152.x.x.x" in result
    # Port and identity survive - the connection is still traceable.
    assert ":36618" in result


@pytest.mark.parametrize(
    "keep",
    [
        # The CDP endpoint. Masking it would break browser diagnostics.
        "DevTools listening on ws://127.0.0.1:9222/devtools/browser/x",
        "bound to 192.168.1.42:8080",
        "route via 10.0.0.1 metric 100",
        "gateway 172.16.0.1 reachable",
        "listening on 0.0.0.0:27036",
    ],
)
def test_loopback_and_private_addresses_survive(keep: str) -> None:
    assert _text(keep) == keep


@pytest.mark.parametrize(
    "version",
    [
        "SteamOS 3.6.20 build 20240701",
        "using Proton 10.0 (Beta)",
        "GE-Proton9-27 selected",
    ],
)
def test_version_strings_are_not_mistaken_for_addresses(version: str) -> None:
    assert _text(version) == version


# ── preservation ──────────────────────────────────────────────────
def test_diagnostic_content_survives_the_standard_profile() -> None:
    """The whole point of collecting logs must not be scrubbed away."""
    original = (
        'Traceback (most recent call last):\n'
        '  File "/home/deck/homebrew/plugins/Unifideck/main.py", line 42\n'
        "WINEPREFIX=/home/deck/.local/share/unifideck/prefixes/1234567890\n"
        "using /home/deck/.local/share/Steam/steamapps/common/"
        "Proton - Experimental/proton\n"
        "AppID 2147483647 launch rc=74 umu-run exited\n"
        "prefix uuid 3f2504e0-4f89-11d3-9a0c-0305e82c3301\n"
    )
    assert _text(original) == original


def test_home_paths_are_never_rewritten() -> None:
    """Path resolution is most of what these bundles are for."""
    original = "/home/somebody/.local/share/unifideck/launches/abc.log\n"
    assert _text(original) == original


def test_aggressive_profile_is_opt_in_only() -> None:
    """The blob rule is riskier than the rest, so it is scoped.

    Only the browser stderr log uses it; the standard profile must
    leave even a high-entropy string alone.
    """
    # Synthetic high-entropy string, not a credential — the point of the
    # fixture is that it LOOKS like one. gitleaks:allow keeps the secret
    # scanner from flagging the scrubber's own test data.
    token = "dBjftJeZ4CVPmB92K27uhbUJU1p1rwW1gFWFOEjXk9Qz2"  # gitleaks:allow
    assert token in _text(f"selected {token}", "text")
    assert token not in _text(f"selected {token}", "text_aggressive")


@pytest.mark.parametrize(
    "line",
    [
        # Real Vulkan warning from a live device: 41 chars, camelCase.
        "Warning: maxDynamicUniformBuffersPerPipelineLayout reduced to 16",
        # Lower-case hex - SHA sums identify builds and are diagnostic.
        "commit a94a8fe5ccb19ba61c4c0873d391e987982fbbd3 built ok",
        "prefix uuid 3f2504e0-4f89-11d3-9a0c-0305e82c3301 created",
        "flag enable_experimental_shader_disk_cache_for_vulkan_backend on",
    ],
)
def test_blob_rule_spares_long_identifiers_and_hashes(line: str) -> None:
    """Regression: the blob rule used to be length-only.

    That version redacted `maxDynamicUniformBuffersPerPipelineLayout`
    out of a Mesa warning - six false positives and zero true ones on a
    real capture. It now demands mixed case *and* a digit, which is
    what separates an opaque token from an identifier or a hex hash.
    """
    assert _text(line, "text_aggressive") == line


def test_blob_rule_still_catches_an_unlabelled_token() -> None:
    """Tightening it must not make it useless."""
    token = "dBjftJeZ4CVPmB92K27uhbUJU1p1rwW1gFWFOEjXk9Qz2"  # gitleaks:allow
    result = _text(f"session {token} issued", "text_aggressive")
    assert token not in result
    assert "<REDACTED>" in result


def test_aggressive_profile_drops_whole_auth_lines() -> None:
    """A line-wrapped redirect URL could leave a usable fragment.

    So the aggressive profile replaces the entire line rather than
    substituting inside it.
    """
    result = _text(
        "[1:2:INFO] navigation to https://login/cb?code=abc123def456 failed\n"
        "[1:2:INFO] DevTools listening on ws://127.0.0.1:9222/devtools/x\n",
        "text_aggressive",
    )
    assert "abc123def456" not in result
    assert "line dropped" in result
    # The loopback CDP line is a real diagnostic and is kept.
    assert "DevTools listening" in result


# ── structured formats ────────────────────────────────────────────
def test_json_token_nested_in_a_list_is_masked() -> None:
    """Closes the gap in the shared key-based redactor.

    ``redact_for_audit`` recurses into dicts but not lists, so a token
    inside an array would otherwise pass straight through.
    """
    payload = json.dumps({"accounts": [{"access_token": _JWT, "user": "a"}]})
    out, _hits, _ = scrub.apply_profile(payload.encode("utf-8"), "json")
    assert _JWT.encode() not in out
    assert b"accounts" in out


def test_json_scrubbing_preserves_structure() -> None:
    payload = json.dumps({"games": [{"id": 5, "name": "Some Game"}]})
    out, _hits, _ = scrub.apply_profile(payload.encode("utf-8"), "json")
    parsed = json.loads(out)
    assert parsed["games"][0]["name"] == "Some Game"


def test_long_error_messages_survive_json_scrubbing() -> None:
    """Regression: a shared helper was truncating at 256 characters.

    Reusing ``redact_for_audit`` cut an install failure's
    ``error_message`` mid-sentence on a real capture, discarding the
    line that actually explained the failure. Audit entries are bounded
    on purpose; diagnostic bundles are not.
    """
    message = (
        "legendary_exit_1: [cli] INFO: Downloads are resumable, you can "
        "interrupt the download with CTRL-C and resume it using the same "
        "command later on. | [cli] CRITICAL: Installation cannot proceed, "
        "exiting. | Installation requirements check returned the following "
        "results: | - Warning: (Linux) This game uses EasyAntiCheat and may "
        "not run on linux | ! Failure: Not enough available disk space! "
        "43.45 GiB < 66.38 GiB"
    )
    assert len(message) > 256
    payload = json.dumps([{"title": "A Game", "error_message": message}])
    out, _hits, _ = scrub.apply_profile(payload.encode("utf-8"), "json")
    parsed = json.loads(out)
    assert parsed[0]["error_message"] == message
    assert "truncated" not in out.decode("utf-8")


def test_json_scrubbing_is_lossless_apart_from_credentials() -> None:
    """Everything that is not a credential must round-trip exactly."""
    original = {
        "history": [
            {"id": 1, "note": "x" * 900, "access_token": "shhh"},
            {"id": 2, "nested": {"deep": ["a", "b", {"ok": True}]}},
        ],
        "count": 2,
    }
    out, _hits, _ = scrub.apply_profile(
        json.dumps(original).encode("utf-8"), "json",
    )
    parsed = json.loads(out)
    assert parsed["history"][0]["note"] == original["history"][0]["note"]
    assert parsed["history"][1] == original["history"][1]
    assert parsed["count"] == 2
    assert parsed["history"][0]["access_token"] != "shhh"


def test_malformed_json_falls_back_to_text_rules() -> None:
    out, _hits, _ = scrub.apply_profile(b'{"broken": ', "json")
    assert b"broken" in out


def test_jsonl_keeps_every_line_parseable() -> None:
    raw = (
        json.dumps({"event": "A", "kwargs": {"access_token": "secret-abc"}})
        + "\n"
        + json.dumps({"event": "B"})
        + "\n"
    )
    out, _hits, _ = scrub.apply_profile(raw.encode("utf-8"), "jsonl")
    lines = [line for line in out.decode("utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    for line in lines:
        json.loads(line)
    assert b"secret-abc" not in out


def test_corrupt_jsonl_line_is_kept_not_dropped() -> None:
    """A truncated line is itself a diagnostic (interrupted write)."""
    out, _hits, _ = scrub.apply_profile(b'{"event": "A"}\n{"trunc\n', "jsonl")
    assert b"trunc" in out


# ── robustness ────────────────────────────────────────────────────
def test_none_profile_is_byte_exact() -> None:
    raw = b"\x00\x01\x02binary-vdf\xff"
    out, hits, dropped = scrub.apply_profile(raw, "none")
    assert out == raw
    assert (hits, dropped) == (0, 0)


def test_scrubbing_never_raises_on_binary_input() -> None:
    out, _hits, _ = scrub.apply_profile(b"\x00\xff\xfe\x80garbage", "text")
    assert isinstance(out, bytes)


def test_counters_report_what_was_masked() -> None:
    raw = f"one {_JWT} two {_JWT}\n".encode()
    _out, hits, _dropped = scrub.apply_profile(raw, "text")
    assert hits >= 2


def test_profile_rules_are_published_for_the_manifest() -> None:
    """The bundle states its own policy, so a reader need not guess."""
    rules = scrub.profile_rules()
    assert "blob" in rules["text_aggressive"]
    assert "blob" not in rules["text"]
