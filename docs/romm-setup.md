# RomM setup

RomM is a self-hosted game library. Unifideck imports its Linux and Windows
games so you can install and play them from Steam on your Deck. Emulated
systems, such as consoles, are not imported.

## Before you start

You need a RomM server your Deck can reach. If you do not have one yet, start
with the [RomM quick start guide](https://docs.romm.app/latest/Getting-Started/Quick-Start-Guide/).

You also need either an account that can approve device pairing or a client API
token, and the server address. Use the same address you open in a browser.

## Connect to your server

### Step 1: Open the sign-in window

> Steam **⋯** button → **Decky** → **Unifideck** → **Settings** → **STORE CONNECTIONS**
> → **RomM** → **Sign in**

### Step 2: Enter your server details

Fill in **Server URL**, for example:

```
http://192.168.1.20:8080
https://romm.example.com
```

Leave **Verify SSL certificate** on unless you trust a self-signed certificate
used by your server.

**Archive download directory** is optional. It holds downloads before they are
unpacked, not the installed games. Choose a drive with room for both the archive
and the unpacked game at once. Default paths are listed in the quick reference
below.

### Step 3: Sign in

**Device pairing (default).** Press **Connect**, then scan the QR code or open
the displayed URL. Sign in to RomM and approve the device named
"Unifideck (hostname)". Your phone must be able to reach the server, which may
mean joining your home Wi-Fi. Your password never reaches Unifideck.

**API token.** If your server does not support device pairing, or you prefer a
token, enable **Use an API token instead**, enter your **Client API token** and
press **Connect**. The token needs `roms.read` and `platforms.read` permissions;
device pairing requests the same permissions automatically.

### Step 4: Finish connecting

Connection finishes automatically unless Linux or Windows has multiple platform
folders. In that case, deselect any unwanted folders and press **Finish**.
All folders are included by default. A library sync starts once connected.

Closing the window before completion saves no local connection, though an
approved device and token may remain in RomM.

## Installing and playing

1. Your games appear in Steam's library and under the **RomM** tab. To refresh
   the library later, go to **Settings → LIBRARY SYNC → Sync Libraries**.
2. Open a game and press **Install**. Choose an install location in the storage
   picker, then wait for the download and extraction to finish.
3. Press **Play**. Windows games use Proton; Linux games run natively.

> **Note:** if the wrong program starts, open the game's page in Steam, press
> the gear icon and choose **Change executable…** to select the right file.

Unifideck downloads the game from RomM and unpacks it. A game stored as a
single archive keeps its format, and RomM bundles a multi-file game into a ZIP.
A bare `.exe` that is not in an archive is not installed, and setup programs
are not run for you. The downloaded file is deleted once the game is unpacked,
and uninstalling removes only the installed copy, not the game on your server.

When Linux and Windows versions have exactly the same name, Unifideck imports
the Linux version.

## Game variants

Multiple versions of a game on the same platform appear as separate Steam
entries. RomM's metadata may give them identical names: for example, Lighthouse
and BanjoRecomp builds can both appear as **Banjo-Kazooie**.

Unifideck does not currently provide variant labels or a selector. To tell those
entries apart in Steam, give them distinct names or cover art on the RomM server.

For entries already in Steam, use **Force Sync** to refresh their names. Choose
**Resync Artwork** to replace existing covers, or **Keep Artwork** to update
names without replacing covers.

## Artwork

For missing portrait covers, Unifideck tries your selected RomM cover first.
Other artwork comes from SteamGridDB or Steam. Ordinary sync preserves artwork
already on the Deck, including any you customized in Steam.

To replace existing covers, use **Force Sync → Resync Artwork**. This affects
all stores and overwrites manual artwork changes. If new covers do not appear,
restart Steam after artwork processing finishes.

## Disconnecting or changing servers

**Disconnect** removes the saved connection on the Deck, not installed games
or server tokens. To revoke access, delete the token in RomM's client API tokens;
removing the paired device alone may not revoke its tokens. If the saved token
expires or is rejected, sign in again.

Changing the server's address (scheme, hostname, port or proxy path) creates a
separate library identity, even if it is another address for the same server.
Existing installs stay on disk but are not associated with the new address.
Connecting to the original address restores those associations; there is no
automatic migration.

## Troubleshooting

| Problem | What to do |
|---|---|
| Pairing code expired or request denied | Press **Connect** to start a new pairing. |
| Server returned HTTP 404 | Check the server address, port and any proxy path. Use an API token if the server does not support device pairing. |
| No Linux or Windows platforms found | Check that your RomM library has games on those platforms. Other systems are not imported. |
| SSL or certificate error | Check the certificate and server address. Disable verification only if you trust your self-signed server. |
| Cannot connect or connection refused | Check that the server is running and reachable from your Deck. Try its address in the Deck's browser. |

## Quick reference

| Setting | Default |
|---|---|
| Install location | `~/Games/RomM` |
| Download staging | `~/.local/share/unifideck/romm_downloads` |
| Connection file | `~/.local/share/unifideck/romm_config.json` (mode `0600`, accessible only to your user) |
| Token permissions | `roms.read`, `platforms.read` |
