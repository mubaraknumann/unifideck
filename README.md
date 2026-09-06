# Unifideck - Unified Game Library for Steam Deck

A Decky Loader plugin that brings together Steam, Epic Games Store, GOG, Amazon Games, Ubisoft Connect, Battle.net, and Xbox Cloud Gaming in a single library experience on your Steam Deck.

![License](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)
![Platform](https://img.shields.io/badge/platform-Steam%20OS-orange.svg)
![Downloads](https://img.shields.io/github/downloads/mubaraknumann/unifideck/total.svg?label=downloads&color=brightgreen)
[![Sponsor](https://img.shields.io/badge/Sponsor-GitHub-ea4aaa?logo=github&logoColor=white)](https://github.com/sponsors/mubaraknumann) [![Ko-fi](https://img.shields.io/badge/Ko--fi-F16061?logo=ko-fi&logoColor=white)](https://ko-fi.com/mubaraknumann)

## Table of Contents

- [Features](#features)
- [Screenshots](#screenshots)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Getting Started](#getting-started)
- [Documentation](#documentation)
- [Known Limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Languages](#languages)
- [Building](#building)
- [Tech Stack](#tech-stack)
- [Credits](#credits)
- [Support](#support)
- [License](#license)
- [Author](#author)
- [Disclaimer](#disclaimer)

## Features

Unifideck brings your games from other stores into Steam, so they show up in your library and play just like Steam games — same Play button, same Gaming Mode, same controller-friendly feel. No switching to desktop mode, no juggling separate launchers.

- **All your games in one place** — Your Epic, GOG, Amazon, Ubisoft, Battle.net, and Xbox titles sit right alongside your Steam library, in tabs you can browse like any other.
- **Install and play like a Steam game** — Press Install, watch the progress, then press Play — all from the game's page in Gaming Mode, exactly the way Steam games work.
- **Install games wherever you like** — Internal storage, an SD card, or a folder you pick yourself.
- **Proton handled for you** — Most Windows games just work; Unifideck sets up a recent Proton automatically. If a game is picky, you can force a specific version from Steam's usual Compatibility menu.
- **Cover art and real game info** — Cover images, icons, Metacritic scores, and Steam Deck compatibility ratings, so your non-Steam games look like they belong.
- **Cloud saves for Epic and GOG** — Your progress syncs with the store's cloud and follows you between devices, with a heads-up if your local and cloud copies ever disagree.

## Screenshots

### Unified Game Library

<img width="1920" height="1080" alt="Screenshot_20260109_123258" src="https://github.com/user-attachments/assets/58aafad6-5c54-475d-a309-c44f77895b72" />

### Game Details

![20260104022821_1](https://github.com/user-attachments/assets/afc0922e-aace-4d47-925e-1bc7f1e48140)

## Prerequisites

- **Decky Loader** must be installed on your Steam Deck.
- **Microsoft Edge** is required for store sign-in and Xbox Cloud Gaming. If it is missing, Unifideck will prompt you to install it.
- All other store CLIs and helper tooling are bundled with the plugin.

[Decky Loader Installation Guide](https://github.com/SteamDeckHomebrew/decky-loader)

## Installation

1. Download the latest plugin ZIP from the [Releases](https://github.com/mubaraknumann/unifideck/releases) page.
2. Open **Quick Access Menu** (three dots button).
3. Navigate to **Decky** -> **Settings** (gear icon).
4. Enable **Developer Mode** if it is not already enabled.
5. Click **Install Plugin from ZIP**.
6. Select the downloaded ZIP file.

If an update gets stuck on `installing plugin`, uninstall the current Unifideck plugin and install the latest ZIP again.

https://www.youtube.com/watch?v=lP-90uYd72w

## Getting Started

1. Open the **Quick Access Menu** and launch **Unifideck**.
2. Connect the stores you want to use.
3. Set your default install location if you want internal storage, SD card, or a custom path.
4. Run **Sync Libraries** or **Force Sync**.
5. Restart Steam when prompted so new shortcuts and artwork are applied.
6. For Ubisoft titles purchased through Epic, complete the one-time account link at [epicgames.com/id/link/ubisoft](https://epicgames.com/id/link/ubisoft).

Installed games are playable immediately after install. The Steam restart is still needed after sync or cleanup so the library refreshes fully.

## Documentation

**User guides**

- **[FAQ](docs/faq.md)** - Common issues, workarounds, and version-specific fixes collected from releases, code comments, and GitHub issues.
- **[Launch Options](docs/launch-options.md)** - Environment-variable tweaks, what's supported, and what's planned.
- **[Proton Compatibility](docs/proton-compatibility.md)** - Choosing a Proton version and troubleshooting/quick fixes.
- **[Cloud Saves](docs/cloud-saves.md)** - How Epic/GOG cloud sync works, conflict handling, and custom save paths.
- **[Microsoft / xCloud](docs/microsoft-xcloud.md)** - Xbox Game Pass and cloud-streaming integration.

**Developer / reference**

- **[Architecture & Build](docs/architecture.md)** - The layered backend, EventBus, RPC mixins, and build flow.
- **[Ubisoft Store Spec](docs/ubisoft-store-spec.md)** - How the Ubisoft Connect integration works end to end.
- **[UI Injection](docs/ui-injection.md)** & **[Steam UI Patching Reference](docs/STEAM_UI_PATCHING_REFERENCE.md)** - How Unifideck patches Steam's React UI.
- **[CONTRIBUTING](CONTRIBUTING.md)** - PR process and contribution guidelines.

## Known Limitations

- Unifideck replaces Steam's default **All Games**, **Installed**, and **Great on Deck** tabs, so some sort and filter behavior is not preserved.
- With **[TabMaster](https://github.com/Tormak9970/TabMaster)** installed, Unifideck skips custom tab injection and relies on `[Unifideck]` collections instead.
- Steam still needs a restart after sync or cleanup so new shortcuts and artwork fully apply.
- Xbox Cloud Gaming support is **streaming-only** and depends on **Microsoft Edge**.
- Cloud saves currently cover **Epic** and **GOG** only, and game-level support varies.
- Some titles still need manual Proton experimentation or store-specific workarounds.
- **Proton version and launch options are configured through Steam's native shortcut Properties** (Compatibility tab / Launch Options field) — there is no in-plugin picker. Wrapper-style launch options and LSFG are **not yet wired up**; see the [Launch Options guide](docs/launch-options.md).
- Not every game has SteamGridDB artwork or complete metadata.
- For **Ubisoft**, choose your Proton version **before** installing. Changing Proton after install can invalidate the prefix and force a reinstall. See the [Ubisoft store spec](docs/ubisoft-store-spec.md) for details.

## Troubleshooting

For a longer list of release-specific problems and fixes, see the **[FAQ](docs/faq.md)**.

### Install Stuck on `installing plugin`

Uninstall the current plugin and install the latest ZIP again. This was the recommended workaround for the 0.6.0 -> 0.6.1 transition.

### Games or Artwork Do Not Appear After Sync

Run **Force Sync** if needed, then restart Steam when prompted so shortcuts and artwork are reloaded.

### Epic Login Shows a Blank Page or `Pretty Print`

Sign into Epic in a regular browser first, accept any pending legal updates, then retry in Unifideck.

### A Game Will Not Install or Launch

Check available storage, make sure the store account is still connected, and inspect `~/.local/share/unifideck/launcher.log`.

### Microsoft / xCloud Will Not Open

Install Microsoft Edge when prompted. After the first successful Microsoft sign-in, you may still need to click **Play via Cloud** once inside the xCloud home screen to finish OAuth.

### Ubisoft Titles from Epic Hang on Login or Ask for a Key

Make sure your Epic and Ubisoft accounts are linked at [epicgames.com/id/link/ubisoft](https://epicgames.com/id/link/ubisoft). If problems continue, clear `~/.local/share/unifideck/chromium-auth`, `~/.local/share/unifideck/ubisoft_installer_cache`, and the Ubisoft prefixes under `~/.local/share/unifideck/prefixes/`, then try again.

### Logs

The easiest way to get logs is **Settings -> Capture Logs** in the Unifideck Quick Access panel. One tap collects every log and state file, adds a report describing your device and where everything lives, and writes a single zip to your **Downloads** folder. Attach that file to your bug report. It never contains your passwords, store login tokens, or browser cookies.

Individual locations, if you need them directly:

- **Decky/backend log** - `~/homebrew/logs/Unifideck/` (one file per plugin session)
- **Per-launch logs** - `~/.local/share/unifideck/launches/<id>.log` (plugin side) and `<id>.game.log` (Proton/game output)
- **Library sync activity** - `~/.local/share/unifideck/sync_activity.log`
- **Edge/browser sign-in log** - `~/.local/share/unifideck/edge-auth.log`

## Languages

Unifideck currently ships with English (US), French, Brazilian Portuguese, Russian, Japanese, German, Spanish, Italian, Simplified Chinese, Korean, Dutch, Polish, Turkish, and Ukrainian.

To add a new language, create a JSON file in `src/i18n/locales/` using `en-US.json` as the template and wire it into the language selector.

## Building

To build the plugin from source:

1. Install dependencies: `pnpm install`
2. Build a release ZIP: `./build-plugin.sh prod` — produces a versioned plugin ZIP in `out/`.

Common flows (`build-plugin.sh`):

- `./build-plugin.sh prod install` - build, install into Decky, and restart it.
- `./build-plugin.sh dev` / `./build-plugin.sh dev install` - development build (build number auto-incremented).
- `./build-plugin.sh dev quick-install` - fast rsync of backend/config to the live install, no full repackage (run `pnpm run build` first if you changed the frontend).
- `pnpm run build` / `pnpm run watch` - frontend bundle only.

**Tests & checks:** `npm run test:all` (backend + frontend), `npm run test:backend`, `npm run test:coverage`, `npm run typecheck`, `npm run lint`.

## Tech Stack

- **Frontend** - React, TypeScript, Rollup, `@decky/api`, `@decky/ui`, `i18next`
- **Backend** - Python, Decky Loader RPC, a layered architecture with an EventBus and dependency-injection core, CDP-based auth and browser helpers
- **Store tooling** - legendary, gogdl, nile, comet, winetricks, umu-launcher
- **Services and data** - SteamGridDB, Epic/GOG/Amazon/Microsoft APIs, Microsoft Edge, Metacritic, compatibility metadata

## Credits

This project builds on a lot of open source work and community help.

- **Platform and UI** - [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader), `@decky/api`, `@decky/ui`, and the SteamDeckHomebrew community
- **Store and runtime tooling** - [legendary](https://github.com/derrod/legendary), gogdl, [nile](https://github.com/imLinguin/nile), [comet](https://github.com/imLinguin/comet), [winetricks](https://github.com/Winetricks/winetricks), [umu-launcher](https://github.com/Open-Wine-Components/umu-launcher), and [SteamGridDB](https://www.steamgriddb.com/)
- **Reference projects and patterns** - [TabMaster](https://github.com/Tormak9970/TabMaster), [SteamGridDB Decky](https://github.com/SteamGridDB/decky-steamgriddb), [ProtonDB Decky](https://github.com/OMGDuke/protondb-decky), [Heroic Games Launcher](https://github.com/Heroic-Games-Launcher/HeroicGamesLauncher), and [Junk-Store](https://github.com/ebenbruyns/junkstore)
- **Special thanks** - @src893, @xXJSONDeruloXx, @moi952, @Lazer-zx5, @buddax2, @Grails125, @clach04, @kevbenjam, @kmturley, @FreudsCAT, @frank460699, @matheussilva421, DeckWizard, sufi0511, \_badbug, lutianxing, u/EnTei7K, u/IN50MNIAC, derrod, and the Discord testers for invaluable feedback.

## Support

If you want to support development or keep up with releases:

- [Become a GitHub Sponsor](https://github.com/sponsors/mubaraknumann)
- [Buy me a coffee on Ko-fi](https://ko-fi.com/mubaraknumann)
- [Join the Discord](https://discord.gg/s9KVK2jRnp)

## License

GNU General Public License v3.0 or later - see [LICENSE](./LICENSE) for details.

## Author

Numan Mubarak (numanmuabrak@protonmail.com)

## Disclaimer

This is an unofficial third-party tool. It is not affiliated with Valve, Epic Games, CD Projekt / GOG, Amazon, Ubisoft, Blizzard Entertainment, or Microsoft.
