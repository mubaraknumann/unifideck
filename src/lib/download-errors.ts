/**
 * download-errors — map a backend download-failure code/message to a
 * friendly, localized string.
 *
 * The backend folds the store CLI's real error tail into
 * `error_message` (e.g. `insufficient_space:need=66.4GB,free=43.5GB` from
 * the size-aware preflight, or `legendary_exit_1: … Not enough available
 * disk space! …` when legendary itself aborts). The failed-row detail
 * (`friendlyDownloadError`) and the failure toast (`downloadErrorToast`)
 * classify through one helper so they always agree; the toast shows a
 * short twin of the row's sentence.
 *
 * Matching keys off the RAW message text, not the backend `error_type`:
 * `classify_download_error` historically missed legendary's "disk space"
 * phrasing, so the type is unreliable for the disk case. The final
 * branch echoes the raw tail (minus a `legendary_exit_N:` prefix) so an
 * unrecognized failure is never silent again.
 *
 * GOG only started reaching the text heuristics in the audit-§3.2 pass. It
 * used to return five bare codes with no CLI output attached — so a GOG
 * install that died from a full disk, a dropped connection or an expired
 * token showed the literal token `download_failed`, untranslated, in every
 * locale, while Epic and Amazon showed a localized explanation for the same
 * cause. `EXIT_PREFIX_RE` had anticipated `gogdl_exit_N:` all along.
 */
import type { TFunction } from "i18next";
import {
  toastText,
  type ToastKey,
  type ToastString,
} from "../services/toast-text";

/** Pull `need`/`free` GB out of an `insufficient_space:…` code. */
const SPACE_RE = /need=([\d.]+)GB,free=([\d.]+)GB/;
/** Strip a leading machine prefix like `legendary_exit_1: ` / `gogdl_exit_2: `. */
const EXIT_PREFIX_RE = /^\w+_exit_-?\d+:\s*/;

/**
 * Xbox PC (`stores/microsoft/xodus/`). Three families of code:
 *
 * - `xbox_pc_*`: the install precondition, checked before anything is
 *   queued (feature off, components, sign-in, package plan).
 * - `xbox_*`: a refusal or failure of the install, update or uninstall
 *   itself, including the licence stages Xodus reports on stderr.
 * - `xodus_*`: the Xodus CLI adapter (`XodusCliError`). `xodus_exit_N: tail`
 *   is deliberately absent: it falls through to the text rules below, which
 *   read the CLI's own words (disk space, network).
 *
 * `xbox_install_in_progress` reuses `lockConflict`; `xbox_folder_in_use`
 * sits on the shared `folderInUse` row above.
 */
const XBOX_PC_CODE_KEYS: ReadonlyArray<readonly [RegExp, string]> = [
  [/^xbox_pc_disabled$/, "errors.download.xboxPcDisabled"],
  [
    /^xbox_pc_runtime_missing$|^xodus_backend_missing$/,
    "errors.download.xboxPcRuntimeMissing",
  ],
  [/^xodus_backend_tampered$/, "errors.download.xboxPcRuntimeDamaged"],
  [
    /^xbox_pc_not_signed_in$|^xbox_signin_required$|^xodus_credential_[\w-]+$/,
    "errors.download.xboxPcSignIn",
  ],
  [/^xbox_credentials_unavailable$/, "errors.download.xboxPcCredentials"],
  [/^xbox_pc_unsupported_plan$/, "errors.download.xboxPcUnsupportedPlan"],
  [
    /^xbox_pc_needs_choice$|^xbox_package_choice$/,
    "errors.download.xboxPcNeedsChoice",
  ],
  [
    /^xbox_pc_no_package$|^xbox_package_unavailable$/,
    "errors.download.xboxPcNoPackage",
  ],
  // Xbox Live refused the Xodus sign-in. The CLI's sentence says "Check
  // your connection", so without this it reads as a network error.
  [/^xbox_refused$/, "xboxPc.account.errors.xboxRefused"],
  [/^xbox_license_denied$/, "errors.download.xboxPcLicenseDenied"],
  [/^xbox_license_rate_limited$/, "errors.download.xboxPcLicenseRateLimited"],
  [/^xbox_license_device_limit$/, "errors.download.xboxPcLicenseDeviceLimit"],
  [/^xbox_license_unavailable$/, "errors.download.xboxPcLicenseUnavailable"],
  [/^xbox_license_invalid$/, "errors.download.xboxPcLicenseInvalid"],
  [/^xbox_filesystem_unsupported$/, "errors.download.xboxPcFilesystem"],
  [/^xbox_install_incomplete$/, "errors.download.xboxPcIncomplete"],
  [/^xbox_resolve_failed$/, "errors.download.xboxPcResolveFailed"],
  [/^xbox_market_unknown$/, "errors.download.xboxPcMarketUnknown"],
  [/^xbox_already_installed$/, "errors.download.xboxPcAlreadyInstalled"],
  [/^xbox_update_pending$/, "errors.download.xboxPcUpdatePending"],
  [
    /^xbox_game_running$|^xodus_busy_game_running$/,
    "errors.download.xboxPcGameRunning",
  ],
  [/^xbox_install_in_progress$/, "errors.download.lockConflict"],
  // The folder's drive (an SD card, a USB drive) is not mounted. The record
  // is kept, so the game is found again once the drive is back.
  [/^xbox_install_unreachable$/, "errors.download.xboxPcDriveMissing"],
  [/^xbox_old_partial_in_place$/, "errors.download.xboxPcOldPartial"],
  // Play found missing files, but there is no install to repair.
  [/^xbox_repair_no_record$/, "errors.download.xboxPcRepairNoRecord"],
  [/^xodus_components_busy$/, "errors.download.xboxPcComponentsBusy"],
  [
    /^xodus_(panic|empty_response|timeout|bad_json|failed|components_unreadable)$/,
    "errors.download.xboxPcBackendFailed",
  ],
];

/**
 * Bare backend error codes → a translated string.
 *
 * Matched against the code alone (everything before the first `:`), so a
 * `legendary_exit_1: …disk space…` still falls through to the text
 * heuristics below, which are the ones that read the CLI's own words.
 *
 * These four keys existed and were translated in all 16 locales while being
 * referenced by nothing in `src/` — the same shape as the dead toast channel
 * in audit §1.1.2: the strings were written, the delivery was never built.
 * The codes reaching them are mostly GOG's, which returned bare tokens where
 * Epic and Amazon returned a CLI tail (audit §3.2).
 */
const CODE_KEYS: ReadonlyArray<readonly [RegExp, string]> = [
  // A tool that is missing, or present but not executable — retrying cannot
  // help either one, so this must not read as "please try again". Named
  // explicitly rather than as `_not_found$`: that looser form also swallowed
  // `install_path_not_found`, which is a missing game directory, not a
  // missing CLI. Caught by this file's own test, not by review.
  [
    /^(gogdl|legendary|nile|butler)_not_found$|_spawn_failed$/,
    "errors.download.toolMissing",
  ],
  [
    /^install_not_located$|^install_path_not_found$/,
    "errors.download.directoryNotFound",
  ],
  // legendary refuses a second concurrent install and exits 0 while doing it.
  [/^legendary_install_lock_busy$/, "errors.download.lockConflict"],
  [
    /^marker_write_failed$|^mkdir_failed$|^record_write_failed$/,
    "errors.download.processFailed",
  ],
  // The stall watchdog's own message (`InstallStalledError`), which reads
  // `stalled: no output for 120s while downloading`. It is raised for all
  // three CLI stores since the drain loop was shared, and until now it was
  // the one failure that reached the user as raw English — the seconds and
  // the phase are diagnostic detail, not something a user can act on, so a
  // single key carries it. Matched as the bare code `stalled`, because these
  // patterns are tested against the text *before* the first colon, not the
  // whole message. Audit register item 28.
  [/^stalled$/, "errors.download.stalled"],
  // A title the Microsoft store cannot install or uninstall: a stream-only
  // xCloud title, or an Xbox PC title with no install record and no marker
  // (audit §3.5, register item 11). Mapped to the generic string so the
  // user reads a sentence instead of the token `not_supported`, the defect
  // the §3.2 pass fixed for GOG.
  [/^not_supported$/, "errors.download.generic"],
  // itch.io: a game whose uploads are all soundtracks, HTML5, external
  // links or installer formats butler 15.31 no longer runs.
  [/^itch_no_installable_upload$/, "errors.download.noCompatibleBuild"],
  // butler installs to <root>/<game-url-slug>; another store's game already
  // lives there, so the install was refused rather than mixed into it.
  // Xbox PC installs to <base>/Xbox/<PID>, refused the same way.
  [/^itch_folder_in_use$|^xbox_folder_in_use$/, "errors.download.folderInUse"],
  // itch.io: a bundle game not claimed yet. The install flow opens the claim
  // page on this code (`services/store/claim-flow.tsx`); the key is what
  // the user reads if it is reached any other way.
  [/^itch_claim_required$/, "errors.download.claimRequired"],
  // Uninstall refusals of a store that deletes the folder itself.
  [/^uninstall_refused_unsafe_path$/, "errors.download.uninstallRefused"],
  [
    /^uninstall_incomplete_files_remain$/,
    "errors.download.uninstallIncomplete",
  ],
  ...XBOX_PC_CODE_KEYS,
];

interface Classified {
  key: string;
  params?: Record<string, string>;
}

/** The row key (and its params) for a failure, or null for an unknown one. */
function classify(raw: string | undefined): Classified | null {
  if (!raw || !raw.trim()) {
    return { key: "errors.download.generic" };
  }

  const lower = raw.toLowerCase();
  const code = raw.split(":", 1)[0].trim();
  for (const [pattern, key] of CODE_KEYS) {
    if (pattern.test(code)) {
      return { key };
    }
  }

  if (raw.startsWith("insufficient_space:")) {
    const m = SPACE_RE.exec(raw);
    if (m) {
      return {
        key: "errors.download.insufficientSpace",
        params: { need: m[1], free: m[2] },
      };
    }
    return { key: "errors.download.diskSpace" };
  }

  if (
    lower.includes("disk space") ||
    lower.includes("no space") ||
    lower.includes("disk full") ||
    raw.startsWith("low_space:")
  ) {
    return { key: "errors.download.diskSpace" };
  }

  if (lower.includes("network") || lower.includes("connection")) {
    return { key: "errors.download.network" };
  }

  if (lower.includes("login") || lower.includes("auth")) {
    return { key: "errors.download.authExpired" };
  }

  return null;
}

/** The failure as a sentence, for the failed-download row. */
export function friendlyDownloadError(
  raw: string | undefined,
  t: TFunction,
): string {
  const found = classify(raw);
  if (found) return t(found.key, found.params);
  // Unknown failure — surface the real tail so nothing is ever silent.
  return (
    (raw ?? "").replace(EXIT_PREFIX_RE, "").trim() ||
    t("errors.download.generic")
  );
}

/**
 * The failure as a toast body: the row key's short twin under
 * `toasts.downloadError.*` (`TOAST_KEYS`). A toast has no room
 * for the raw tail an unknown failure shows in the row, so that one reads
 * as the generic reason; the row and the log keep the detail.
 */
export function downloadErrorToast(raw: string | undefined): ToastString {
  const key = classify(raw)?.key ?? "errors.download.generic";
  return toastText(TOAST_KEYS[key] ?? "toasts.downloadError.generic");
}

/** Row key → its short toast twin. */
const TOAST_KEYS: Readonly<Record<string, ToastKey>> = {
  "errors.download.authExpired": "toasts.downloadError.authExpired",
  "errors.download.claimRequired": "toasts.downloadError.claimRequired",
  "errors.download.directoryNotFound": "toasts.downloadError.directoryNotFound",
  "errors.download.diskSpace": "toasts.downloadError.diskSpace",
  "errors.download.folderInUse": "toasts.downloadError.folderInUse",
  "errors.download.generic": "toasts.downloadError.generic",
  "errors.download.insufficientSpace": "toasts.downloadError.diskSpace",
  "errors.download.lockConflict": "toasts.downloadError.lockConflict",
  "errors.download.network": "toasts.downloadError.network",
  "errors.download.noCompatibleBuild": "toasts.downloadError.noCompatibleBuild",
  "errors.download.processFailed": "toasts.downloadError.processFailed",
  "errors.download.stalled": "toasts.downloadError.stalled",
  "errors.download.toolMissing": "toasts.downloadError.toolMissing",
  "errors.download.uninstallIncomplete":
    "toasts.downloadError.uninstallIncomplete",
  "errors.download.uninstallRefused": "toasts.downloadError.uninstallRefused",
  "errors.download.xboxPcAlreadyInstalled":
    "toasts.downloadError.xboxPcAlreadyInstalled",
  "errors.download.xboxPcBackendFailed":
    "toasts.downloadError.xboxPcBackendFailed",
  "errors.download.xboxPcComponentsBusy":
    "toasts.downloadError.xboxPcComponentsBusy",
  "errors.download.xboxPcCredentials": "toasts.downloadError.xboxPcCredentials",
  "errors.download.xboxPcDisabled": "toasts.downloadError.xboxPcDisabled",
  "errors.download.xboxPcDriveMissing":
    "toasts.downloadError.xboxPcDriveMissing",
  "errors.download.xboxPcFilesystem": "toasts.downloadError.xboxPcFilesystem",
  "errors.download.xboxPcGameRunning": "toasts.downloadError.xboxPcGameRunning",
  "errors.download.xboxPcIncomplete": "toasts.downloadError.xboxPcIncomplete",
  "errors.download.xboxPcLicenseDenied":
    "toasts.downloadError.xboxPcLicenseDenied",
  "errors.download.xboxPcLicenseDeviceLimit":
    "toasts.downloadError.xboxPcLicenseDeviceLimit",
  "errors.download.xboxPcLicenseInvalid":
    "toasts.downloadError.xboxPcLicenseInvalid",
  "errors.download.xboxPcLicenseRateLimited":
    "toasts.downloadError.xboxPcLicenseRateLimited",
  "errors.download.xboxPcLicenseUnavailable":
    "toasts.downloadError.xboxPcLicenseUnavailable",
  "errors.download.xboxPcMarketUnknown":
    "toasts.downloadError.xboxPcMarketUnknown",
  "errors.download.xboxPcNeedsChoice": "toasts.downloadError.xboxPcNeedsChoice",
  "errors.download.xboxPcNoPackage": "toasts.downloadError.xboxPcNoPackage",
  "errors.download.xboxPcOldPartial": "toasts.downloadError.xboxPcOldPartial",
  "errors.download.xboxPcRepairNoRecord":
    "toasts.downloadError.xboxPcRepairNoRecord",
  "errors.download.xboxPcResolveFailed":
    "toasts.downloadError.xboxPcResolveFailed",
  "errors.download.xboxPcRuntimeDamaged":
    "toasts.downloadError.xboxPcRuntimeDamaged",
  "errors.download.xboxPcRuntimeMissing":
    "toasts.downloadError.xboxPcRuntimeMissing",
  "errors.download.xboxPcSignIn": "toasts.downloadError.xboxPcSignIn",
  "errors.download.xboxPcUnsupportedPlan":
    "toasts.downloadError.xboxPcUnsupportedPlan",
  "errors.download.xboxPcUpdatePending":
    "toasts.downloadError.xboxPcUpdatePending",
  "xboxPc.account.errors.xboxRefused": "toasts.downloadError.xboxRefused",
};
