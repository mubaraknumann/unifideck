"""config/validator.py — JSON Schema validation for Unifideck config.

validates the defaults/config.json file and the optional
user overrides file at boot using a formal JSON Schema (Draft-07). The
schema lives next to this module in config/schema.json.

Design decisions ( planning)
------------------------------------------
- **Q3 Option C** — dual interface: imperative `validate_config()` returns
  a ValidationResult AND emits CONFIG_VALIDATION_* events on the bus.
  The caller (main.py) reads the return value to decide whether to start
  the stores, and SecurityService (or future ConfigService) observes the
  events for audit logging without being directly coupled to the validator.

- **Q5 Option B** — defaults and user overrides are validated separately,
  then the merge is validated once more. This lets error messages pinpoint
  exactly which file is coupled ("your ~/.config/unifideck/config.json has
  an invalid value at stores.microsoft.client_id") rather than reporting
  a generic "merged config is broken".

- **Q6 Option A** — schema.json lives in this package directory.

- **Q7 Option Strict** — schema uses additionalProperties: false everywhere
  to catch typos in key names. Adding a new valid key requires updating
  both the code and the schema — this is a feature, not a bug.

- **Q8 Option Degraded** — when validation fails, we return a result with
  success=False but DO NOT raise. main.py decides whether to start in
  degraded mode or crash. Default policy: degraded — ignore user overrides
  if they're invalid and fall back to the defaults.

- **Q11** — error messages are kept in English because they target
  operators debugging their config, not end-users. The UI that surfaces
  the "degraded mode active" banner uses i18n via en-US.json.

- **Q12 Option Full** — schema covers all 19 top-level sections of
  config.json, not just stores.*. This catches configuration drift in
  any section (cache, sync, security, probes, etc).

Usage
-----
From main.py at boot::

    from unifideck.config.validator import ConfigValidator

    validator = ConfigValidator(bus=self.bus)
    result = await validator.validate_config(
        defaults_path="defaults/config.json",
        user_path="~/.config/unifideck/config.json",
    )
    if not result.success:
        logger.warning(
            "[Unifideck] Config validation failed (%d errors), "
            "starting in degraded mode", len(result.errors),
        )
        # main.py continues anyway with defaults-only config

The validator never raises. Every failure path returns a ValidationResult
with the appropriate error list so the caller always gets a structured
response.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio

    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

# Path to the schema file, resolved relative to this module. Using
# __file__ rather than a hardcoded path so the validator works regardless
# of the cwd when main.py imports it.
_SCHEMA_PATH = str(Path(__file__).resolve().parent / "schema.json")

# Which config file a validation error belongs to. Exposed in the
# ValidationError dataclass so operators can see exactly where to fix
# things. "merged" is used when we validate the final merge between
# defaults and user overrides (rare — normally either the defaults or
# the user file is the one that failed).
_SOURCE_DEFAULTS = "defaults"
_SOURCE_USER = "user_overrides"
_SOURCE_MERGED = "merged"


def _safe_violation(err: Any) -> str:
    """Describe a jsonschema error without quoting the offending value.

    ``err.message`` embeds ``err.instance`` — the user's own config
    value — which makes it unsafe for a support bundle. The pair
    (``validator``, ``validator_value``) says the same thing using only
    the schema side: ``required``/``['client_id']``, ``type``/``integer``.
    Rendered as ``"type != integer"``.

    Falls back to the bare keyword, then to ``""``, because a support
    bundle losing one detail line is always preferable to a validation
    run raising.
    """
    keyword = getattr(err, "validator", None)
    if not keyword:
        return ""
    expected = getattr(err, "validator_value", None)
    if expected is None:
        return str(keyword)[:120]
    return f"{keyword} != {expected}"[:120]


@dataclass(frozen=True)
class ValidationError:
    """A single schema violation, pinned to its source file.

    Immutable so it's safe to pass around and include in event
    payloads without fear of mutation.

    Attributes:
        source: Which file the error came from. One of "defaults",
            "user_overrides", or "merged".
        path: Dotted path to the offending key, e.g.
            "stores.microsoft.client_id". Empty string for root-level
            errors.
        message: Human-readable error from jsonschema, kept short
            (capped at 256 chars). English only — target audience is
            operators debugging their config in plugin logs.

            **Log-only — never put this in a support bundle.**
            jsonschema interpolates the offending *instance* into its
            message, so a bad ``stores.*.client_id`` renders as
            ``"'sk-abc123' is not of type 'integer'"``. The bundle is
            pasted in public; use ``safe_message`` there.
        safe_message: The same violation with no user data — built
            from jsonschema's ``validator`` keyword and its schema-side
            ``validator_value`` (e.g. ``"type != integer"``). Both come
            from the schema, never from the config, so this is safe to
            put anywhere ``path`` is. Empty for errors raised by this
            module rather than by jsonschema.

    """

    source: str
    path: str
    message: str
    safe_message: str = ""


@dataclass
class ValidationResult:
    """Outcome of a full config validation run.

    Attributes:
        success: True iff every validated file (defaults, user overrides
            if present, and the merge) passed the schema. False on any
            failure — callers check this flag to decide whether to
            start in degraded mode.
        errors: List of ValidationError, one per schema violation. Empty
            on success. Capped at 50 entries to avoid log bloat on a
            deeply broken config.
        defaults_validated: True if we successfully read and validated
            the defaults file. False if we couldn't even read it (which
            is a fatal condition — Unifideck cannot start without its
            baseline config).
        user_overrides_present: True if a user overrides file was found
            on disk. False means the user never customised anything,
            which is normal for a fresh install.

    """

    success: bool = False
    errors: list[ValidationError] = field(default_factory=list)
    defaults_validated: bool = False
    user_overrides_present: bool = False


class ConfigValidator:
    """Validates Unifideck configuration against the JSON Schema.

    Stateless apart from the lazily-loaded schema which is cached after
    first use. Safe to instantiate once per plugin boot and reuse for
    every validation call (typically just one at startup).

    Thread safety: the validate_* methods are synchronous and pure apart
    from emitting events on the bus. Safe to call from any context.
    """

    def __init__(self, bus: EventBus | None = None) -> None:
        """Create a validator.

        Args:
            bus: Optional EventBus for emitting CONFIG_VALIDATION_*
                events. When None, validation still works — the events
                just go nowhere, which is the right behaviour for unit
                tests that don't wire up a bus.

        """
        self._bus = bus
        self._schema: dict[str, Any] | None = None
        # Strong references to fire-and-forget event-emit tasks so they
        # aren't garbage-collected mid-flight. See ``_emit_result_event``.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    # ── Public API ──────────────────────────────────────────────

    async def validate_config(
        self,
        defaults_path: str,
        user_path: str | None = None,
    ) -> ValidationResult:
        """Validate defaults + optional user overrides against the schema.

        Args:
            defaults_path: Path to the shipped defaults/config.json.
                Relative paths are resolved against the plugin root.
                If this file cannot be read or fails validation, the
                result has defaults_validated=False and the plugin
                should NOT start (this is the baseline config).
            user_path: Optional path to the user overrides file. When
                present on disk it's validated separately. A missing
                file is normal — many users never customise anything.

        Returns:
            A ValidationResult. Always returns, never raises.
            Callers should check result.success and result.errors to
            decide what to do. Events are emitted on the bus before
            returning.

        """
        result = ValidationResult()
        schema = self._load_schema()
        if schema is None:
            result.errors.append(ValidationError(
                source=_SOURCE_DEFAULTS, path="",
                message="Cannot load validator schema.json",
            ))
            self._emit_result(result)
            return result
        defaults = self._validate_defaults(
            defaults_path, schema, result,
        )
        if defaults is None:
            self._emit_result(result)
            return result
        merged = self._validate_user_overrides(
            defaults, user_path, schema, result,
        )
        self._validate_merged(merged, schema, result)
        # Cap errors to 50 to avoid pathological log spam.
        if len(result.errors) > 50:
            result.errors = result.errors[:50]
        result.success = (
            result.defaults_validated and len(result.errors) == 0
        )
        self._emit_result(result)
        return result

    def _validate_defaults(
        self, defaults_path: str, schema: dict[str, Any],
        result: ValidationResult,
    ) -> dict[str, Any] | None:
        """Read + validate the defaults file, update result in place.

        Returns the parsed defaults dict on success (even with
        validation errors — the data is still readable), or None
        if the file itself is unreadable, in which case the caller
        should bail out immediately since there's nothing to merge
        against.
        """
        defaults = self._read_json(defaults_path)
        if defaults is None:
            result.errors.append(ValidationError(
                source=_SOURCE_DEFAULTS, path="",
                message=f"Cannot read defaults file at {defaults_path}",
            ))
            return None
        errors = self._validate_against_schema(
            defaults, schema, _SOURCE_DEFAULTS,
        )
        result.errors.extend(errors)
        result.defaults_validated = len(errors) == 0
        return defaults

    def _validate_user_overrides(
        self,
        defaults: dict[str, Any],
        user_path: str | None,
        schema: dict[str, Any],
        result: ValidationResult,
    ) -> dict[str, Any]:
        """Validate user overrides if present; return merged config.

        A missing user file is NOT an error — it just means the user
        never customised anything, which is normal. If the file is
        present but unreadable or fails validation, the errors are
        appended to the result and the merge falls back to defaults
        only (we don't merge broken overrides in).

        Note on schema semantics: user overrides are expected to be
        *partial* — a user who only wants to bump sync.artwork_concurrency
        writes a tiny file containing just that key, and the merge
        with defaults fills in the rest. We therefore validate the
        user file against a RELAXED copy of the schema where all
        `required` clauses have been stripped. Type checks, strict
        additionalProperties, URL patterns and range constraints all
        still apply — only the "this key must be present" rule is
        dropped, because overrides are partial by design.
        """
        if user_path is None:
            return defaults
        expanded = str(Path(user_path).expanduser())
        if not Path(expanded).is_file():
            return defaults
        result.user_overrides_present = True
        user_data = self._read_json(expanded)
        if user_data is None:
            result.errors.append(ValidationError(
                source=_SOURCE_USER, path="",
                message=(
                    f"Cannot read or parse user overrides "
                    f"at {expanded}"
                ),
            ))
            return defaults
        relaxed_schema = self._strip_required(schema)
        user_errors = self._validate_against_schema(
            user_data, relaxed_schema, _SOURCE_USER,
        )
        result.errors.extend(user_errors)
        if user_errors:
            return defaults
        return self._deep_merge(defaults, user_data)

    @staticmethod
    def _strip_required(schema: Any) -> Any:
        """Recursively return a deep copy of schema with 'required' dropped.

        Used to build the relaxed schema applied to partial user
        overrides. Preserves every other constraint (type, pattern,
        additionalProperties, minimum/maximum, enum, $ref, $defs...)
        so overrides are still validated strictly for the keys they
        DO contain — only the "this key must exist" rule is removed.

        Works recursively through properties, $defs, arrays of
        sub-schemas (oneOf, anyOf, allOf), and plain dicts.
        """
        if isinstance(schema, dict):
            out = {}
            for key, value in schema.items():
                if key == "required":
                    continue
                out[key] = ConfigValidator._strip_required(value)
            return out
        if isinstance(schema, list):
            return [
                ConfigValidator._strip_required(item) for item in schema
            ]
        return schema

    def _validate_merged(
        self, merged: dict[str, Any], schema: dict[str, Any],
        result: ValidationResult,
    ) -> None:
        """Final sanity check on the merged config.

        Only runs if the defaults and user overrides both passed
        individually — otherwise the merge is meaningless. Catches
        edge cases where merging introduces violations that neither
        source had on its own (e.g. a user override setting a whole
        section to null, bypassing required-field constraints).
        """
        if not result.defaults_validated:
            return
        if any(e.source == _SOURCE_USER for e in result.errors):
            return
        merged_errors = self._validate_against_schema(
            merged, schema, _SOURCE_MERGED,
        )
        result.errors.extend(merged_errors)

    # ── Private helpers ─────────────────────────────────────────

    def _load_schema(self) -> dict[str, Any] | None:
        """Load schema.json from disk, cached on first call.

        Returns None if the file cannot be read or parsed — the
        caller treats this as a fatal condition because without a
        schema, we can't validate anything.
        """
        if self._schema is not None:
            return self._schema
        try:
            with Path(_SCHEMA_PATH).open(encoding="utf-8") as f:
                self._schema = json.load(f)
            return self._schema
        except (OSError, json.JSONDecodeError):
            logger.exception("[ConfigValidator] cannot load schema at %s", _SCHEMA_PATH)
            return None

    @staticmethod
    def _read_json(path: str) -> dict[str, Any] | None:
        """Read and parse a JSON file, returning None on any failure.

        Failures are logged at warning level. Callers treat None as
        "file unreadable" and report it as a validation error with
        the appropriate source.
        """
        expanded = str(Path(path).expanduser())
        try:
            with Path(expanded).open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "[ConfigValidator] cannot read %s: %s", expanded, e,
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                "[ConfigValidator] %s is not a JSON object", expanded,
            )
            return None
        return data

    @staticmethod
    def _validate_against_schema(
        data: dict[str, Any], schema: dict[str, Any], source: str,
    ) -> list[ValidationError]:
        """Run jsonschema validation and convert errors to our format.

        Collects ALL errors (not just the first) via iter_errors so
        operators see everything they need to fix in one log pass.
        Returns an empty list on success.
        """
        # Import jsonschema lazily so the validator module can be
        # imported even in environments where the dependency isn't
        # yet installed (e.g. during setup scripts).
        try:
            import jsonschema
        except ImportError:
            logger.exception(
                "[ConfigValidator] jsonschema not installed — "
                "validation skipped",
            )
            return [ValidationError(
                source=source,
                path="",
                message="jsonschema library not installed",
            )]
        errors: list[ValidationError] = []
        validator = jsonschema.Draft7Validator(schema)
        for err in validator.iter_errors(data):
            path = ".".join(str(p) for p in err.absolute_path)
            errors.append(ValidationError(
                source=source,
                path=path,
                message=err.message[:256],
                safe_message=_safe_violation(err),
            ))
        return errors

    @staticmethod
    def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
        """Recursively merge overrides into base, returning a new dict.

        Used to produce the final config that would be seen by
        ConfigManager.get(). Arrays are replaced wholesale (not
        concatenated) — same semantics as ConfigManager._merge.
        """
        result = dict(base)
        for key, value in overrides.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(value, dict)
            ):
                result[key] = ConfigValidator._deep_merge(
                    result[key], value,
                )
            else:
                result[key] = value
        return result

    def _emit_result(self, result: ValidationResult) -> None:
        """Emit CONFIG_VALIDATION_COMPLETED or _FAILED on the bus.

        Best-effort fire-and-forget: a failure to emit (bus down,
        handler raised, or no running loop) is swallowed at debug
        level — validation must never block the plugin boot
        because the audit couldn't be logged.
        """
        if self._bus is None:
            return
        try:
            import asyncio

            from unifideck.core.types.events import Events
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop — drop the emission.
                return
            if result.success:
                # Track task so it isn't GC'd before the bus delivers the event.
                task = loop.create_task(self._bus.emit(
                    Events.CONFIG_VALIDATION_COMPLETED,
                    defaults_validated=result.defaults_validated,
                    user_overrides_present=result.user_overrides_present,
                ))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            else:
                task = loop.create_task(self._bus.emit(
                    Events.CONFIG_VALIDATION_FAILED,
                    error_count=len(result.errors),
                    defaults_validated=result.defaults_validated,
                    user_overrides_present=result.user_overrides_present,
                    first_error_source=(
                        result.errors[0].source if result.errors else ""
                    ),
                    first_error_path=(
                        result.errors[0].path if result.errors else ""
                    ),
                ))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
        except (RuntimeError, asyncio.CancelledError) as e:
            logger.debug(
                "[ConfigValidator] event emit failed: %s", e,
            )
