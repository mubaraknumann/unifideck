"""Global test isolation: the toast bridge, and $HOME-redirecting env vars.

``frontend_bridge.EVENTS_FILE`` is a module-level constant pointing at the
REAL ``~/.local/share/unifideck/launcher_events.jsonl``. Any test that
exercises a path calling ``launcher_toast`` — umu retry, compat/prereq
install, store handlers — therefore appended genuine toast events to the
live file. The plugin's *persistent* ``get_launcher_toasts`` poll drains
that file regardless of whether the QAM panel is open, so running the
suite popped real "Retrying Launch — Retrying UMU in 3s (attempt 2/2)…"
toasts into the Steam UI.

That is worse than cosmetic. The file is capped at 100 lines AND is
collected into diagnostic bundles, so a test run silently evicted real
launch history — the exact evidence a bug report depends on. Measured
before this fixture landed: 36 of 79 live lines were test noise.

``tests/unit/test_frontend_bridge.py`` already redirects ``EVENTS_FILE``
for its own cases; this does it for every other test, autouse, so no test
can reach the user's data dir. A test that patches ``EVENTS_FILE``
explicitly still wins — this only guarantees the default is never live.

The second fixture closes a different leak, one that only shows up on
CI. 18 test files build a fake device tree under a scratch dir and point
``HOME`` at it with ``monkeypatch.setenv``. That is not sufficient on its
own: production path resolution deliberately prefers the XDG variables
over ``$HOME`` (``config/user_config_path.resolve_user_config_path``
checks ``UNIFIDECK_USER_CONFIG``, then ``XDG_CONFIG_HOME``, and only then
``~/.config``), so with any of them exported the code under test walks
right past the fixture's fake home to the real one.

GitHub's ubuntu runner image exports ``XDG_CONFIG_HOME``; SteamOS and
the containers we reproduce CI in do not. So the suite passed everywhere
locally and failed only on CI, where
``test_credentials_are_still_audited_as_present`` asserted a token file
it had just written was ``present`` and got ``missing`` — the audit had
resolved ``/home/runner/.config/unifideck/`` instead of the fake home.
Two test files had already worked around this one ``delenv`` at a time;
doing it here covers every current and future HOME-patching test.

Only the variables that *redirect path resolution* are cleared.
``XDG_RUNTIME_DIR``/``XDG_SESSION_TYPE``/``XDG_CURRENT_DESKTOP`` are
left alone: the support bundle only reports those as diagnostics, and a
test asserting on the environment report should see the real values.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Exported by a real desktop session or a CI runner, and honoured ahead of
# ``$HOME`` by the resolvers named in the module docstring.
_HOME_REDIRECTING_ENV = (
    "UNIFIDECK_USER_CONFIG",   # config/user_config_path (absolute override)
    "UNIFIDECK_PLUGIN_DIR",    # core/paths
    "XDG_CONFIG_HOME",         # config/user_config_path, support_bundle
    "XDG_DATA_HOME",           # stores/ubisoft/binaries
    "XDG_CACHE_HOME",          # support_bundle/probe_stack
)


@pytest.fixture(autouse=True)
def _isolate_home_redirecting_env(monkeypatch, tmp_path_factory):
    """Point ``HOME`` at a tmp dir and unset the vars that would override it.

    Unsetting alone only helped tests that remembered to patch ``HOME``
    themselves. One that forgot reached the developer's real home: a unit
    test for the nile credential self-heal ran the genuine quarantine helper
    and renamed the live ``~/.config/nile/user.json``, signing the machine
    out of Amazon. Test runs must never be able to touch real user state, so
    the redirect is applied to every test rather than opted into.

    A test that genuinely wants a different ``HOME`` (or one of these vars)
    can still ``setenv`` it in its own body: this fixture runs first, so the
    test's value wins.
    """
    for name in _HOME_REDIRECTING_ENV:
        monkeypatch.delenv(name, raising=False)
    # A dir of its own, NOT under the test's ``tmp_path``: several tests
    # assert on the exact contents of ``tmp_path`` and a stray home sandbox
    # inside it makes them fail.
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    # ``Path.home()`` consults these before ``HOME`` on some platforms, and
    # ``os.path.expanduser`` falls back to the password database when HOME is
    # absent — keep both pointing at the sandbox.
    monkeypatch.setenv("USERPROFILE", str(home))


def _live_data_dir():
    """The user's REAL unifideck data dir, resolved from the password database.

    Deliberately not from ``$HOME`` — the fixtures above have already
    redirected that, which is the whole point.
    """
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "share" / "unifideck"


@pytest.fixture(scope="session", autouse=True)
def _fail_on_live_data_writes():
    """Fail the run if the suite wrote into the user's real data directory.

    The ``HOME`` redirect above is necessary but not sufficient: it cannot
    reach a path a module resolved **at import time**, before the fixture
    ran. Three such constants were found doing exactly that, and the proof
    was mtimes rather than a failing test — a suite run wrote a synthetic
    ``fenris`` row into the live ``battlenet_id_map.json`` and fixture saves
    into ``save_backups/``, hours after the plugin had last been up.

    Reading real user state is bad; *writing* it destroys evidence and, in
    one measured case, signed the machine out of Amazon. This turns the next
    occurrence into a red run instead of a discovery months later.

    Compares a directory listing rather than hooking ``open``, so it catches
    writes through any route — ``shutil``, ``os.replace``, a subprocess —
    not only the ones going through Python file objects.

    **Session-scoped deliberately.** The tree is ~12k files (the Wine
    prefixes dominate) and walking it costs ~0.7s, which per-test would cost
    more than the entire suite. Once at each end is ~1.5s total and still
    names the files that changed, which is what identifies the culprit.
    """
    live = _live_data_dir()
    before = _snapshot(live)
    # Sampled at BOTH ends: a browser that started or stopped mid-run
    # still rewrote its profile, and either sighting is enough to say
    # the suite did not.
    edge_live = _edge_is_running()
    yield
    after = _snapshot(live)
    edge_live = edge_live or _edge_is_running()
    added = sorted(
        k for k in after.keys() - before.keys()
        if not _foreign_writer(k, edge_live=edge_live)
    )
    changed = sorted(
        k for k in after.keys() & before.keys()
        if after[k] != before[k]
        and not _foreign_writer(k, edge_live=edge_live)
    )
    if not added and not changed:
        return
    pytest.fail(
        f"the test run wrote into the REAL user data directory ({live}).\n"
        "A path was almost certainly resolved at import time, before the "
        "HOME redirect — make it a call-time resolver, as "
        "``wrapper_session.prefix_index_path`` documents.\n"
        f"  created:  {added}\n"
        f"  modified: {changed}",
        pytrace=False,
    )


#: SQLite sidecars. The running plugin keeps ``playtime.db`` open, and SQLite
#: touches its ``-wal``/``-shm`` companions on a timer whether or not anything
#: of ours runs — measured advancing every 60s on an idle dev Deck with no
#: tests in flight. Attributing that to the suite makes this guard fire on
#: every run during normal on-device development, which is precisely when the
#: plugin is live, and a guard that cries wolf gets disabled. The database
#: file itself is still watched; only the sidecars are exempt.
_FOREIGN_SUFFIXES = ("-wal", "-shm", "-journal")

#: The shared Edge auth profile. A RUNNING browser owns this whole tree
#: and rewrites most of it — telemetry every few minutes, and Cookies,
#: History, Preferences and Sessions wholesale on exit. Observed
#: mid-suite with a storefront window open, which is precisely when
#: on-device development happens.
_EDGE_PROFILE_DIR = "edge-auth"


def _edge_is_running() -> bool:
    """Whether a live Edge owns the shared auth profile.

    Scans ``/proc`` for the ``--user-data-dir`` we pass. Deliberately
    NOT a blanket exemption: when no browser is running, a change under
    ``edge-auth/`` really is the suite's doing and must still fail —
    ``auth.edge_browser.cookie_writer`` writes that profile's cookie DB
    for real, and a test reaching it would be exactly the kind of
    live-data write this guard exists to catch (the docstring above
    records one such run signing the machine out of Amazon).
    """
    for entry in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            cmdline = entry.read_bytes()
        except OSError:
            continue
        if b"--user-data-dir=" in cmdline and b"/edge-auth" in cmdline:
            return True
    return False


def _foreign_writer(rel_path: str, *, edge_live: bool) -> bool:
    """Whether ``rel_path`` is written by something other than the suite."""
    if rel_path.endswith(_FOREIGN_SUFFIXES):
        return True
    return edge_live and rel_path.startswith(_EDGE_PROFILE_DIR)


def _snapshot(root):
    """``{relative path: (mtime, size)}`` for every file under ``root``."""
    if not root.is_dir():
        return {}
    snap = {}
    for path in root.rglob("*"):
        try:
            if path.is_file():
                stat = path.stat()
                snap[str(path.relative_to(root))] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            continue
    return snap


@pytest.fixture(autouse=True)
def _isolate_by_uuid_index(monkeypatch):
    """Point udev's ``by-uuid`` index at an empty per-test temp dir.

    ``mount_naming.BY_UUID_DIR`` is a module-level ``Path("/dev/disk/by-uuid")``
    and is the default for both ``uuid_by_device()`` and ``scan_mounts()``, so a
    test that builds a synthetic ``/proc/mounts`` naming ``/dev/sda1`` but does
    not pass ``by_uuid_dir=`` reads the REAL host disk index. Whether it then
    gets a name-derived id or a UUID one depends on the machine's partition
    layout, which makes the assertion non-deterministic across hosts.

    That is not hypothetical: ``test_spaced_mount_point_yields_a_path_safe_id``
    passed on both this Deck and a python:3.11 container but failed on CI's
    3.11 runner and passed on its 3.12 runner, because each matrix job gets its
    own VM and only one of them had a UUID indexed for ``/dev/sda1``. It
    expected ``ext:External_SSD`` and got ``ext:1f78b26…``.

    Patching ``BY_UUID_DIR`` itself does NOT work: it is bound as a default
    argument at import time (``by_uuid_dir: Path = BY_UUID_DIR``), so rebinding
    the module attribute leaves both defaults pointing at the real dir. What is
    resolved per call is the ``uuid_by_device`` global, so the guard goes there
    and neutralises only the unsafe default: a call that reaches for the real
    ``/dev/disk/by-uuid`` gets an empty map, which is the documented degradation
    for network shares and FUSE mounts and yields the name-derived id.

    Tests that exercise real UUID behaviour pass ``by_uuid_dir=`` a fake index
    (see ``_fake_uuid_index``) and still get the genuine lookup.
    """
    from unifideck.utils import mount_naming, mounts

    real = mount_naming.BY_UUID_DIR
    original = mount_naming.uuid_by_device

    def _guarded(root: Path = real) -> dict[str, str]:
        if Path(root) == Path(real):
            return {}
        return original(root)

    # Both namespaces: ``mounts`` imported the name directly, and other
    # callers reach it through ``mount_naming``.
    monkeypatch.setattr(mounts, "uuid_by_device", _guarded)
    monkeypatch.setattr(mount_naming, "uuid_by_device", _guarded)


@pytest.fixture(autouse=True)
def _isolate_launcher_bridge(tmp_path, monkeypatch):
    """Point the launcher→frontend bridge file at a per-test temp path."""
    # Imported inside the fixture so conftest import never depends on
    # sys.path being set up yet (pytest.ini's ``pythonpath`` handles it,
    # but collection order should not be load-bearing here).
    from unifideck.launcher import frontend_bridge

    monkeypatch.setattr(
        frontend_bridge,
        "EVENTS_FILE",
        tmp_path / "launcher_events.jsonl",
    )


@pytest.fixture(autouse=True)
def _pin_device_type(monkeypatch):
    """Pin the detected device to a Deck for every test.

    Compat facets, badges and the tab filter are resolved against the
    *running* device, which is read from DMI. That makes any assertion
    about them depend on where the suite runs: this dev Deck reports
    ``Jupiter`` and resolves the Deck track, while CI has no DMI at all
    and resolves the generic SteamOS one — silently changing which
    rating a facet carries.

    Pinning here rather than per-test means a new compat assertion
    cannot accidentally encode the developer's hardware. A test that
    wants a different device overrides this env var itself (see
    ``tests/unit/test_device_type.py``), which is also how the Machine
    path is exercised in production.
    """
    from unifideck.utils import device

    monkeypatch.setenv("UNIFIDECK_DEVICE_TYPE", "deck")
    # detect_device_type() is memoised, so a value resolved under one
    # test's patched DMI would leak into every later test.
    device.reset_cache()
