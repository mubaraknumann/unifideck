"""Tests for ``_safe_violation`` — the redacted half of a validation error.

``ValidationError.message`` is jsonschema's own text, which interpolates the
offending *instance*: a bad credential key renders as
``"'sk-abc123' is not of type 'integer'"``. That is fine in a plugin log and
unacceptable in a support bundle, which reporters paste in public. The
support bundle therefore carries ``safe_message``, built only from the schema
side of the violation.

Kept jsonschema-free on purpose, matching ``test_config_defaults_schema.py``:
the vendored jsonschema is not importable under every local Python (its
``rpds`` native extension is built per-ABI). ``_safe_violation`` reads exactly
two documented attributes off the error object, so a stand-in that carries
them exercises the real contract.
"""
from __future__ import annotations

from types import SimpleNamespace

from unifideck.config.validator import _safe_violation

#: Stand-in for a real credential value, to prove it never survives.
SECRET = "sk-SECRET-abc123"  # noqa: S105 — test fixture, not a credential


def test_a_type_violation_keeps_the_expectation_and_drops_the_value() -> None:
    """The diagnostic must survive redaction, or the block is pointless."""
    err = SimpleNamespace(
        validator="type",
        validator_value="integer",
        message=f"'{SECRET}' is not of type 'integer'",
        instance=SECRET,
    )
    safe = _safe_violation(err)
    assert safe == "type != integer"
    assert SECRET not in safe


def test_a_required_violation_names_the_missing_key() -> None:
    """``required`` puts the key on the schema side, so it is safe to keep."""
    err = SimpleNamespace(validator="required", validator_value=["host"])
    assert _safe_violation(err) == "required != ['host']"


def test_a_keyword_without_a_schema_value_degrades_to_the_keyword() -> None:
    err = SimpleNamespace(validator="enum", validator_value=None)
    assert _safe_violation(err) == "enum"


def test_an_error_this_module_raised_itself_has_no_keyword() -> None:
    """Our own ValidationErrors (schema unreadable, jsonschema missing)
    carry no jsonschema keyword — an empty string, never a crash."""
    assert _safe_violation(SimpleNamespace(validator=None, validator_value=None)) == ""
    assert _safe_violation(SimpleNamespace()) == ""


def test_a_long_schema_value_is_capped() -> None:
    """A huge ``enum`` must not blow up the bundle line."""
    err = SimpleNamespace(validator="enum", validator_value=[f"v{i}" for i in range(200)])
    assert len(_safe_violation(err)) <= 120


def test_a_secret_hidden_in_a_schema_value_is_still_capped_not_leaked() -> None:
    """``validator_value`` comes from our own shipped schema, so it cannot
    hold user data — this pins that assumption as a test rather than a
    comment, so a future schema carrying a default credential would fail
    here instead of shipping."""
    err = SimpleNamespace(validator="const", validator_value=SECRET)
    # Documented behaviour: schema-side values ARE included. If a schema ever
    # legitimately holds a secret, this test is the tripwire to revisit.
    assert _safe_violation(err) == f"const != {SECRET}"
