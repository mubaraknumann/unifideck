/**
 * Every Xbox PC refusal and failure code renders as a translated sentence.
 *
 * The backend returns `xbox_pc_*` from the install precondition (before
 * anything is queued), `xbox_*` from the install, update and uninstall
 * themselves, and `xodus_*` from the Xodus CLI adapter. Unmapped, each would
 * reach the toast as a raw token in all 16 locales.
 *
 * Asserts the chosen key, not its text. The translation check for these keys
 * belongs in `download-errors.test.ts` once the locale files carry them.
 */
import { describe, it, expect } from "vitest";
import { friendlyDownloadError } from "./download-errors";

/** Identity `t`: these tests care which key is chosen. */
const t = ((key: string) => key) as never;

describe("friendlyDownloadError: Xbox PC codes", () => {
  it.each([
    ["xbox_pc_disabled", "errors.download.xboxPcDisabled"],
    ["xbox_pc_runtime_missing", "errors.download.xboxPcRuntimeMissing"],
    ["xodus_backend_missing", "errors.download.xboxPcRuntimeMissing"],
    ["xodus_backend_tampered", "errors.download.xboxPcRuntimeDamaged"],
    ["xbox_pc_not_signed_in", "errors.download.xboxPcSignIn"],
    [
      "xbox_signin_required: NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-http 401",
      "errors.download.xboxPcSignIn",
    ],
    ["xodus_credential_keyring_missing_data", "errors.download.xboxPcSignIn"],
    ["xbox_credentials_unavailable: credential-io", "errors.download.xboxPcCredentials"],
    ["xbox_pc_unsupported_plan", "errors.download.xboxPcUnsupportedPlan"],
    ["xbox_pc_needs_choice", "errors.download.xboxPcNeedsChoice"],
    ["xbox_package_choice: 2 editions", "errors.download.xboxPcNeedsChoice"],
    ["xbox_pc_no_package", "errors.download.xboxPcNoPackage"],
    ["xbox_package_unavailable", "errors.download.xboxPcNoPackage"],
    ["xbox_license_denied: tail", "errors.download.xboxPcLicenseDenied"],
    ["xbox_license_rate_limited", "errors.download.xboxPcLicenseRateLimited"],
    ["xbox_license_device_limit", "errors.download.xboxPcLicenseDeviceLimit"],
    ["xbox_license_unavailable", "errors.download.xboxPcLicenseUnavailable"],
    ["xbox_license_invalid", "errors.download.xboxPcLicenseInvalid"],
    ["xbox_filesystem_unsupported", "errors.download.xboxPcFilesystem"],
    ["xbox_install_incomplete: container_missing", "errors.download.xboxPcIncomplete"],
    ["xbox_resolve_failed: xodus_timeout", "errors.download.xboxPcResolveFailed"],
    ["xbox_market_unknown", "errors.download.xboxPcMarketUnknown"],
    [
      "xbox_already_installed: /run/media/sd/Xbox/9PK087LNGJC5",
      "errors.download.xboxPcAlreadyInstalled",
    ],
    ["xbox_update_pending", "errors.download.xboxPcUpdatePending"],
    ["xbox_game_running", "errors.download.xboxPcGameRunning"],
    ["xodus_busy_game_running", "errors.download.xboxPcGameRunning"],
    ["xbox_install_in_progress", "errors.download.lockConflict"],
    ["xodus_components_busy", "errors.download.xboxPcComponentsBusy"],
    [
      "xbox_install_unreachable: /run/media/sd/Xbox/9PK087LNGJC5",
      "errors.download.xboxPcDriveMissing",
    ],
    [
      "xbox_old_partial_in_place: /run/media/sd/Xbox/9PK087LNGJC5",
      "errors.download.xboxPcOldPartial",
    ],
    ["xodus_components_unreadable: PermissionError", "errors.download.xboxPcBackendFailed"],
    ["xodus_panic: thread main panicked", "errors.download.xboxPcBackendFailed"],
    ["xodus_empty_response", "errors.download.xboxPcBackendFailed"],
    ["xodus_failed: RuntimeError", "errors.download.xboxPcBackendFailed"],
    ["xbox_folder_in_use: gog:1", "errors.download.folderInUse"],
    ["record_write_failed: PermissionError", "errors.download.processFailed"],
    ["uninstall_refused_unsafe_path", "errors.download.uninstallRefused"],
    ["uninstall_incomplete_files_remain", "errors.download.uninstallIncomplete"],
    ["stalled: no output for 300s while downloading", "errors.download.stalled"],
  ])("maps %s", (raw, key) => {
    expect(friendlyDownloadError(raw, t)).toBe(key);
  });

  it("lets an xodus exit tail reach the text rules", () => {
    expect(friendlyDownloadError("xodus_exit_1: No space left on device", t)).toBe(
      "errors.download.diskSpace",
    );
    expect(friendlyDownloadError("xodus_exit_2: something odd", t)).toBe("something odd");
  });

  it("does not swallow a longer code that merely starts like one", () => {
    expect(friendlyDownloadError("xbox_pc_disabled_later", t)).toBe("xbox_pc_disabled_later");
  });
});
