# Amazon Luna — Technical Documentation

## Table of contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Browser](#browser)
- [Authentication flow](#authentication-flow)
- [Platform spoofing (required)](#platform-spoofing-required)
- [Catalog synchronization](#catalog-synchronization)
- [Region support](#region-support)
- [Launching a Luna title](#launching-a-luna-title)
- [Controller support (Steam Deck)](#controller-support-steam-deck)
- [Virtual keyboard](#virtual-keyboard)
- [Steam Deck environment](#steam-deck-environment)
- [Logout and profile handling](#logout-and-profile-handling)
- [Error handling and status values](#error-handling-and-status-values)
- [Configuration](#configuration)
- [Security](#security)
- [What was verified, and what was not](#what-was-verified-and-what-was-not)
- [When Amazon changes the API](#when-amazon-changes-the-api)
- [Known limitations](#known-limitations)

---

## Overview

The Amazon Luna integration lets Steam Deck users **stream their Luna games** from their Steam library through the Unifideck plugin. Games are streamed from Amazon servers; nothing is installed locally.

A Luna title is an ordinary browser game of kind `stream`, routed through `launcher/browser_games.py` and `flows/browser_window.py`, reading `library_cache.json`. Luna reuses the shared browser process layer, the CDP transport, the per-flavour CDP ports and the CDP-injected virtual keyboard that the xCloud path already uses.

Two things are specific to Luna:

1. **A CDP platform spoofer.** Luna blocks Linux at game start, so the kiosk browser has to present itself as Windows.
2. **An HTTP catalog.** The library is read from Luna's own web API with the session cookies of the Luna browser profile. No browser is opened for a library sync and no page is scraped.

### What it brings to the plugin

- **Your Luna library** as Steam shortcuts, streamed in a fullscreen kiosk browser
- **Browser-session sign-in** from Steam's Quick Access Menu (QAM), once
- **Native launch** with a Play control in the game's detail page
- **Virtual keyboard** (AZERTY/QWERTY) injected via CDP
- **Controller support in Gaming Mode** with a gamepad Steam Input layout

### How Luna differs from xCloud

1. **Auth is browser-session-cookie only, not OAuth.** There is no client id, no token endpoint of ours and no token file. "Signed in" means non-expired Amazon cookies exist in the Luna browser profile. `LunaSessionMonitor` reads that cookie store.
2. **Luna blocks Linux at game start** ("Nicht unterstütztes Betriebssystem" / "Unsupported operating system"). Only the persistent platform spoofer bypasses it.
3. **Luna has its own browser profile**, separate from the `edge-auth` profile of the other stores, so there is no single sign-on with them.

Luna is also separate from the download-based Amazon Games (Prime Gaming / Nile) integration under `stores/amazon/`. Credentials and profile are not shared.

---

## Architecture

Code lives in `py_modules/unifideck/stores/luna/`:

| Module / package                       | Responsibility                                                              |
| -------------------------------------- | --------------------------------------------------------------------------- |
| `luna_store*.py`                       | `LunaStore` and its mixins: status, retry/recovery, library sync, install   |
| `luna_api/client.py`                   | `LunaApiClient`: token exchange and the `getPage` call                      |
| `luna_api/parse.py`                    | Pure parser from a `getPage` response to titles                             |
| `luna_api/regions.py`                  | Per-region constants (marketplace id, tempo host, locale)                   |
| `luna_api/errors.py`                   | `LunaApiError`, `LunaSessionError`, `LunaApiShapeError`                     |
| `luna_catalog/`                        | `LunaTitle` model and the reconciliation of titles against library entries  |
| `luna_session_monitor.py`              | Is the Luna session usable? (reads a copy of the cookie database)           |
| `luna_auth.py`, `luna_browser*.py`     | Interactive sign-in and kiosk browser composition                           |
| `luna_platform_spoofer/`               | Browser-level CDP platform spoof                                            |
| `luna_play_activator.py`, `luna_launch_flow.py` | Launch sequence                                                    |
| `luna_profile.py`                      | Luna profile directory resolve/create/writability check                     |

`auth/edge_browser/cookie_reader.py` is the counterpart of `cookie_writer.py` and decrypts the profile's cookies for the API client.

### Two processes

- **Plugin backend** (long-lived): sign-in, session monitor, library sync, shortcut reconciliation.
- **Launcher subprocess** (short-lived, spawned by Steam on Play): resolves the title through the browser-games path, opens the kiosk browser and runs the Luna launch sequence (spoofer, verify, play activation, keyboard). The spoofer and play activator live here because they attach to the browser the launcher just spawned.

The two share only files on disk: config, `library_cache.json`, the Luna profile (cookies) and the launcher log. Spoofer and play activator are constructible from `LunaConfig` alone.

---

## Browser

Luna uses the browser Unifideck 0.8.1 already manages for the other stores: a **downloaded Chromium** (`ChromeInstaller`, a pinned ungoogled-chromium build), not a system-installed browser. There is no sandbox override and nothing to configure per browser brand.

When the browser is not installed, the backend reports `needs_browser` and the frontend shows the existing browser install prompt. The install runs under a 600 s ceiling; on success the interrupted operation (sign-in or sync) resumes without user intervention.

The spoofed browser version follows the installed Chromium (see [Version tracking](#version-tracking)), and the API client derives its UA and `browserVersion` from the same pinned version constant.

---

## Authentication flow

### Browser-session sign-in

There is no OAuth exchange. `LunaAuth` opens the kiosk browser at the configured Luna sign-in page with the platform spoofer already attached. `LunaBrowserAuth` observes the signed-in state over CDP, and `LunaSessionMonitor` confirms Amazon session cookies landed in the Luna profile.

The connector polls `LunaSessionMonitor.is_usable()` until cookies appear or the **300 s** sign-in timeout fires. If the window is closed early, no cookies persisted or the timeout fires, partial state is discarded and the panel returns to not-connected.

### Session persistence

The sign-in persists in the Luna profile, `~/.local/share/unifideck/luna-profile` by default (`stores.luna.profile_dir`). Cookies there are reused for every launch and for every library sync.

`LunaSessionMonitor` is the single source of truth for "is the Luna session usable?". Because the browser locks its SQLite cookie database, it reads a temporary copy, within a 5 s budget:

- **usable**: at least one non-expired Amazon cookie, status `connected`
- **expired**: cookies absent or all expired, status `session_expired` (a re-authentication prompt, distinct from never-connected)
- **unknown**: the store could not be read in time, status `session_unknown`; the profile is left untouched and a retry is exposed

A session can also be rejected server-side while its cookies look fresh. The API client reports that as `session_expired` too (see [Catalog synchronization](#catalog-synchronization)).

---

## Platform spoofing (required)

### Why the spoofer is mandatory

Luna blocks the Linux client at game start. The decisive signal is `navigator.userAgentData.getHighEntropyValues().platform` (User-Agent Client Hints), not the User-Agent string alone, so overriding only the UA string is not enough.

`LunaPlatformSpoofer` runs as a **persistent CDP session for the whole streaming session**, attached at **browser** level. It installs a browser-level `Target.setAutoAttach { autoAttach, flatten, waitForDebuggerOnStart }` so every target (pages, future pages, service workers) is caught and gets the full override before it is released. A page-level override would die on navigation to the game page.

### What the spoofer overrides

| Override                                                | Mechanism                                                | Why                                              |
| ------------------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------ |
| UA string + full `userAgentMetadata` (platform=Windows) | `Emulation.setUserAgentOverride`                         | Defeats UA Client Hints `getHighEntropyValues()` |
| `navigator.platform` set to a Windows value             | `Page.addScriptToEvaluateOnNewDocument` (before scripts) | A page cannot read back a Linux platform         |
| WebGL `UNMASKED_RENDERER_WEBGL` set to the configured renderer | same injected script                              | A page cannot sniff a Linux GPU                  |

The values are configurable (`spoof_ua_template`, `spoof_uach_platform`, `spoof_navigator_platform`, `spoof_webgl_renderer`).

### Version tracking

The spoofed browser version must track the real installed browser. A spoofed UA whose Chrome major is older than the real one made Amazon treat the client as outdated. The spoofer reads the major version from CDP `/json/version` (the `Browser` field, `Chrome/156.x` for the pinned Chromium) at launch and substitutes it into the `{version}` placeholder of `spoof_ua_template` and the Client Hints metadata. If it cannot be determined, the launch aborts with a spoofing-configuration error rather than guessing. `LunaConfig.is_valid()` rejects a template without `{version}`.

The brand list in the Client Hints metadata stays "Google Chrome" on purpose; it is part of the spoof.

### Verification and failure

After the detail page loads and before the play click, `verify()` evaluates `navigator.userAgentData.getHighEntropyValues(['platform'])` and the launch flow asserts it equals the configured Windows value. A mismatch, a CDP attach failure or an undeterminable browser version maps to `platform_spoofing_failure`: the launch aborts, the reported value is logged, the launcher exits non-zero.

---

## Catalog synchronization

Sync retrieves the account's Luna titles over HTTP and reconciles them against the Luna entries in the library. **It never opens, attaches to or scrapes a browser.** The flow in `LunaStore.get_library`:

1. Check the browser is installed (`needs_browser` otherwise).
2. Check the session is usable (`not_connected` / `session_expired` otherwise; existing entries are kept).
3. Resolve the region for `stores.luna.luna_host` (see [Region support](#region-support)).
4. Fetch the library through `LunaApiClient.fetch_library()` under the store's retry wrapper (up to 4 attempts).
5. Reconcile the titles with the existing entries.

### The two HTTP calls

**1. Access token.** The client reads the Amazon cookies for the region's cookie domain (for example `amazon.de`) from the Luna profile with `cookie_reader.read_cookies`, which copies the cookie database to a private temp directory, decrypts `v10` values (the browser runs with `--password-store=basic`), skips `v11` or undecodable rows and deletes the copy. It then sends

```
GET https://{luna_host}/token        (Cookie header, Windows Chrome User-Agent)
```

The endpoint answers HTTP 200 either way. The body is `{"invalidReason": ..., "token": ...}`. A non-empty `invalidReason` or an empty `token` is a rejected session (`LunaSessionError`).

**2. Library page.**

```
POST https://{tempo_host}/getPage    Content-Type: text/plain;charset=UTF-8
```

The body (one constant, `GET_PAGE_BODY` in `luna_api/client.py`) asks for `searchContext.query = "my_games"` with `featureScheme = "WEB_V1"` and `pageContext.pageType = "multistate_browse_results"`. HTTP 401/403 is a rejected session; other 4xx, 5xx, timeouts (20 s total) and bad JSON are `LunaApiError`.

Headers sent: `Content-Type`, `Accept`, `User-Agent` (Windows Chrome of the pinned major), `Origin` and `Referer` (`https://{luna_host}/`), `x-amz-access-token`, `x-amz-locale`, `x-amz-marketplace-id`, `x-amz-country-of-residence`, `x-amz-device-type: browser`, `x-amz-platform: web`. `x-amz-locale` is required: `getPage` answers HTTP 400 without it.

Not sent, because they are bound to a browser session and unavailable outside the browser: `x-amz-tdi`, `x-amz-session-id`, `x-amz-device-serial-number`, `x-amz-profile-token`. Also not sent: `x-amz-react-version`, `x-amz-client-version`, `x-amz-timezone`.

> **The header set above is confirmed live** (the Steam Deck sync stored 88 titles with it). It was not reduced one header at a time, so which of these headers are optional besides the required `x-amz-locale` is not known.

### Response shape

The response is a page of widgets. The library is the `GRID` widget with `id == "collection_browse_my_games"` (falling back to the first `GRID` whose children are `GAME_TILE`). Each child is a `GAME_TILE`:

- `presentationData` is a JSON **string** containing, among other things, `gameId` and the title
- the first entry of `actions` has a `target` of the form `/game/<slug>/<ASIN>`; a few titles only have `/game/<slug>`
- `hoverDetails.productUrl` carries `asin=` and `territory=`

`browser_url` is `{luna_host}{target}`; the product id is `presentationData.gameId` (or the tile id without the `game_tile_` prefix). The parser does not require an ASIN. A tile without id, title or `/game/` target is counted as malformed and skipped. A response with no grid at all raises `LunaApiShapeError`; it is deliberately **not** treated as an empty library, so Amazon changing the format cannot wipe the user's entries.

Entries get the tags `browser` and `luna` and `metadata["browser_url"]`. On launch, `launcher/browser_games.browser_target` reads that record and returns the URL with `kind="stream"` for a `store == "luna"` title. The product id stays the entry's store id, so existing entries keep their identity and have their URL updated in place.

### Status mapping

| API outcome                                          | Result                                          | Status              |
| ---------------------------------------------------- | ----------------------------------------------- | ------------------- |
| No usable session                                    | none; existing entries kept                     | `not_connected` / `session_expired` |
| `LunaSessionError` from token or `getPage`           | none; existing entries kept                     | `session_expired`   |
| Luna host has no region row                          | `[]` (authoritative) and a `region_unavailable` skip event | `region_unavailable` |
| Timeout, 5xx, other 4xx, bad JSON, `LunaApiShapeError` | retried, then none; existing entries kept      | `sync_error`        |
| 200, shape ok, at least one valid tile               | authoritative list                              | `connected`         |
| 200, shape ok, zero tiles                            | `[]` (removes Luna entries)                     | `connected`         |
| 200, some tiles malformed                            | valid titles plus the previous entries carried forward | `connected`  |

### Prime versus subscription

Every tile in the sample is a "Play now" tile. There is no per-tile field that separates titles included in Prime from titles that need a Luna subscription, so the library is treated as "what this account can play". The old DOM text marker ("In Prime enthalten") is gone with the scraper and has no API equivalent that was seen.

---

## Region support

Each Luna country site uses its own marketplace id, default locale and a regional "tempo" backend. These live in one frozen table in `luna_api/regions.py`, keyed by the Luna host. Every value was read from the site's own page config: `https://<luna host>/?g=s` serves `marketplace: "…"`, `locale: "…"` and names the tempo host it calls (read on 2026-11-16; `?g=s` stops Luna's geo redirect, see below).

| Luna host            | Cookie domain   | Country | Marketplace id   | Default locale | Tempo host                                   | Tested with an account |
| -------------------- | --------------- | ------- | ---------------- | -------------- | -------------------------------------------- | ---------------------- |
| `luna.amazon.de`     | `amazon.de`     | DE      | `A1PA6795UKMFR9` | `de_DE`        | `proxy-prod.eu-west-1.tempo.digital.a2z.com` | **yes**                |
| `luna.amazon.com`    | `amazon.com`    | US      | `ATVPDKIKX0DER`  | `en_US`        | `proxy-prod.us-east-1.tempo.digital.a2z.com` | no                     |
| `luna.amazon.ca`     | `amazon.ca`     | CA      | `A2EUQ1WTGCTBG2` | `en_CA`        | `proxy-prod.us-east-1.tempo.digital.a2z.com` | no                     |
| `luna.amazon.co.uk`  | `amazon.co.uk`  | GB      | `A1F83G8C2ARO7P` | `en_GB`        | `proxy-prod.eu-west-1.tempo.digital.a2z.com` | no                     |
| `luna.amazon.fr`     | `amazon.fr`     | FR      | `A13V1IB3VIYZZH` | `fr_FR`        | `proxy-prod.eu-west-1.tempo.digital.a2z.com` | no                     |
| `luna.amazon.it`     | `amazon.it`     | IT      | `APJ6JRA9NG5V4`  | `it_IT`        | `proxy-prod.eu-west-1.tempo.digital.a2z.com` | no                     |
| `luna.amazon.es`     | `amazon.es`     | ES      | `A1RKKUPIHCS9HS` | `es_ES`        | `proxy-prod.eu-west-1.tempo.digital.a2z.com` | no                     |
| `luna.amazon.pl`     | `amazon.pl`     | PL      | `A1C3SOZRARQ6R3` | `pl_PL`        | `proxy-prod.eu-west-1.tempo.digital.a2z.com` | no                     |
| `luna.amazon.nl`     | `amazon.nl`     | NL      | `A1805IZSGTT6HS` | `nl_NL`        | `proxy-prod.eu-west-1.tempo.digital.a2z.com` | no                     |
| `luna.amazon.se`     | `amazon.se`     | SE      | `A2NODRKZP88ZB9` | `sv_SE`        | `proxy-prod.eu-west-1.tempo.digital.a2z.com` | no                     |
| `luna.amazon.com.be` | `amazon.com.be` | BE      | `AMEN7PMS3EDWL`  | `nl_NL`        | `proxy-prod.eu-west-1.tempo.digital.a2z.com` | no                     |

Other Amazon domains redirect: `luna.amazon.pt` → ES, `luna.amazon.at` → DE, `luna.amazon.ie` and `luna.amazon.com.mx` → US.

### Which region is used

The region is not a setting. Luna picks the country by the visitor's location (from Germany every `luna.amazon.*` host answers `302 → https://luna.amazon.de/?g=s`), and after sign-in the account's Amazon session cookies (`at-*`, `sess-at-*`) sit on that country's domain. So `stores.luna.luna_host` defaults to empty ("auto"):

1. Signed in: the region whose cookie domain holds an unexpired sign-in cookie (`detect_region`, which reads cookie names and hosts only, never values).
2. Not signed in yet: `https://luna.amazon.com`, which Luna geo-redirects to the user's own country. The sign-in check accepts any Luna country host, so a redirected sign-in still completes.

A non-empty `luna_host` overrides the detection.

### Language

Luna offers the same 13 languages on every country site (Settings → Language). The web client stores the choice in `localStorage["locale_override"]` (`de_DE` style) and the catalog API takes the same tag in `x-amz-locale`; `getPage` was verified to answer the full library for `de_DE`, `en_US` and `fr_FR`. unifideck follows its own UI language: an exact match, else the same language in the country's variant (French in Canada → `fr_CA`), else any variant of that language, else the country site's default (`luna_locale.py`). The kiosk value is written before Play together with the streaming quality (see [Launch flow](#launch-flow)).

---

## Launching a Luna title

### Launch flow

On Play, the launcher resolves the browser-game entry, opens the kiosk browser and runs `luna_launch_flow.run_luna_browser_game`:

1. **Check** the browser is installed (the generic browser-game check; a missing browser raises the browser-required toast).
2. **Spawn** the Luna kiosk via `LunaBrowser` at the title's URL.
3. **Attach** `LunaPlatformSpoofer` browser-level on `browser_game_cdp_port()`. Its CDP session stays open until the window closes: Chrome drops a session's `Emulation.setUserAgentOverride` and pre-document scripts as soon as that session detaches.
4. **Verify** the spoofed platform; a mismatch aborts with `platform_spoofing_failure`.
5. **Page settings**: write the streaming quality (`luna_video_quality_setting`: LIMITED / STANDARD / BEST, from the settings panel) and the language (`locale_override`, see [Language](#language)) into the page's `localStorage`, and reload once when one of them changed (`luna_page_settings.py`). Luna reads both once per page load; the values then persist in the profile. A failure is logged and never blocks the launch.
6. **Activate** play via `LunaPlayActivator`: within 15 s, click the detail page's primary Play button (`primary_entitled_button_game_session_async`) with a trusted CDP pointer event (Luna ignores a scripted `click()`), confirm the pre-launch dialog Luna raises without hardware acceleration ("Hardware Acceleration is Off" / "Hardwarebeschleunigung ist aus"; found language-neutrally by its `chrome://settings` hint, its last button is Continue Anyway), then wait up to 180 s for `/player` with the `<video>` playing (Luna provisions for ~30 s first). Not reaching `/player` maps to `launch_failure`; once `/player` is reached a slow stream is not a failure, the window stays open and the spoof is held until it closes.
7. **Inject** the virtual keyboard over the same CDP session.
8. **Block** on the shared `wait_for_browser_exit(..., log_tag="luna")` until the window closes.

The launcher rejects a launch with a non-zero exit when the `browser_url` is empty or malformed, its host is not in `allowed_stream_hosts`, or the SteamOS X11 authority file cannot be found.

### Browser flags

`LunaBrowser` composes the shared browser process layer with Luna flags: fullscreen kiosk, `--user-data-dir=<Luna profile>`, `--remote-debugging-port=<browser_game_cdp_port()>`, the `gamepad_flags`, `--autoplay-policy=no-user-gesture-required`, and the window class `unifideck-luna`.

`--webrtc-ip-handling-policy=default` is required: Luna streams over WebRTC, and the plugin's ungoogled-chromium gathers no usable ICE candidates without it (only two `host/tcp` candidates, no UDP or STUN). The peer connection then stays at ICE `new`, the video track stays muted and the player shows "Loading" forever. Verified on a Steam Deck with the same account both ways: without the flag nothing connects after 40 s; with it the connection is up about 40 s after Play (800x450 video, track live).

### Dedicated Luna profile (no SSO)

Luna uses its own profile, separate from `edge-auth`. There is deliberately no single sign-on with xCloud, GOG, Ubisoft or Amazon Games. If `profile_dir` is unset the shared browser-game profile dir is used, but a dedicated dir (the default) is recommended so the spoofed Amazon session does not mix with other browser-game cookies.

The profile directory may have been created by an earlier browser build. Chromium keeps the same cookie scheme (`v10`, fixed key) across these versions, so an existing profile keeps working.

---

## Controller support (Steam Deck)

Luna controller input works in **Gaming Mode with a gamepad Steam Input layout on the shortcut**. It is not supported in Desktop Mode. The gamepad browser flags are set before the page loads, and the Deck controller reaches the page as an Xbox-360-style pad through the gamepad layout.

When a Luna shortcut becomes the active launch target, `LunaStore.request_steam_input_layout` best-effort requests a gamepad layout within 5 s, reusing `refresh_steam_controller_layout` (the xCloud path does the same).

### Manual layout selection

If the automatic layout does not apply within 5 s, set it once by hand:

1. In Gaming Mode, open the Luna game's page in your Steam library.
2. Press the controller / gear icon.
3. Choose **Controller Layout** (or **Controller Settings**).
4. Open **Templates** and pick a **Gamepad** template (for example **Gamepad With Joystick Trackpad**).
5. Apply it. The choice persists in Steam Cloud.

---

## Virtual keyboard

The Deck has no physical keyboard and Steam's overlay keyboard is not available because the browser runs outside Steam. A virtual keyboard is injected via CDP over the same browser-game port the spoofer and play activator use.

It shows within 500 ms of a text field gaining focus (`focusin`) and hides on blur. Layout: **AZERTY** for `fr` locales, **QWERTY** otherwise and as the default. A missing CDP dependency aborts the launch and is logged.

---

## Steam Deck environment

The plugin runs inside PluginLoader, which has no graphical environment, so the launcher injects the missing variables via the shared `clean_env()`:

| Variable                   | Value                           | Reason                               |
| -------------------------- | ------------------------------- | ------------------------------------ |
| `DISPLAY`                  | `:0`                            | X11 session                          |
| `XDG_RUNTIME_DIR`          | `/run/user/1000`                | Bus socket                           |
| `DBUS_SESSION_BUS_ADDRESS` | `unix:path=/run/user/1000/bus`  | D-Bus                                |
| `XAUTHORITY`               | `/run/user/1000/xauth_*` (glob) | X11 auth (randomly named on SteamOS) |
| `GTK_MODULES`              | `""`                            | Suppress canberra warnings           |

If the xauth file cannot be found, the launcher exits non-zero and records it. `LD_LIBRARY_PATH` and `LD_PRELOAD` are scrubbed.

---

## Logout and profile handling

Connect and disconnect are done from the plugin (QAM, Luna, Sign in / Logout). On logout the kiosk is killed first (it owns the cookie database), then `LunaSessionMonitor.clear_cookies()` clears the Amazon cookies in the Luna profile within 5 s using the shared `cookie_writer.py` helper. If clearing fails, the previous session state is retained and reported instead of leaving a half-cleared profile.

---

## Error handling and status values

Every failure point maps to a distinct state so the panel never gets stuck. Network and API operations are retried behind a `recovery_in_progress` indicator, each failure is logged with a timestamp, operation name and description, and the last known stable state is restored before a retry action is offered.

Status values Luna surfaces:

| Status                       | Meaning                                                              |
| ---------------------------- | -------------------------------------------------------------------- |
| `connected`                  | A usable session exists and the last sync succeeded                  |
| `not_connected`              | No session; first sign-in needed                                     |
| `session_expired`            | Cookies absent/expired, or the token/`getPage` call rejected them    |
| `session_unknown`            | The cookie store could not be read in time; retry offered            |
| `needs_browser`              | The plugin's Chromium is not installed; install prompt               |
| `sync_error`                 | The library could not be read (timeout, HTTP error, unexpected shape) after all retries; entries kept |
| `region_unavailable`         | The configured Luna host has no region row                           |
| `platform_spoofing_failure`  | The CDP platform spoof failed or verification mismatched             |
| `launch_failure`             | A launch did not reach the player                                    |
| `recovery_in_progress`       | A retry/recovery cycle is running                                    |

---

## Configuration

Parameters are read from `settings.json` under the `stores.luna` prefix. There are no OAuth keys and no `games.map` routing key.

| Key                        | Usage                                                      |
| -------------------------- | ---------------------------------------------------------- |
| `luna_host`                | Luna site URL; default empty = auto (signed-in region, see [Which region is used](#which-region-is-used)) |
| `video_quality`            | Streaming quality LIMITED / STANDARD / BEST; empty = Auto (Luna's own value). Set from the settings panel |
| `allowed_stream_hosts`     | Host allow-list for launch URLs (stream kind)              |
| `signin_path`              | Luna sign-in page path                                     |
| `player_path`              | The shared `/player` path                                  |
| `profile_dir`              | Luna profile dir (default `~/.local/share/unifideck/luna-profile`) |
| `browser_game_cdp_flavour` | Resolves the browser-game CDP port                         |
| `cloud_browser_flags`      | Kiosk / cloud-gaming browser flags                         |
| `gamepad_flags`            | Gamepad browser flags                                      |
| `spoof_ua_template`        | UA template with a `{version}` placeholder                 |
| `spoof_uach_platform`      | UA Client Hints platform (Windows)                         |
| `spoof_navigator_platform` | `navigator.platform` override                              |
| `spoof_webgl_renderer`     | WebGL unmasked renderer override                           |

`LunaConfig.is_valid()` names any missing, empty or malformed key. The API constants (endpoints, marketplace ids, request body) are intentionally **not** settings; they live in code next to their tests (see [When Amazon changes the API](#when-amazon-changes-the-api)).

---

## Security

- No OAuth tokens exist for Luna; the persisted secret is the Amazon session cookies in the Luna profile.
- The API client reads those cookies from a private temp copy of the cookie database and deletes the copy. It never logs the token or any cookie value; the access token only lives in memory for one sync.
- The Luna profile is dedicated; there is no SSO with other stores.
- The spoofer runs browser-level and only overrides OS-identity signals; it does not touch Amazon credentials.
- The test fixture is sanitized (see below) and a test asserts it holds no tokens, session ids or cookie names.

---

## What was verified, and what was not

### Verified

- **DE account, 88 titles.** A real replay of the token call and `getPage` against `luna.amazon.de` returned HTTP 200 with 88 tiles (88 distinct ids), one `GRID` widget `collection_browse_my_games`, all `GAME_TILE`, each with one action whose primary button reads "Play now". 87 targets look like `/game/<slug>/<ASIN>`, 1 is `/game/fortnite` without ASIN. That response is the basis of the parser and of the sanitized fixture `tests/fixtures/luna/getpage_my_games.json` (6 tiles).
- `getPage` answers 400 without `x-amz-locale`.
- Every region row against its site's own page config (see [Region support](#region-support)).
- `getPage` returns the full library for `x-amz-locale` `de_DE`, `en_US` and `fr_FR`.
- On device: the language switch via `locale_override` ("Jetzt spielen"), the language-neutral pre-launch dialog in English and German, and the quality being read back from the profile.
- Unit tests cover the parser, the client against an injected HTTP session (URL, body, headers, error mapping), the cookie reader (round trip with `cookie_writer`), the region table, and the sync status mapping. They do not talk to Amazon.

### Not verified / open points

| Point                                   | Status                                                                                           |
| --------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Reduced header set                      | **Accepted.** The library sync on the Steam Deck ran with exactly the headers listed above (no `x-amz-tdi` / session id / device serial / profile token) and stored 88 titles. Which of the sent headers are optional beyond `x-amz-locale` (required) was not narrowed down. |
| Live run of the final client            | **Done** on the Steam Deck: `LunaApiClient` synced 88 titles into `library_cache.json`.        |
| Prime versus subscription marker        | Absent. No field in `GAME_TILE` carries it; the library is treated as "playable by the account". |
| Account without a Luna subscription    | No sample. An empty grid is treated as zero titles (entries removed); an auth error as `session_expired`. |
| Pagination                              | None observed: no cursor or next-page field; the widget-level `serviceToken` is a page-context token. The client does a single request and logs a WARNING (counts only) if a grid holds non-tile widgets or non-`/game/` actions. |
| Title count mismatch                    | An earlier DOM probe of the same account counted 90 `game_tile` elements; the API returned 88. The 2 extra are unexplained (possibly non-library tiles in the DOM). |
| Regions other than DE                   | Values read from each site's page config (see table), not exercised with an account there.     |
| Widevine                                | The downloaded ungoogled-chromium ships without Widevine. Whether Luna's player needs it is untested. |
| Sign-in window class                    | Launch is tagged `unifideck-luna` through `wait_for_browser_exit`. The sign-in flow (`luna_auth.py`) does not go through it, so under a Steam Play compat tool that window may stay hidden in Gaming Mode. Not changed here. |
| Spoofer attach hang                     | A previously seen `attached_targets()` hang affected the old scrape. Sync no longer launches a browser so it cannot occur there; it may still matter in the launch flow. |

---

## When Amazon changes the API

The integration depends on two undocumented calls, so expect breakage. Symptoms and handling:

- **Shape change.** If `getPage` returns JSON without the expected grid, `parse_library_page` raises `LunaApiShapeError`. The store retries, then reports `sync_error` and **keeps the existing library entries**. Nothing is deleted on drift.
- **Auth change.** A `/token` response with `invalidReason` or HTTP 401/403 from `getPage` shows as `session_expired`. If a fresh sign-in does not fix it, the token call changed.
- **HTTP 400.** Usually a missing or renamed required header; the log line has the status only.

### Capturing a fresh `getPage` call

1. Start the Luna kiosk with the Luna profile (sign in if needed) so it listens on the browser-game CDP port (`browser_game_cdp_port()`).
2. Attach to a page target over CDP, enable the `Network` domain (`Network.enable`) and open the "my games" view (`{luna_host}/browse?quick_search=my_games`).
3. Record the `Network.requestWillBeSent` events for the `getPage` request (URL, headers, `postData`) and the matching `Network.getResponseBody`.
4. Compare with the constants below and update them.

Treat the capture as secret material: it contains the access token, cookies and session ids. Keep it out of the repository, logs and issues.

### Where the constants live

- `py_modules/unifideck/stores/luna/luna_api/client.py`: `GET_PAGE_BODY`, `BROWSER_VERSION`/`USER_AGENT`, the `/token` and `getPage` URLs, `get_page_headers()`.
- `py_modules/unifideck/stores/luna/luna_api/regions.py`: marketplace ids, locale, country, tempo host per Luna host.
- `py_modules/unifideck/stores/luna/luna_api/parse.py`: grid id (`collection_browse_my_games`), tile type (`GAME_TILE`), id prefix and path prefix.

### Refreshing the test fixture safely

`tests/fixtures/luna/getpage_my_games.json` is a trimmed real response. To refresh it:

1. Save the capture outside the repository.
2. Keep a handful of tiles, including one `/game/<slug>` target without ASIN. Titles, slugs and ASINs are public catalog data.
3. Replace every `serviceToken`, `searchId` and similar page-context value with a fixed dummy string. Strip access tokens, cookies, session ids and `tdi` values entirely.
4. Run `tests/unit/test_luna_api_parse.py`; its fixture check fails if anything token-, session- or cookie-like remains.
5. Format with `pnpm run check:prettier`.

Never copy values out of the profile's cookie database into the repository.

---

## Known limitations

1. **Streaming only.** No games are downloaded or installed.
2. **The platform spoofer is required.** Without it every launch fails with an unsupported-OS error.
3. **The spoofed version must track the installed browser.** It is derived from the live browser on each launch.
4. **No single sign-on.** The Luna profile is separate from the other stores.
5. **Controller works in Gaming Mode only,** with a gamepad Steam Input layout on the shortcut.
6. **Catalog depends on undocumented endpoints.** See [When Amazon changes the API](#when-amazon-changes-the-api) and the open points above.
7. **Only DE is verified.** Other regions are best-effort.
8. **Network quality.** Streaming depends on the Internet connection.
