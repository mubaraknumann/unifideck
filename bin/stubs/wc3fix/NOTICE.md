# Patched `crypt32.dll` — Warcraft III: Reforged login fix

## What this is

Two rebuilt Wine `crypt32.dll` binaries that accept the current 88-byte
`CERT_CHAIN_ENGINE_CONFIG` struct, plus the patch that produced them.

| File                                   | SHA-256                                                            |
| -------------------------------------- | ------------------------------------------------------------------ |
| `x86_64-windows/crypt32.dll`           | `dca9a2c04c55a9239f7300a183e7d69222cbf7607c473a229ca314fa6903b6e3` |
| `i386-windows/crypt32.dll`             | `c73b62661f9a34203d08c6fc47329a8f8a22791f3da5db471276919be192ab1b` |
| `crypt32-chain-engine-config-88.patch` | `b57366f609a8ee15f80788dc9b38f05e19c93efc884024a4c49a153e4722b93b` |

## Why it exists

Warcraft III: Reforged 3.0 ships a `ClientSdk.dll` that calls
`CertCreateCertificateChainEngine()` with an 88-byte
`CERT_CHAIN_ENGINE_CONFIG`. Wine 11.0's `crypt32` accepts only the 64- and
80-byte layouts and rejects anything else with `E_INVALIDARG`, so the game
cannot build a certificate chain, treats Blizzard's TLS as untrusted, and
tells the user to _"check your VPN"_. Every shipping Proton is on the Wine
11.0 stable branch, GE-Proton11-6 and `experimental-11.0-*` included, so no
Proton selection avoids it.

See `py_modules/unifideck/launcher/proton/fixes/wc3_crypt32.py` for how these
are used, and `docs/proton-compatibility.md` for the user-facing writeup.

## These only work inside a Proton build

Wine's `crypt32` is a **split PE/unix DLL**: the PE half here is paired with
`files/lib/wine/<arch>-unix/crypt32.so`, and Wine only wires that unix half
up for a DLL it loads as _builtin_ from Proton's own directory. Dropping one
of these next to a game executable and preferring it with `crypt32=n,b` was
measured on-device and kills the game on launch:

```
ACCESS_VIOLATION (Failed to read address 0x0000000000000040)
    CRYPT32.dll <- ClientSdk.dll <- ClientSdk.dll
```

So each DLL is only valid alongside the `crypt32.so` of the build it was
compiled against — **GE-Proton11-6** — which is why `SUPPORTED_PROTON_BUILDS`
lists that one build and refuses every other.

## Provenance

Obtained from the `v1.0` release of
<https://github.com/FerMPY/wc3-reforged-proton-fix>, asset
`crypt32-wc3fix-dlls.zip`, whose published `SHA256SUMS` records
`85d6b91190bb51a533498c2b6498d07376e772ee73cd535f60a01e93167adb02`. That
hash was verified against the downloaded archive before the DLLs were
extracted, and both DLLs were confirmed to contain the patch's new
`dwExclusiveFlags` trace strings, which the stock GE-Proton11-6 DLLs do not.

## Licensing

These are derivative works of **Wine**, which is licensed under the **GNU
Lesser General Public License, version 2.1 or later**. The upstream fix
repository states no license of its own; the LGPL terms of the original
work carry over regardless.

`crypt32-chain-engine-config-88.patch` is kept beside the binaries
deliberately, so the modifications are distributed with the object code it
produced. The patch is a backport of three upstream Wine commits, which
shipped in **Wine 11.6**:

- `02bb0a34` — crypt32: Trace `CERT_CHAIN_ENGINE_CONFIG` fields in `CertCreateCertificateChainEngine()`.
- `eef8e97d` — crypt32: Don't access `CERT_CHAIN_ENGINE_CONFIG::dwExclusiveFlags` without checking size.
- `c7cc9be8` — crypt32: Also accept `CERT_CHAIN_ENGINE_CONFIG` without `dwExclusiveFlags`.

Wine source: <https://gitlab.winehq.org/wine/wine>. LGPL-2.1 text:
<https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html>.

## Retiring this

**GE-Proton11-7 already carries the upstream fix** (measured 2026-09-17: its
stock `crypt32.dll` contains the backport's `dwExclusiveFlags` trace strings,
which GE-Proton11-6's does not), and Warcraft III logs in on it with no
workaround. `proton_has_crypt32_fix()` probes for exactly that, so the
workaround already switches itself off on 11-7 and newer.

This directory therefore only serves users still on GE-Proton11-6, and
becomes dead weight once that build is no longer worth supporting. When that
happens, drop the DLLs, `fixes/wc3_crypt32.py`, `handlers/battlenet_wc3.py`
and its call in `handlers/battlenet.py` — and delete any variant left under
`~/.local/share/unifideck/proton/`.
