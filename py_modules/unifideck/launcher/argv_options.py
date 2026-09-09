"""launcher/argv_options.py — turn Steam's argv into a game key and options.

Steam invokes ``bin/unifideck-launcher`` with the shortcut's
``LaunchOptions`` appended as **arguments**, not as environment variables.
Everything here is about that hand-off:

* :func:`parse_argv` splits ``argv`` into the ``store:game_id`` key and the
  raw options tail;
* :func:`promote_env_tokens` lifts our own ``UNIFIDECK_*`` flags out of that
  tail and into the launcher's own environment, where the auth and install
  routing reads them;
* :func:`env_overrides_from` builds the overlay applied to the **game's**
  environment, from the user's tokens plus any LSFG request.

Split out of ``dispatcher.py`` when wiring the launch-options parser pushed
that file over the 550-LOC cap (audit §2.9). The three belong together: they
are the only code that interprets the argv tail, and the two env functions
are easy to confuse because one targets the launcher's environment and the
other the game's.
"""
from __future__ import annotations

import os
import shlex

from .types.errors import GameNotFoundError
from .types.options import apply_lsfg_env, parse_launch_options, tokenize_options


def parse_argv(argv: list[str]) -> tuple[str, str]:
    """Return ``(game_key, raw_options)`` from the launcher's ``argv``.

    ``argv[1]`` is the ``store:game_id`` key Unifideck writes into every
    shortcut's ``LaunchOptions``; ``argv[2:]`` is whatever the user added
    after it.

    The tail is re-joined with ``shlex.join``, not with a plain space, so
    the round trip through :func:`tokenize_options` is lossless. Steam
    consumes the quotes when it splits ``LaunchOptions`` into argv, so
    ``MY_VAR="alpha beta"`` arrives as the single element
    ``MY_VAR=alpha beta``; joining that on spaces destroys the boundary and
    the reparse then truncates the value at the space. Measured on a real
    game launch: the game received ``MY_QUOTED=alpha``. ``shlex.join``
    re-quotes any element that needs it, so every consumer downstream --
    all of which split with ``shlex`` -- sees the elements Steam actually
    passed.
    """
    if len(argv) < 2:
        raise GameNotFoundError(
            "missing store:game_id argument",
            context={"argv": argv},
        )
    game_key = argv[1]
    if ":" not in game_key:
        raise GameNotFoundError(
            f"malformed game key {game_key!r}, "
            "expected 'store:game_id'",
            context={"game_key": game_key},
        )
    raw_options = shlex.join(argv[2:])
    return game_key, raw_options


def promote_env_tokens(raw_options: str) -> None:
    """Promote ``UNIFIDECK_*=value`` tokens from launch options to ``os.environ``.

    Steam passes plugin launch options to the wrapper as argv,
    not as env vars : a shortcut configured with launch options
    ``"amazon:amazon-auth UNIFIDECK_AMAZON_ACTION=auth"`` arrives
    as ``sys.argv[1:] = ["amazon:amazon-auth", "UNIFIDECK_AMAZON_ACTION=auth"]``.

    The auth-detection path (and other downstream code) reads
    these flags from ``os.environ``, so we promote any
    bare ``KEY=value`` token in the joined raw options string
    into the process environment before that code runs.
    Only tokens starting with ``UNIFIDECK_`` are promoted —
    don't pollute the env with arbitrary user-supplied args.
    That narrowness is deliberate; the user's own env tokens take the
    separate route through :func:`env_overrides_from`, which targets the
    *game's* environment rather than the launcher's.

    Splits via ``tokenize_options`` (shlex) rather than a bare
    ``raw_options.split()``: the latter turns ``KEY="a b"`` into two
    tokens and promotes a truncated value with a stray quote. Audit §2.9
    found this was the same split, written twice, in two different ways.
    """
    for token in tokenize_options(raw_options):
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        if not key.startswith("UNIFIDECK_"):
            continue
        # Don't clobber an existing real env var — caller wins
        # in case Steam ever evolves to pass env vars properly.
        os.environ.setdefault(key, value)


def env_overrides_from(raw_options: str) -> dict[str, str]:
    """Env overlay for the game, from the user's launch options.

    Two sources, merged so the explicit token wins:

    * the LSFG overlay, when the options requested frame generation
      (a ``~/lsfg`` sentinel or ``LSFG=1`` / ``ENABLE_LSFG=1``), read
      out of the plugin's ``export`` lines;
    * the user's own ``KEY=value`` tokens.

    Lands on ``LaunchContext.env_overrides``, which both env builders
    (``helpers.prepare_native_env`` and ``proton/infrastructure/core``)
    already apply **last**, so a user value beats the plugin's own compat
    env. Putting LSFG here rather than in those builders is what lets both
    launch paths pick it up without either one growing a new parameter.

    The key pattern is uppercase-only, which is load-bearing: this dict is
    user-controlled, and ``LauncherService`` used to read an internal
    ``started_at`` out of it.
    """
    parsed = parse_launch_options(raw_options)
    return {**apply_lsfg_env(parsed), **parsed.env_overrides}
