# Feasibility Study: Humble Bundle (DRM-free library)

**Verdict: feasible, medium effort, moderate fragility.** Every comparable tool supports Humble
(Lutris, Playnite official, GOG Galaxy plugins, NonSteamLaunchers, GameHub), and Heroic has an
open PR. Nobody has asked for it in our tracker (GitHub search for "humble": 0 results).

The library API is unofficial but stable and widely used. Sign-in fits our standard Edge
window. The real cost is on the install side: Humble serves whatever the developer uploaded, with
no metadata about which file to run. That is the same problem as GameVault archives, so this
store should reuse that pipeline instead of building its own (see [vaults.md](vaults.md)).

Nothing here was measured with a signed-in account yet. The Cloudflare and cookie facts below
come from anonymous requests made on 2026-09-24.

## MVP walkthrough

| Step | Status | Evidence |
|---|---|---|
| Sign in | Proven (refs) | One cookie, `_simpleauth_sess`. It is **HttpOnly**, so it must be read over CDP (`Network.getAllCookies`), which our Edge harness already does. A normal browser sign-in clears reCAPTCHA, TOTP and the "Humble Guard" email check. Programmatic login (`/processlogin`) does not, so never use it. |
| List owned games | Proven (refs) | `GET /api/v1/user/order` gives `[{gamekey}]`. Then `GET /api/v1/orders?all_tpkds=true&gamekeys=..&gamekeys=..` in batches of about 10. |
| Filter to games | Proven (refs) | Keep a subproduct only if it has a `windows` or `linux` download with a non-empty `download_struct`. This also drops ebooks, audio and Steam-key-only items. Dedupe by `machine_name`: one game appears once per bundle it came in. |
| Download | Proven (refs) | `download_struct[].url.web` is a signed URL with a `ttl`. Re-fetch the order right before downloading. `md5`/`sha1` and `file_size` are provided for checking. |
| Install | Design choice | Linux: `.sh` MojoSetup (unzip, merge `data/noarch` + `data/x86_64`), `.tar.gz`, `.zip`. Windows: `.zip` (extract) or Inno/NSIS `.exe` (run under Proton). No exe metadata anywhere. |
| Launch | Existing code | Native or umu through the generic launcher, as GameVault and itch.io do. |

### Steam keys and subscriptions

- Games delivered as Steam keys appear only in `tpkd_dict.all_tpks[]`, never as downloadable
  subproducts. The download filter already excludes them. They belong in Steam, not here.
- **Humble Choice** games are mostly Steam keys, so Choice adds little.
- **Humble Games Collection** ended on 2023-11-07. **Trove** became the "Vault" inside the
  Windows-only Humble app. Neither is worth supporting.

What is left is the DRM-free catalog from bundles and store purchases. For a long-time Humble
customer that can be hundreds of games.

## Integration design

- **Store type:** API store, closest to itch.io. No bundled binary is needed; the API is plain HTTPS.
  humble-cli (Go, MIT, active in 2026-09) is a useful reference but not worth shipping.
- **Sign-in:** the standard Edge window at `humblebundle.com/login?goto=/home/library`, done when
  the URL reaches `/home/library`. Read `_simpleauth_sess` over CDP. Check the session with a
  cheap authenticated call; Lutris never checks it, and Heroic's PR uses `/user/settings` with
  redirects off.
- **Install pipeline:** reuse the generic acquire, extract, find exe, marker pipeline from the
  GameVault work. A Humble "source" supplies the catalog and the signed download. The
  run-the-installer step (Inno/NSIS in the prefix, then rescan) is shared with GameVault
  archives that hold `setup.exe`, so build it once.
- **Platform choice:** native Linux first, Windows under umu as fallback, the same rule as itch.io.
- **Sync cost:** batch requests, cache orders by `gamekey` (orders never change after purchase),
  and fetch only new gamekeys on later syncs. Heroic testers reported sync "takes a very long
  time" without this.

## Risks

- **Unofficial API.** No public contract. Lutris warns Humble "is restricting API calls from
  software like Lutris", though that warning is about login, which we avoid.
- **Cloudflare** fronts the site (`server: cloudflare`, `__cf_bm` cookie). Plain requests with the
  session cookie work today. A future challenge page would break sync; requests should send a
  normal browser User-Agent.
- **Cookie lifetime is unverified.** The anonymous cookie is issued for 90 days; the signed-in
  lifetime needs measuring. Plan a clear "sign in again" state.
- **Terms of service** ban automated access "that sends more request messages ... than a human
  can reasonably produce". A cached, batched sync stays inside that; a full re-fetch every sync
  might not.
- **Install heuristics fail sometimes.** Heroic says so about its own exe detection. The
  shortcut must offer a way to pick the exe.

## Next step

A spike with a real account in an Edge window:
1. capture `_simpleauth_sess` over CDP;
2. list orders and count games after filtering;
3. download one Linux and one Windows title;
4. record the cookie's expiry.

## References

| Source | License | Use |
|---|---|---|
| JosefNemec/PlayniteExtensions `HumbleLibrary` | MIT | order model fields, sign-in redirect |
| smbl64/humble-cli | MIT | minimal headers, batch sizes |
| lutris/lutris `services/humblebundle.py` | GPL-3.0 | download filter, installer types (approach only) |
| Heroic PRs #4771, #5681 | GPL-3.0 | what testers hit (duplicates, slow sync, Linux installs) |
| humblebundle.com/terms | n/a | automated-access clause |
