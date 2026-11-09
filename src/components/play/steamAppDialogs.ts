/**
 * steamAppDialogs: the controller and gear actions every Play-section
 * variant shares.
 *
 * Each variant used to carry its own copy of these two SteamClient calls.
 * They live here once now, with the gear's "native menu first, Properties
 * as the fallback" rule.
 */
import { openNativeAppManageMenu } from "../../utils/nativeAppMenu";

interface SteamAppsDialogs {
  ShowControllerConfigurator?: (id: number) => void;
  OpenAppSettingsDialog?: (id: number, page: string) => void;
}

function steamApps(): SteamAppsDialogs | undefined {
  return (window as unknown as { SteamClient?: { Apps?: SteamAppsDialogs } })
    .SteamClient?.Apps;
}

/** Open Steam's controller configurator for `appId`. */
export function openControllerConfig(appId: number): void {
  steamApps()?.ShowControllerConfigurator?.(appId);
}

/** Open Steam's Properties dialog for `appId`. */
export function openAppSettings(appId: number): void {
  steamApps()?.OpenAppSettingsDialog?.(appId, "general");
}

/**
 * The gear button: Steam's native app menu (Manage / Properties / ...),
 * matching the native gear, or Properties directly when that menu cannot
 * be found.
 */
export function openGearMenu(
  target: HTMLElement | null | undefined,
  appId: number,
): void {
  if (!openNativeAppManageMenu(target ?? null)) openAppSettings(appId);
}
