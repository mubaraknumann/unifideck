# OS Compatibility — SteamOS, Bazzite & CachyOS

Unifideck is built and tested on **SteamOS** (Steam Deck). The **Steam Machine**
runs the same SteamOS 3 / gamescope stack and is supported — see
[Steam Machine](#steam-machine) below for what differs. It also runs on other
Decky-Loader-capable distros — **Bazzite** (Fedora Atomic / immutable rootfs) and
**CachyOS** (Arch, rolling release) are the two we explicitly account for here.

> **Support posture:** Bazzite and CachyOS are **community-supported, not officially
> supported.** The code is written to *self-adapt* to any Linux distro / username /
> system-Python version rather than branch on the distro, so it should work on par with
> SteamOS. This document records every SteamOS-specific assumption we found and how each
> is handled, so contributors don't re-bake Deck-only paths and users know what to expect.

If you hit a problem on one of these distros, export the launcher logs
(`~/.local/share/unifideck/launches/`) and include your `cat /etc/os-release` +
`python3 --version` when reporting.

---

## What's portable by design

These are handled generically and need no per-distro logic:

| Area | How it stays portable |
| --- | --- |
| Home / data paths | All paths route through `Path.expanduser()` / `Path.home()` and `$HOME`-respecting config — no hardcoded `/home/deck` in live code. |
| Steam install | `STEAM_PATH_CANDIDATES` probes `~/.steam/steam`, `~/.local/share/Steam`, and Flatpak Steam, validated by a `steamapps/` check. |
| External drives / SD cards | `_scan_external_mounts()` reads `/proc/mounts`, skips the `$HOME` device, scans 2 levels deep — no hardcoded device names. |
| Proton / GE-Proton | Latest GE-Proton auto-downloads to `~/.local/share/unifideck/compat-tools` (checked first); compat roots also cover `~/.steam/root`, `~/.steam/steam`, `~/.local/share/Steam`. |
| Bundled tools | legendary, gogdl, nile, comet, umu, winetricks ship in `bin/` as Linux x86_64 — fine on any x86_64 distro. |
| Auth window size | Reads the live display resolution; 1280×800 is only a fallback. |
| Plugin backend Python | Decky Loader ships its **own** Python 3.11 on every distro, so the backend ABI is fixed regardless of host. |

---

## Known irregularities & how they're handled

### 1. System-Python ABI for the game launcher — **handled (build-time)**

The Steam-shortcut launcher (`bin/unifideck-launcher`, `#!/usr/bin/env python3`) runs
under the **host** `/usr/bin/python3`, whose minor version differs by distro and, on
CachyOS, drifts over time (Arch is rolling). Its cloud-save path needs cffi's
ABI-specific `_cffi_backend.<abitag>.so`.

`build-plugin.sh` vendors that backend for **every** version in
`LAUNCHER_PYTHON_VERSIONS=(3.10 3.11 3.12 3.13 3.14)`, so whichever system Python the
host ships finds a matching `.so` automatically. Versions with no published cffi wheel
are skipped with a warning. Keep this range in sync with `ACCEPTED_VERSIONS` in
`py_modules/unifideck/launcher/proton/infrastructure/selector.py`.

If a host runs a Python outside that range (e.g. a future 3.15 before we add it), the
launcher **degrades gracefully**: cloud-save is disabled and the user gets a one-time
toast (`cloudSync.unavailableNativeDep`) explaining why — game launches are unaffected.

### 2. Deck microSD device path — **handled (runtime)**

SteamOS mounts the internal microSD at `/run/media/mmcblk0p1`, a device node that doesn't
exist on desktops or other handhelds. The Ubisoft SD install default
(`py_modules/unifideck/stores/ubisoft/config.py`) now resolves the first writable mount
under `/run/media` dynamically (handling both `/run/media/<label>` and udisks2's
`/run/media/<user>/<label>` nesting), falling back to the historical Deck path only when
nothing is mounted. Live install detection already re-scans removable media at scan time.

### 3. Proton compat-tool roots & `~/.steam/root` — **handled**

Proton/GE resolution previously depended on the `~/.steam/root` symlink existing. The
scan lists in `selector.py` and `ge_installer.py` now also include `~/.steam/steam/...`
so resolution survives a missing symlink.

### 4. `/home/deck` fallback strings — **handled**

A few `os.environ.get("DECKY_PLUGIN_DIR", "/home/deck/...")` / `HOME=/home/deck` /
file-browser placeholder fallbacks baked in the `deck` user. They were only reachable
when the real env var was unset, but they now use `~`/`Path.home()`/`/home` so nothing
assumes the SteamOS username.

---

## Steam Machine

Valve's desktop SteamOS console (DMI `product_name` **Fremont**, released
2026-06-30). Same SteamOS 3, same gamescope, same Big Picture UI, same Proton —
so nearly everything here applies unchanged. It has a microSD slot, so §2 above
is correct on it too. Its GPU is AMD RDNA3, so nothing in Proton/Vulkan
selection changes.

What is genuinely different, and how it is handled:

### Compatibility ratings are per-device — **handled**

Valve rates each device **independently**: the same title can be Playable on a
Deck and Verified on a Machine. Measured across a 300-title sample, 21 titles
promote and 7 demote between the two. Steam's own endpoint returns four tracks
in one response (`resolved_*` = Deck, `machine_resolved_*`, `steamos_resolved_*`,
`frame_resolved_*`); `compatibility/deck_verified.py` parses all of them and
`rpc/mixins/_compat_payload.py` serves the one matching the detected device.

The **SteamOS** track uses a 3-value enum (`0` unknown, `1` unsupported,
`2` compatible) rather than Valve's 4-value ladder, and never emits `3`. Its
integers must not be routed through the Deck ladder.

Steam packs all four into one `AppOverview` field,
`steam_hw_compat_category_packed`, two bits each — Deck at bit 0, SteamOS at 4,
Machine at 6, Frame at 8. We write every track we know
(`src/lib/steam-bridge/compat-packed.ts`), because Steam's own badge and
library-filter components read the slot for the *running* device.

### Steam's native compat tab is named per device — **handled**

Steam emits a different tab id depending on hardware: `GreatOnDeck`,
`GreatOnMachine`, or `SteamOSCompatible` (the Steam Frame reuses `GreatOnDeck`).
All are in `DEFAULT_TABS_TO_HIDE`, unconditionally, so our tab never ends up
sitting beside a near-identical native one.

### Collections are account-global — **handled**

Our compat collection is named after the device, but Steam syncs collections
through the cloud. A user with both a Deck and a Machine would otherwise have
each device delete the other's collection on every boot;
`validCollectionNames()` recognises all three device names as valid while
creating only the local one.

### Forcing a device for testing

`detect_device_type()` honours `UNIFIDECK_DEVICE_TYPE` (`deck` / `machine` /
`other`) ahead of DMI, so the Machine path can be exercised on a Deck:

```bash
sudo systemctl set-environment UNIFIDECK_DEVICE_TYPE=machine
sudo systemctl restart plugin_loader
# undo:
sudo systemctl unset-environment UNIFIDECK_DEVICE_TYPE
```

A support bundle taken while this is set carries `device_type_forced: true`, so
a forced run is never mistaken for real hardware.

### Steam Frame — not supported

Parsed and cached, but never surfaced. Valve publishes a Frame rating for almost
no titles yet, and the headset is ARM64 while every bundled store CLI
(`legendary`, `gogdl`, `nile`, `comet`) is `linux_x86_64`.

---

## Open limitations (no code change — document only)

- **Controller layouts on non-Deck hardware.** The xCloud / in-app-browser controller
  feature applies Steam Input layouts targeting the Deck's built-in controller
  (`controller_neptune_*.vdf`), and `PRIMARY_CONTROLLER_INDEX = 0` assumes a
  built-in pad. On a Steam Machine, a desktop or a non-Deck handheld the pad is
  external and the template match may simply not fire. Best-effort and
  non-fatal — `applySelectedTemplate` returns `false` and the default layout
  stays, so everything else works. Store-login pages are the surface most
  affected, since they lean on the Deck's trackpad-as-mouse.
- **Game Pass / xCloud sign-in needs Microsoft Edge.** Auth uses Edge via Flatpak (the
  installer adds a `--user` Flathub remote) or a native `microsoft-edge` binary. Bazzite
  ships Flatpak + Flathub by default; a minimal CachyOS may need `flatpak` installed and
  Flathub added first. **Other stores (Epic, GOG, Amazon, Ubisoft) are unaffected.**

---

## Manual verification checklist (real Bazzite / CachyOS hardware)

We don't run CI on these distros, so when validating on real hardware:

1. `cat /etc/os-release` and `python3 --version` — note the system Python.
2. Build + install: run `./build-plugin.sh`, install the zip via Decky.
3. Confirm `py_modules/` contains `_cffi_backend.cpython-3XY-*.so` matching your
   system Python (e.g. `cpython-313` on a 3.13 host).
4. Sign into a store (Epic/GOG fastest), install a small game.
5. Launch it. Confirm the per-game prefix appears under
   `~/.local/share/unifideck/prefixes/<...>` and Proton resolves (check the launch log).
6. If you have an SD card / external drive, install a game to it and confirm the path is
   correct (not `/run/media/mmcblk0p1`).
7. Confirm cloud-save sync runs (or degrades with the `cloudSync.unavailableNativeDep`
   toast if your Python is unsupported — that's expected, not a crash).
