# Unifideck - Unified Game Library for Steam Deck

A Decky Loader plugin that brings together games from Steam, Epic Games Store, and GOG into a single, unified library experience on your Steam Deck.

![License](https://img.shields.io/badge/license-GPL--3.0-blue.svg)
![Platform](https://img.shields.io/badge/platform-Steam%20Deck-orange.svg)

## Table of Contents
- [Features](#features)
- [Screenshots](#screenshots)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Getting Started](#getting-started)
- [Known Limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [License](#license)
- [Credits](#credits)
- [Author](#author)
- [Disclaimer](#disclaimer)

## Features

- **Unified Game Library** - Epic Games and GOG games appear directly in your Steam library
- **One-Click Installation** - Install Epic and GOG games directly from Steam's interface
- **Automatic Artwork** - Game covers, banners, and logos fetched automatically from SteamGridDB
- **In-App Authentication** - Log into Epic and GOG stores without leaving Gaming Mode
- **No Restart Required** - Installed games are playable immediately without restarting Steam*

*Still needs one time restart after libraries have been synced

## Screenshots

### Unified Game Library

![20260104022735_1](https://github.com/user-attachments/assets/208a9610-2b4f-4e1d-941f-33a152cc34d8)

### Game Details

![20260104022821_1](https://github.com/user-attachments/assets/afc0922e-aace-4d47-925e-1bc7f1e48140)

## Prerequisites

**Decky Loader** must be installed on your Steam Deck
- [Decky Loader Installation Guide](https://github.com/SteamDeckHomebrew/decky-loader)

That's it! All other tools and dependencies are bundled with the plugin.

## Installation

1. Download the plugin ZIP file: https://github.com/user-attachments/files/24423959/unifideck-plugin-v0.2.0.zip
2. Open **Quick Access Menu** (three dots button)
3. Navigate to **Decky** → **Settings** (gear icon)
4. Enable **Developer Mode** if not already enabled
5. Click **Install Plugin from ZIP**
6. Navigate to the downloaded ZIP file and select it
7. The plugin will install automatically

## Getting Started

1. Open the **Quick Access Menu** and find **Unifideck**
2. Connect your **Epic Games** and/or **GOG** accounts using the authenticate buttons
3. Click **Sync Libraries** and wait for completion. Restart Steam.

Your games will now appear in your Steam library!

## Known Limitations

- The plugin creates custom tabs that replace the standard Great on Deck, All Games and Installed tabs so standard filtering and sorting will not work (for now).
- Some GOG games come as multiple download files - these are handled automatically but may take longer to install
- A few Epic multiplayer games need extra setup for online features (work in progress)
- Not all games have artwork available - some may show default images. Suggest using SteamGrid DB.
- Cloud Saves do not work
- Compatibility issues with Tabmaster (WIP)

## Troubleshooting

### Games Don't Appear After Syncing

1. Restart Steam/Steam Deck
2. Re-run sync/force sync from the Quick Access Menu
3. Check that your accounts are still connected

### Can't Install a Game

1. Make sure you have enough storage space
2. Check that your store account is still authenticated
3. Try logging out and back into the store
4. Check the launcher logs at `~/.local/share/unifideck/launcher.log`

### Cover Art Missing

1. Run another sync - artwork is fetched during the sync process
2. Some games may not have artwork available in the SteamGridDB database

### Game Won't Launch

1. Check the launcher logs at `~/.local/share/unifideck/launcher.log`
2. For GOG games, verify the game folder exists in `~/GOG Games/`
3. Try reinstalling the game

## License

GNU General Public License v3.0 - see [LICENSE](./LICENSE) file for details

## Credits

This project builds upon numerous open source projects, libraries, and tools. We are grateful to all contributors and maintainers.

### Core Framework
- **[Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader)** - For plugin runtime environment and backend API integration
- **[decky-frontend-lib](https://github.com/SteamDeckHomebrew/decky-frontend-lib)** - For Steam UI components, routing, and Deck-specific React hooks

### Game Store Integration (Binaries)
- **[legendary](https://github.com/derrod/legendary)** - For authenticating, syncing library, downloading, and launching Epic Games Store titles
- **[umu-launcher](https://github.com/Open-Wine-Components/umu-launcher)** - For running Windows games (.exe) with Proton compatibility layer
- **[innoextract](https://constexpr.org/innoextract/)** - For extracting GOG Windows game installers without Wine

### Python Libraries
- **[websockets](https://github.com/python-websockets/websockets)** - For real-time communication with Steam client
- **[python-vdf](https://github.com/ValvePython/vdf)** - For reading/writing Steam's shortcuts.vdf and config files
- **[Requests](https://github.com/psf/requests)** - For GOG API authentication and game metadata fetching
- **[steamgrid](https://github.com/ZebcoWeb/python-steamgrid)** - For fetching game artwork (grid, hero, logo) from SteamGridDB
- **[certifi](https://github.com/certifi/python-certifi)** - For providing SSL certificates for HTTPS requests
- **[charset-normalizer](https://github.com/Ousret/charset_normalizer)** - For handling character encoding in API responses
- **[idna](https://github.com/kjd/idna)** - For internationalized domain name support in URLs
- **[urllib3](https://github.com/urllib3/urllib3)** - For underlying HTTP client for requests library
- **[pip](https://github.com/pypa/pip)** - For managing Python dependencies in isolated environment (bundled)

### APIs & Services
- **[SteamGridDB](https://www.steamgriddb.com/)** - For automatically downloading cover art, banners, and logos for non-Steam games
- **Epic Games API** - For fetching Epic library data and game metadata via legendary
- **GOG API** - For authenticating users, fetching game library, and retrieving installer URLs

### Decky Plugins (Code Reference)
The following Decky plugins were studied as reference during development:
- **[TabMaster](https://github.com/CEbbinghaus/TabMaster)** - For library tab replacement and Steam UI patching techniques
- **[CSSLoader](https://github.com/DeckThemes/SDH-CssLoader)** - For plugin architecture and settings management patterns
- **[SteamGridDB Decky](https://github.com/SteamGridDB/decky-steamgriddb)** - For SteamGridDB API integration and artwork downloading
- **[ProtonDB Decky](https://github.com/OMGDuke/protondb-decky)** - For game compatibility rating integration patterns
- **[HeroicGamesLauncher](https://github.com/Heroic-Games-Launcher/HeroicGamesLauncher)** - For Epic and GOG launcher integration approaches
- **[Junkstore](https://github.com/ebenbruyns/junkstore)** - For non-Steam game management and authentication references

### Special Thanks
- **Valve** - For the Steam Deck platform and Steam OS
- **SteamDeckHomebrew Community** - For Decky Loader and extensive documentation
- **derrod** - For legendary and Epic Games integration insights
- All open source contributors whose work makes this project possible

## Author

Numan Mubarak

## Disclaimer

This is an unofficial third-party tool. Not affiliated with Valve, Epic Games, or CD Projekt (GOG).
