# The game runs, but stays behind the Gaming Mode loading screen

A recurring, recognisable failure with more than one cause. The game **is**
running: you hear its audio, and in Desktop Mode it displays normally. In
Gaming Mode you get Steam's loading/launch screen forever, usually with only an
*Abort* button.

This document is the diagnosis path and the standing rules that keep it fixed.
Read it before changing anything in the launch path, the window environment, or
the AppOverview patches.

---

## Why Gaming Mode is different

In Gaming Mode, Steam runs inside **gamescope**, which decides what to show. A
window is only eligible to be focused once gamescope can attribute it to the app
Steam is waiting on. Two things drive that:

1. **Window environment** — `build_steam_window_env` in
   `launcher/proton/infrastructure/core.py` stamps the Steam window identity
   into the game's environment. This is what makes adoption work in the ordinary
   case.
2. **The `STEAM_GAME` property** — `launcher/proton/infrastructure/gamescope_window_tagger.py`
   sets `STEAM_GAME=<appid>` on windows whose `WM_CLASS` is `steam_app_<appid>`,
   on the gamescope X display (`:0`, not the nested `:1` the Steam client draws
   on). Setting it puts the app in gamescope's `FOCUSABLE_APPS` and switches
   focus immediately.

   umu has its own `monitor_windows` that would do this, but it only runs when
   `is_steammode` is true, which requires `container=flatpak`. We run under
   `container=pressure-vessel`, so umu skips it and the tagger is the backstop.

Desktop Mode has no gamescope compositor, so neither mechanism matters there.
**That asymmetry is the tell**: "fine on the desktop, stuck in Gaming Mode"
points at focus/identity, never at Proton, the prefix, or the game itself.

---

## Diagnose it: work from evidence, in this order

Both known causes are silent. Neither crashes, and one of them logs a single
WARNING that is easy to scroll past. Do not guess between them — each has a
distinct fingerprint.

### 1. Was the window ever tagged?

```bash
grep -i 'gamescope_tagger' ~/.local/share/unifideck/launches/<id>.log
```

| What you see | Meaning |
|---|---|
| `STEAM_GAME=<appid> set on wid=0x...` | Tagging worked. Not this cause. |
| `no umu zipapp among ... cannot tag` | **Cause A** — the tagger could not load Xlib. |
| `cannot open display :0` | The tagger ran outside a gamescope session, or `:0` is wrong here (override with `UNIFIDECK_GAMESCOPE_DISPLAY`). |
| `started for appid=...` and nothing after | The watch loop matched no window; check the appid below. |
| no lines at all | The tagger never started: `GAMESCOPE_WAYLAND_DISPLAY` was unset (not Gaming Mode) or `SteamGameId` was 0. |

### 2. Is Steam tracking the shortcut under a *foreign* appid?

```bash
grep -i 'fossilize\|steam app id' ~/.local/share/unifideck/launches/<id>.game.log
```

The Fossilize shader-cache path must contain **the shortcut's own appid**:

```
shadercache/3759543982/fozpipelinesv6/...     <- correct: our shortcut
shadercache/223810/fozpipelinesv6/...         <- WRONG: a real Steam app's id
```

A foreign id there, or a gamescope surface reporting `steam app id: 0`, is
**Cause B**. The loading screen is then waiting on a window belonging to an app
that never started, so no amount of tagging will help.

---

## Cause A — the window was never tagged

**Root cause (found 2026-09-17):** the tagger imports `Xlib`, which is not a
dependency of this plugin; it rides inside the bundled umu zipapp, which goes on
`sys.path` for the duration of the import. The lookup asked for
`bin/umu/umu/umu_run.py` — and in the source tree **that is a symlink to
`umu-run`, which the plugin packager does not preserve**. Every built plugin
ships `umu-run` alone, so the lookup found nothing on every real install while
passing in a checkout, where the symlink resolves.

No zipapp → no Xlib → nothing tagged → nothing focused. One WARNING, then the
launch carried on and sat behind the loading screen.

**Fix:** `_find_umu_zipapp` now tries the real archive (`umu-run`) first, accepts
the symlink name as a fallback, and confirms with `zipfile.is_zipfile` that what
it found is something `sys.path` can import from.

Guarded by `tests/unit/test_gamescope_window_tagger.py`, whose load-bearing case
builds a plugin root shaped like a **packaged** install — `umu-run` only. A test
written from the repo layout passes throughout this bug, which is exactly why it
survived.

## Cause B — a foreign AppOverview identity

Covered in depth by the store-patch rules: the AppOverview getters in
`src/lib/steam-bridge/app-store-patcher.ts` must **borrow content, never
identity**. Returning another app's `AppOverview` (or a synthesised one carrying
its `appid`/`GameID`) makes Steam resolve our shortcut to that app, so it tracks
a game that never started.

Scope is every Unifideck shortcut for a title the user also owns on Steam. Copy
store fields onto the shortcut's own object, re-stamp `unAppID` when borrowing
`AppDetails`, and keep the `getShortcutRunGameId` guard that only trusts a stored
`gameid` when `BigInt(gameid) >> 32n === BigInt(appid)`.

---

## Standing rules

1. **Verify every runtime path against the BUILT plugin, never the checkout.**
   The packager drops symlinks and dev-only files. A path that resolves in
   `git` and not in `~/homebrew/plugins/Unifideck/` is the exact shape of this
   bug. `bin/umu/umu/umu_run.py` is currently the only tracked symlink; if
   another is added, code must not depend on it by name.
2. **Ask a file what it is, not what it is called.** Confirm an archive with
   `zipfile.is_zipfile`, an executable with `os.access(..., os.X_OK)`. Name
   checks pass on a stub and fail on a rename.
3. **A degradation that only logs a WARNING is a silent failure.** If losing a
   component costs the user something visible, say so in the message, name where
   it looked, and give it a test that runs against the packaged layout.
4. **"Works in Desktop Mode, broken in Gaming Mode" is a focus/identity bug.**
   Start with this document; do not re-triage Proton, prefixes or the game.
5. **Never spoof identity on an `AppOverview`.** Borrow content only — see
   Cause B and `docs/STEAM_UI_PATCHING_REFERENCE.md`.

## Related

- `docs/ui-injection.md`, `docs/STEAM_UI_PATCHING_REFERENCE.md` — the patching rules behind Cause B.
- `docs/proton-compatibility.md` — Proton selection, and the Warcraft III crypt32 workaround.
- `launcher/proton/infrastructure/gamescope_window_tagger.py` — the tagger, with the failure recorded in its own docstring.
