# Proton Compatibility

Unifideck runs Windows games through Proton/umu in a per-game Wine prefix. By default it
uses the **latest GE-Proton**, auto-downloaded from GitHub on first use and cached.
**Proton Experimental** is the offline fallback if GE-Proton can't be fetched or installed.

If you already manage GE-Proton with an external tool (ProtonPlus, ProtonUp-Qt, or a distro
package), Unifideck will use that copy instead of downloading a second one — but only when it
is **at least as new** as the build Unifideck has. If yours is older, or its version can't be
read, Unifideck keeps using its own. It also keeps one GE-Proton of its own on disk as a
recovery Proton when your external copy falls a long way behind, and tells you once per
release when it does. Set `compat.external_ge` to `"off"` in `config.json` to ignore external
tools entirely.

You normally don't need to do anything — the default works for most games. When a game needs
a specific Proton, set it through **Steam's native compatibility tool** (below).

---

## Choosing a Proton version

Pick the Proton tool the same way you would for any non-Steam game, via Steam:

1. Open the game's **Properties** — on the Unifideck game page, the **⚙ App Settings** button
   opens Steam's menu; or right-click the game in your Library → **Properties**.
2. Go to **Compatibility**.
3. Tick **"Force the use of a specific Steam Play compatibility tool"** and choose a tool
   (e.g. a `GE-Proton…` version or `Proton Experimental`).

Unifideck detects this choice, remembers it per-game (saved to
`~/.local/share/unifideck/proton_settings.json`), and applies it when the game launches. It
also clears Steam's force-compat flag at launch so the game doesn't get wrapped in Proton
twice — your selection still shows in the Properties dialog.

> [!NOTE]
> You don't choose Proton in the Unifideck panel itself — there's no in-plugin Proton picker.
> The Steam Compatibility dropdown is the supported way.

### Selection priority

When launching, Unifideck picks the Proton tool in this order:

1. **Per-game choice** — the tool you forced via Steam's Compatibility dropdown
   (`proton_settings.json`).
2. **Steam's own compat override** for that shortcut (`localconfig.vdf`).
3. **Unifideck global default**, if set (`config.json` → `compat.proton_tool`).
4. **Externally managed GE-Proton** — a tool such as ProtonPlus's `Proton-GE Latest`, used
   only when it is provably at least as new as Unifideck's own copy
   (`config.json` → `compat.external_ge`, `"auto"` by default, `"off"` to ignore).
5. **Latest GE-Proton** — auto-downloaded and cached (the default).
6. **Proton Experimental** — offline fallback only.

---

## Troubleshooting & quick fixes

**The game won't launch, or you see "Path Not Found".**
The per-game Wine prefix may be missing or half-created. Delete it and relaunch — Unifideck
rebuilds it automatically:

```
rm -rf ~/.local/share/unifideck/prefixes/<game_id>
```

(Ubisoft games may store their prefix at a custom path recorded in
`~/.local/share/unifideck/ubisoft_id_map.json`.)

**It shows "Downloading Proton" and then fails.**
GE-Proton is fetched from GitHub on first use, so it needs an internet connection. If the
download fails (offline, GitHub rate-limited, interrupted extract), Unifideck falls back to
Proton Experimental. Relaunch when you're back online and it will retry, then cache the
result for next time.

**"No compatibility tool" / nothing to run.**
Make sure Steam has at least one Proton available — run Steam once, or install a Proton/
GE-Proton. Unifideck needs either GE-Proton or Proton Experimental present.

**A specific game crashes or misbehaves with the default Proton.**
Force a different tool via the Steam Compatibility dropdown (above) — try the latest
GE-Proton or Proton Experimental, and check the game on [ProtonDB](https://www.protondb.com/)
for per-game tips.

**Missing runtime dependencies (DXVK, Visual C++, etc.).**
Unifideck runs the usual prefix setup (winetricks/vcredist/DXVK) best-effort when it creates
the prefix. If a game is missing something, delete the prefix (above) and relaunch to
re-run setup.

**Game-specific environment tweaks.**
Some issues are fixed with environment variables (e.g. `PROTON_USE_WINED3D=1`,
`PROTON_NO_ESYNC=1`/`PROTON_NO_FSYNC=1`, `WINEDLLOVERRIDES=...`). These can be set via the
shortcut's launch options — see **[Launch Options](launch-options.md)** for the exact syntax
and caveats.

---

## Known game issues

### Warcraft III: Reforged says "Please check your VPN"

The game launches, reaches its menu, and then refuses to sign in with:

> There was an error in handling the request. Please check your VPN

**Your network is fine and there is nothing to fix at your end.** This is an upstream Wine
bug, and the message is misleading.

Reforged 3.0 ships a `ClientSdk.dll` that calls `CertCreateCertificateChainEngine()` with the
current 88-byte `CERT_CHAIN_ENGINE_CONFIG` structure. Wine 11.0 accepts only the two older
layouts — 64 and 80 bytes — and rejects anything larger with `E_INVALIDARG`. The game cannot
build a certificate chain engine, so it treats Blizzard's perfectly valid TLS as untrusted and
reports it as a VPN or proxy problem.

Upstream fixed it in **Wine 11.6** (three commits: `02bb0a34`, `eef8e97d`, `c7cc9be8`).

#### What you need to do

**Use GE-Proton11-7 or newer** — measured 2026-09-17, it is the first Proton carrying the
upstream fix, and on it the game simply works with no workaround at all. Set it in the game's
Properties in Steam → Compatibility.

On **GE-Proton11-6**, Unifideck patches it for you automatically (below). On anything older —
including Proton Experimental `11.0-*` — it tells you in a toast and the game keeps showing
the VPN error.

#### How the fix works

Unifideck checks the selected Proton's own `crypt32.dll` for the upstream fix. If it is
already there, Unifideck does nothing. That is a direct capability check rather than a version
allowlist, so this workaround switches itself off as soon as your Proton catches up.

If it is missing and the build is GE-Proton11-6, Unifideck builds a private patched copy under
`~/.local/share/unifideck/proton/GE-Proton11-6-wc3fix` and points `PROTONPATH` at it for this
one title. Your real GE-Proton11-6 is never modified, and no other game is affected.

Only GE-Proton11-6 is patched, because in Wine this DLL is a **split PE/unix pair** — the
`crypt32.dll` under `files/lib/wine/x86_64-windows/` only works alongside the `crypt32.so`
under `files/lib/wine/x86_64-unix/` from the same build, and the bundled DLL was compiled
against GE-Proton11-6. Pairing it with another build's unix half is untested, so Unifideck
refuses rather than guessing.

The copy is a **hardlink tree**: every file except the two `crypt32.dll`s is a second name for
the original's inode, so it costs about 1.7 MB of real disk and a fraction of a second,
against the ~1.5 GB a plain copy would take. It is rebuilt automatically if you update
GE-Proton11-6 or Unifideck ships a new DLL. It does not appear in Steam's compatibility
dropdown, because Unifideck sets `PROTONPATH` itself.

For the record, the obvious cheaper approach does **not** work: dropping the patched DLL next
to `Warcraft III.exe` with `WINEDLLOVERRIDES=crypt32=n,b` loads it, but Wine only wires up the
unix half for a DLL loaded as *builtin* from Proton's own directory. The game then dies
instantly with `ACCESS_VIOLATION (Failed to read address 0x40)` inside `CRYPT32.dll`. That is
why the patched DLL has to live inside a Proton build.

Provenance, hashes, and the LGPL notice for the bundled DLLs are in
`bin/stubs/wc3fix/NOTICE.md`. Once a Proton built on Wine 11.6 or newer ships, this whole
workaround can be removed.

---

## Picking Proton from launch options (`PROTON=` / `PROTONPATH=`)

> [!WARNING]
> **Not currently supported.** Selecting Proton with `PROTON=GE-Proton…` or `PROTONPATH=…` in
> a game's launch options does **not** work in the current build — the launcher overwrites
> `PROTONPATH` itself, and the launch-option parser that handled `PROTON=` isn't wired into
> the launch path. Use the [Steam Compatibility dropdown](#choosing-a-proton-version)
> instead. (`PROTON=` was supported in earlier 0.x releases and is intended to return — see
> [Launch Options → Planned](launch-options.md#planned--not-yet-wired).)

---

GE-Proton includes additional patches and fixes not present in official Proton, which is why
Unifideck defaults to it.
