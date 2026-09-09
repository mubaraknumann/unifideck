from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

_ENV_TOKEN_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")
_LSFG_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)$")
@dataclass
class ParsedOptions:
    """Parsed options."""
    game_args: list[str] = field(default_factory=list)
    env_overrides: dict[str, str] = field(default_factory=dict)
    lsfg_requested: bool = False
def tokenize_options(raw: str) -> list[str]:
    """Split a launch-options string respecting shell quoting.

    Falls back to a naive whitespace split if ``shlex`` chokes
    on malformed input — the previous behaviour Steam shipped
    before Proton 8 and we don't want to error out on it.

    Public because the dispatcher's ``_promote_env_tokens`` shares it.
    That function used to split with a bare ``raw.split()``, which loses a
    quoted value containing a space; the frontend's ``extractUserParams``
    regex already anticipates quoted values, so the two were one bad
    launch-options string away from disagreeing (audit §2.9).
    """
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def _split_env_overrides(
    tokens: list[str], result: ParsedOptions,
) -> list[str]:
    """Pull ``KEY=VALUE`` env tokens into ``result.env_overrides``.

    Returns the tokens that remain (non-env). The split is greedy
    on the leading token sequence : Steam's convention is to put
    env tokens before the command, but we filter anywhere in the
    list because a few user-shared launch-options strings put
    them between wrappers and ``%command%``.
    """
    remaining: list[str] = []
    for tok in tokens:
        m = _ENV_TOKEN_RE.match(tok)
        if m:
            result.env_overrides[m.group(1)] = m.group(2)
        else:
            remaining.append(tok)
    return remaining


def _filter_lsfg_marker(
    tokens: list[str], result: ParsedOptions, home: str,
) -> list[str]:
    """Remove ``~/lsfg`` tokens, setting the request flag.

    The LSFG launcher is a sentinel : its mere presence in the
    launch options means "wrap this run with LSFG env vars". We
    consume the token (it isn't a real wrapper) and signal
    upstream by setting ``result.lsfg_requested``.
    """
    filtered: list[str] = []
    for tok in tokens:
        expanded = tok.replace("~", home, 1) if tok.startswith("~") else tok
        if expanded.endswith("/lsfg"):
            result.lsfg_requested = True
        else:
            filtered.append(tok)
    return filtered


def parse_launch_options(raw: str) -> ParsedOptions:
    """Parse a Steam ``launch-options`` string into wrappers, args, env.

    Refactor history (2026-05-14): inlined tokenisation, env
    split, LSFG filter and ``%command%`` split (CC=11). Pulled
    each phase into a module-level helper so this function reads
    as a linear pipeline.
    """
    result = ParsedOptions()
    if not raw or not raw.strip():
        return result

    tokens = tokenize_options(raw)
    remaining = _split_env_overrides(tokens, result)
    home = str(Path("~").expanduser())
    lsfg_filtered = _filter_lsfg_marker(remaining, result, home)

    # Env-based LSFG opt-in (``LSFG=1`` or ``ENABLE_LSFG=1``).
    # Equivalent of dropping a ~/lsfg sentinel — same outcome.
    if result.env_overrides.get("LSFG") == "1":
        result.lsfg_requested = True
    if result.env_overrides.get("ENABLE_LSFG") == "1":
        result.lsfg_requested = True

    _split_tokens_around_command(lsfg_filtered, result)
    return result

def _split_tokens_around_command(
    tokens: list[str], result: ParsedOptions,
) -> None:

    """Collect the game arguments, ignoring the ``%command%`` markers.

    With a ``%command%`` marker, only the tokens *after* it are game
    arguments; the ones before are wrapper words and are **dropped**, because
    Steam has already applied them pre-exec (audit §2.9, measured: with
    ``env %command% epic:Salt`` Steam ran the launcher under ``env`` and still
    delivered ``argv[1]``). They used to be collected into a ``wrappers``
    list that six argv builders prepended and nothing ever populated in
    production; passing them to the *game* instead would be the §2.9 hazard.
    Audit register item 23b.

    Without a marker every bare token is a game argument — measured: tokens
    after the game key arrive as plain argv.
    """
    found_cmd = False
    before: list[str] = []
    for tok in tokens:
        if tok == "%command%":
            found_cmd = True
            continue
        if tok == "#%command%":
            continue
        if found_cmd:
            result.game_args.append(tok)
        else:
            before.append(tok)
    if not found_cmd:
        # No marker: every bare token is a game argument. Measured — Steam
        # delivers tokens after the game key straight through as argv.
        result.game_args = before


def _strip_matching_quotes(value: str) -> str:
    """Strip a single layer of matching single/double quotes.

    Matches the shell's basic dequoting. Idempotent : a value
    without quotes is returned unchanged.
    """
    if len(value) < 2:
        return value
    if value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    if value[0] == "'" and value[-1] == "'":
        return value[1:-1]
    return value


def _parse_lsfg_export_line(line: str) -> tuple[str, str] | None:
    """Parse a single ``export KEY=VALUE`` line from an LSFG script.

    Returns ``(key, value)`` if the line is a valid export of an
    LSFG-prefixed variable, otherwise ``None``. Handles single-
    and double-quoted values by stripping the surrounding quotes.
    Anything else (comments, shebangs, ``exec``, non-export, keys
    that don't match the LSFG filter) returns ``None`` so the
    caller can ``continue`` cleanly.

    Refactor history (2026-05-14): quote stripping moved to
    ``_strip_matching_quotes`` to flatten the parent's branching.
    """
    line = line.strip()
    if not line or line.startswith(("#", "#!")):
        return None
    if line.startswith("exec ") or not line.startswith("export "):
        return None
    kv = line[len("export "):]
    if "=" not in kv:
        return None
    key, _, value = kv.partition("=")
    key = key.strip()
    value = _strip_matching_quotes(value.strip())
    if not _LSFG_KEY_RE.match(key):
        return None
    return key, value


def apply_lsfg_env(
    opts: ParsedOptions,
    lsfg_script: Path | None = None,
) -> dict[str, str]:
    """Build the env overlay for an LSFG-wrapped launch.

    Returns ``{"ENABLE_LSFG": "1", <LSFG_*>: <value>, ...}`` —
    empty dict when LSFG was not requested or the script isn't on
    disk. Parses ``export`` lines from ``~/lsfg`` (or the supplied
    ``lsfg_script`` path, used by tests) and applies only keys
    that pass :data:`_LSFG_KEY_RE`.
    """
    if not opts.lsfg_requested:
        return {}
    if lsfg_script is None:
        lsfg_script = Path(str(Path("~/lsfg").expanduser()))
    if not lsfg_script.is_file():
        return {}
    overlay: dict[str, str] = {"ENABLE_LSFG": "1"}
    try:
        content = lsfg_script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # We can't read the script (permissions, racing rotate),
        # but the user did ask for LSFG — return the enable flag
        # so the launcher can at least try with default values.
        return overlay
    for raw_line in content.splitlines():
        parsed = _parse_lsfg_export_line(raw_line)
        if parsed is not None:
            key, value = parsed
            overlay[key] = value
    return overlay
