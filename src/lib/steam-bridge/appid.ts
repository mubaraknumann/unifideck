/**
 * Signed/unsigned 32-bit Steam AppID conversion.
 *
 * Steam is inconsistent about which form it uses for non-Steam shortcuts:
 * `shortcuts.vdf` and `appStore.m_mapApps` are keyed by the **signed**
 * value (`-325061865`) while `config.vdf`'s `CompatToolMapping`, the
 * `reaper`'s `SteamLaunch AppId=` argument and our own RPC payloads use the
 * **unsigned** one (`3969905431`). Passing the wrong form silently matches
 * nothing, which is how the user's Force-Compat choice used to get dropped
 * (see the backend `get_steam_compat_tool_override` docstring).
 *
 * Lives in its own module so the frontend has ONE implementation — both
 * `app-store-patcher` and `shortcut-types` need it and neither should import
 * the other.
 */

/** Normalise an AppID to its unsigned 32-bit form. */
export function toUnsignedAppId(id: number): number {
  return id < 0 ? id + 0x100000000 : id;
}

/** Normalise an AppID to its signed 32-bit form. */
export function toSignedAppId(id: number): number {
  return id > 0x7fffffff ? id - 0x100000000 : id;
}
