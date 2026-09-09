"""What a wrapper store's session consists of, per store.

py_modules/unifideck/launcher/wrapper_session_specs.py

The data half of session propagation; ``wrapper_session`` is the behaviour
half. Split because they change for different reasons and at different rates:
the mechanism is stable, while these declarations track whatever a vendor
client happens to do this release.

**What is generic is the mechanism; what lives here is the evidence.** A
generic guess about which files constitute a session, or about what a
signed-out prefix looks like, would be exactly the mistake the guards exist to
prevent — the same reason ``stores/shared/prefix_placement`` refuses to own
``remover``/``holds_game``. Adding a store is a row in :data:`SPECS`.

Measured Battle.net layout (client build 17651, 2026-08-11).

**The login token is a registry key, not a file.** That is the single fact
that makes this work, and getting it wrong is why a first attempt shipped a
session the server answered with ``ERROR_TOKEN_NOT_FOUND (49)``. The client's
own log says it plainly: ``BattleNetLogin::DeleteToken(): Deleting registry
token``. The keys live in ``user.reg`` under ``Software\\Blizzard
Entertainment\\Battle.net\\`` — ``UnifiedAuth`` (the token),
``EncryptionKey``, ``Identity``. Those are exactly the three names the
``stores/battlenet/prefix/manager.py`` docstring recorded from its on-device
experiment; searching for them under ``AppData`` finds nothing because they
were never files. ``launcher/wine_registry`` moves them.

It is also why a whole-prefix ``rsync`` clone opens signed in: it carries
``user.reg``. Any copy that moves only the client's files cannot.

The files that travel with the token, and why each is classified as it is:

    AppData/Local/Battle.net/Account/<accountid>/account.db
        128 KB fixed size, fully encrypted; 12 of its 32 pages differed
        between the auth prefix and a prefix whose client had run. Rotates
        with the session, but is not the token itself. Fixed size means a
        shrink test — Ubisoft's logout signature — cannot work here.
    AppData/Local/Battle.net/BrowserCaches/{common,<accountid>}/
        CEF cookie jars: bnet.pam, bnet.extra, web.id, JSESSIONID, login.key.
        The web half of the session.
    AppData/Local/Battle.net/CachedData.db
        NOT the token, and never evidence: its ``login_cache`` and
        ``key_value_store`` were identical across a rotation, and
        ``battlenet/store.py`` documents the licence ledger as surviving a
        sign-out. Carried for ownership freshness only.
    AppData/Roaming/Battle.net/Battle.net.config
        Not session material, and never copied **as a file**. It is the
        client's settings file, and it travels key by key instead: see
        :class:`PrefsSpec` and ``launcher/wrapper_prefs``.

**The settings file is per-prefix, and that was a bug of its own.** Every game
gets its own prefix, so the launcher's own settings live in whichever prefix
the user happened to change them in and reach no other. The reported symptom
was a language setting that "reverts to default every time you launch a game":
not a clobber, an absence of propagation. Measured layout (same client build)::

    {
      "Client": { "GaClientId": ..., "AutoLogin": ..., "Toasts": {...},
                  "Version": { "LastBuildVersion": "17651", ... },
                  "Install": { "DefaultInstallPath": "C:/Program Files (x86)" } },
      "5a61123b37cafce1": {                    # hash of the client install path
          "Client": { "Language": "enUS", "LoginSettings": {...} },
          "Path": "C:\\Program Files (x86)\\Battle.net",
          "Services": { "LastLoginRegion": "US", ... } },
      "Games": { "d1": { "LastPlayed": ..., "LastActioned": ... } }
    }

Two things about it are worth writing down, because neither is guessable:

* **The launcher's language is** ``<install-hash>.Client.Language``, not
  ``Client.Language``. The hash covers the client's install path, which
  ``client_install.INSTALLER_ARGS`` pins to ``C:\\Program Files (x86)\\
  Battle.net``, so it is the same string in every one of our prefixes
  (verified: ``5a61123b37cafce1`` in the auth prefix, the template and a game
  prefix). A store that let the install path vary would get a second section
  here rather than a merge conflict.
* **The client rewrites the whole file from memory when it starts.** A game
  prefix's config was observed carrying a ``LastPlayed`` from a session nine
  hours older than the file's own mtime. So a write that lands while the client
  is up is discarded without error, exactly the hazard
  ``wine_registry.registry_is_writable`` exists for. Preferences are written
  only into a prefix with no live client.

Stdlib-only; runs under the SYSTEM python (3.10-3.14).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# ``prefix_layout`` is stdlib-only (logging + pathlib) and its package
# ``__init__`` is empty, so importing it here does not drag the Proton
# stack into the plugin-side importers of this module.
from unifideck.launcher.proton.infrastructure.prefix_layout import (
    resolve_drive_c,
)

GAMES_DIR_NAME = "games"


@dataclass(frozen=True, slots=True)
class PrefsSpec:
    """One wrapper store's launcher settings, and what must not travel.

    A **denylist**, deliberately, where the session is an allowlist. The two
    are different kinds of question. Session material is a fixed, measurable
    set of files, and guessing at it is the mistake this module's own docstring
    warns about. Settings are not: a vendor client writes a key only once the
    user moves it off default, so ``HardwareAcceleration`` is absent from every
    config until something turns it off, and no allowlist we could write today
    would carry the setting a user changes next release. Naming the handful of
    keys that are provably *not* preferences is the only form of this that can
    be complete.

    ``exclude`` holds dotted paths. A ``*`` segment matches exactly one key,
    which is how the install-hash section above is addressed without hardcoding
    a hash - the same single-``*`` convention :meth:`SessionSpec.expand` uses
    for Battle.net's numeric account id. A pattern matches its whole subtree,
    so ``Games`` excludes ``Games.d1.LastPlayed`` without naming it.
    """

    # drive_c-relative path to the client's settings file.
    file: str
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """One wrapper store's session material and how to judge it.

    Paths are ``drive_c``-relative and may contain a single ``*`` component
    (Battle.net scopes its vault by numeric account id, which we discover
    rather than hardcode).
    """

    store: str
    files: tuple[str, ...]
    trees: tuple[str, ...] = ()
    # Subset of ``files`` whose presence (non-empty) proves a session exists.
    # Deliberately narrower than ``files``: Battle.net's CachedData.db travels
    # with the session but survives a sign-out, so trusting it as evidence
    # would report "signed in" forever.
    evidence: tuple[str, ...] = ()
    # ``user.reg`` key prefixes holding the token. Moved surgically, section by
    # section, because the same file carries the installed game's own paths.
    # A store with none leaves this empty and nothing registry-related runs.
    registry_keys: tuple[str, ...] = ()
    # Reads the prefix-bound identity the session is cryptographically tied
    # to. Injection is refused across a mismatch. Per-store because the
    # sources are unrelated: a JSON key for Battle.net, a registry key for
    # Ubisoft's DPAPI vault.
    identity: Callable[[Path], str | None] | None = field(
        default=None, compare=False,
    )
    # The client's own settings file, moved key by key rather than copied.
    # A store without one leaves this None and no preference pass runs.
    prefs: PrefsSpec | None = None
    # drive_c-relative content-addressed caches worth carrying back to the
    # template so the next prefix does not re-download them. Read by
    # ``launcher/wrapper_client_cache``, which documents why this may only
    # ever name a *content store* and never an extracted program tree.
    client_cache: tuple[str, ...] = ()

    def expand(self, drive_c: Path, patterns: tuple[str, ...]) -> list[Path]:
        """Resolve ``patterns`` against ``drive_c``, expanding any ``*``."""
        found: list[Path] = []
        for pattern in patterns:
            if "*" in pattern:
                found.extend(sorted(drive_c.glob(pattern)))
            else:
                found.append(drive_c / pattern)
        return found


_BNET_LOCAL = "users/steamuser/AppData/Local/Battle.net"
_BNET_CONFIG = "users/steamuser/AppData/Roaming/Battle.net/Battle.net.config"


def read_gaclientid(prefix: Path) -> str | None:
    """Battle.net's client-instance id, the value its token is bound to.

    Measured: copying the vault without this produced a password form
    (``browser state changed: LoginCredential``); with it the client signed
    straight in. It is identical across our tiers because they are clones, so
    a mismatch means the prefix is not one of ours and its vault would be
    rejected anyway.

    Read from the settings file but never written back into one: it is the
    identity a copy is checked against, so a preference pass that carried it
    would be checking prefixes against a value it had itself installed.
    """
    drive_c = resolve_drive_c(prefix)
    if drive_c is None:
        return None
    config = drive_c / _BNET_CONFIG
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    client = data.get("Client")
    if not isinstance(client, dict):
        return None
    value = client.get("GaClientId")
    return value if isinstance(value, str) and value else None


# Exactly the three keys the on-device experiment in ``manager.py`` named, and
# no more. The sibling ``Launch Options`` key under the same parent is
# deliberately excluded: it carries per-game subkeys (``Launch Options\\OSI``
# appeared once a game had been launched), and spreading one game's launch
# options into every other prefix is not something a session transplant should
# do. ``UnifiedAuth`` is the token; its section timestamp is the rotation clock.
_BNET_REG = "Software\\\\Blizzard Entertainment\\\\Battle.net\\\\"

# Everything in ``Battle.net.config`` is a user preference except these, and
# each one is here for a reason that cost something to learn:
#
#   Client.GaClientId          the identity ``read_gaclientid`` verifies
#                              against; see its docstring
#   Client.Install             per-prefix. Holds DefaultInstallPath, and a
#                              game prefix may sit on removable storage the
#                              other prefixes cannot see
#   Client.SavedAccountNames   sign-in state. Owned by the session pass and by
#   Client.AutoLogin           ``tweaks.clear_client_credentials``; carrying
#                              these would let one prefix's sign-out travel
#   Client.Version             per-prefix client build state. The client
#                              self-updates into a versioned sibling directory
#                              per prefix, so LastBuildVersion from a prefix
#                              that updated would misreport this one's build
#   Games                      per-game LastPlayed / LastActioned / Resumable
#   *.Path, *.Services         inside the install-hash section: the install
#                              path, and per-login service routing
#
# Not listed, therefore carried: ``<hash>.Client.Language`` (the reported bug),
# LoginSettings, Toasts, AutoStartMinimized, GameSearch, and the
# HardwareAcceleration / Sound / Streaming tweaks ``prefix/tweaks.py`` writes.
_BNET_PREFS = PrefsSpec(
    file=_BNET_CONFIG,
    exclude=(
        "Client.GaClientId",
        "Client.Install",
        "Client.SavedAccountNames",
        "Client.AutoLogin",
        "Client.Version",
        "Games",
        "*.Path",
        "*.Services",
    ),
)

# The Agent's content-addressed store, and nothing else under ``Agent/``.
#
# Measured 2026-08-22: on a fresh prefix the Agent makes its own self-update
# the single exclusive operation, so the game's download sits behind it. That
# costs 2 seconds when the local store already holds the build's tagged
# content and 45 minutes when it does not. The prefix is deleted on
# cancel, so the next attempt re-downloads the same ~9 MB from zero. Same
# build hash both times (``d049a9f9…``, Agent 2.40.3.9700); only the TACT tag
# query differed (``Volatile Windows US?`` vs ``KR? acct-IND? geoip-IN?``),
# because the bootstrapper warms the store pre-login as US and the login then
# moves the account's region.
#
# Sibling ``Agent.<build>/`` is deliberately absent: that is the extracted,
# running Agent, and a half-applied copy inherited by every future prefix is a
# far worse failure than a slow one. ``Logs/`` under it is likewise skipped:
# carrying old logs forward is what made the salvage in
# ``stores/shared/prefix_forensics`` ambiguous about which run it was reading.
_BNET_CLIENT_CACHE = ("ProgramData/Battle.net/Agent/data",)

# One row per wrapper store. Ubisoft joins this table when its private
# ``session/`` package is ported onto the shared layer; its ``identity``
# reader is the ``system.reg`` MachineGuid probe that guards its DPAPI vault.
SPECS: dict[str, SessionSpec] = {
    "battlenet": SessionSpec(
        store="battlenet",
        files=(
            f"{_BNET_LOCAL}/Account/*/account.db",
            f"{_BNET_LOCAL}/CachedData.db",
        ),
        trees=(f"{_BNET_LOCAL}/BrowserCaches",),
        evidence=(f"{_BNET_LOCAL}/Account/*/account.db",),
        registry_keys=(
            f"{_BNET_REG}UnifiedAuth",
            f"{_BNET_REG}EncryptionKey",
            f"{_BNET_REG}Identity",
        ),
        identity=read_gaclientid,
        prefs=_BNET_PREFS,
        client_cache=_BNET_CLIENT_CACHE,
    ),
}


def spec_for(store: str | None) -> SessionSpec | None:
    """The session spec for ``store``, or None when it has no session."""
    return SPECS.get(store) if store else None


# ── the prefix index the backend writes for us ─────────────────────────────


