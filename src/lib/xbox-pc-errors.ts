/**
 * xbox-pc-errors: turn an Xbox PC status or RPC error code into a
 * translated sentence.
 *
 * The backend reports component, sign-in and setup failures as bare codes,
 * sometimes with a detail after the first colon (`source_missing:/path`).
 * The QAM rows, the setup modal and the sign-in flow all show them, so the
 * code-to-key table lives here once. An unknown code still reaches the user,
 * inside a generic sentence, so a new backend code is never silent.
 *
 * Install refusals (`xbox_pc_*`, `xbox_*`, `xodus_*` from `install_game`)
 * are not mapped here. They go through `friendlyDownloadError`, like every
 * other store's install failure.
 */
import type { TFunction } from "i18next";

/** Exact codes (the part before the first `:`) and their keys. */
const EXACT_KEYS: Readonly<Record<string, string>> = {
  source_incomplete: "xboxPc.runtime.errors.sourceIncomplete",
  source_ambiguous: "xboxPc.runtime.errors.sourceAmbiguous",
  source_unsupported: "xboxPc.runtime.errors.sourceUnsupported",
  release_unreachable: "xboxPc.runtime.errors.releaseUnreachable",
  release_not_found: "xboxPc.runtime.errors.releaseNotFound",
  release_download_failed: "xboxPc.runtime.errors.releaseDownloadFailed",
  release_mismatch: "xboxPc.runtime.errors.releaseMismatch",
  insufficient_space: "xboxPc.runtime.errors.insufficientSpace",
  backend_checksum_mismatch: "xboxPc.runtime.errors.checksum",
  runtime_checksum_mismatch: "xboxPc.runtime.errors.checksum",
  checksum_mismatch: "xboxPc.runtime.errors.checksum",
  runtime_invalid: "xboxPc.runtime.errors.invalid",
  runtime_backend_incompatible: "xboxPc.runtime.errors.invalid",
  backend_missing: "xboxPc.runtime.errors.backendMissing",
  backend_tampered: "xboxPc.runtime.errors.backendTampered",
  install_in_progress: "xboxPc.runtime.errors.inProgress",
  cancelled: "xboxPc.runtime.errors.cancelled",
  microsoft_sign_in_required: "xboxPc.account.errors.microsoftRequired",
  edge_not_installed: "xboxPc.account.errors.edgeMissing",
  edge_busy: "xboxPc.account.errors.edgeBusy",
  auth_in_progress: "xboxPc.account.errors.inProgress",
  window_not_opened: "xboxPc.account.errors.windowNotOpened",
  window_closed: "xboxPc.account.errors.windowClosed",
  signin_timeout: "xboxPc.account.errors.timeout",
  not_stored: "xboxPc.account.errors.notStored",
  account_mismatch: "xboxPc.account.errors.mismatch",
  xbox_refused: "xboxPc.account.errors.xboxRefused",
  credential_store_unavailable: "xboxPc.account.errors.credentialStore",
  busy_game_running: "xboxPc.account.errors.gameRunning",
  xodus_busy_game_running: "xboxPc.account.errors.gameRunning",
  runtime_missing: "xboxPc.account.needsRuntime",
  notice_not_acknowledged: "xboxPc.section.noticeRequired",
  status_unavailable: "xboxPc.section.statusError",
  components_busy: "xboxPc.runtime.errors.componentsBusy",
  not_writable: "xboxPc.runtime.errors.notWritable",
  mkdir_failed: "xboxPc.runtime.errors.notWritable",
  install_io_error: "xboxPc.runtime.errors.ioError",
  install_failed: "xboxPc.runtime.errors.installFailed",
  xodus_busy: "xboxPc.account.errors.xodusBusy",
  xodus_not_ready: "xboxPc.account.errors.xodusNotReady",
  xodus_protocol_mismatch: "xboxPc.account.errors.xodusNotReady",
  spawn_failed: "xboxPc.account.errors.spawnFailed",
  xodus_spawn_failed: "xboxPc.account.errors.spawnFailed",
  signin_failed: "xboxPc.account.errors.signinFailed",
  url_write_failed: "xboxPc.account.errors.signinFailed",
  xbox_pc_disabled: "xboxPc.errors.disabled",
  xbox_pc_unavailable: "xboxPc.errors.unavailable",
};

/**
 * Prefix families, first match wins: any `xodus_credential_*` is a
 * credential-store failure, and any other `xodus_*` is a failed Xodus check
 * that names its code.
 */
const PREFIX_KEYS: ReadonlyArray<readonly [string, string]> = [
  ["xodus_credential_", "xboxPc.account.errors.credentialStore"],
  ["xodus_", "xboxPc.account.errors.xodusFailed"],
];

/** Split `code:detail` into its two halves (detail may be empty). */
export function splitCode(raw: string): { code: string; detail: string } {
  const at = raw.indexOf(":");
  if (at < 0) return { code: raw.trim(), detail: "" };
  return { code: raw.slice(0, at).trim(), detail: raw.slice(at + 1).trim() };
}

/** The i18n key for `raw`, or null when the code is not known. */
export function xboxPcErrorKey(raw: string): string | null {
  const { code } = splitCode(raw);
  if (code === "source_missing") return "xboxPc.runtime.errors.sourceMissing";
  const exact = EXACT_KEYS[code];
  if (exact) return exact;
  for (const [prefix, key] of PREFIX_KEYS) {
    if (code.startsWith(prefix)) return key;
  }
  return null;
}

/**
 * A translated sentence for an Xbox PC error code.
 *
 * `sourceDir` names where the components were looked for. It fills the
 * `{{path}}` of a bare `source_missing` (rule 8: say where it looked).
 */
export function xboxPcErrorText(
  raw: string | null | undefined,
  t: TFunction,
  sourceDir?: string,
): string {
  const value = (raw ?? "").trim();
  if (!value) return t("xboxPc.errors.generic", { code: "unknown" });
  const key = xboxPcErrorKey(value);
  if (!key) return t("xboxPc.errors.generic", { code: splitCode(value).code });
  const { code, detail } = splitCode(value);
  return t(key, { path: detail || sourceDir || "", code });
}

/**
 * Why a row tagged `xbox_pc` cannot be installed from its game page.
 *
 * `disabled` and `status_unavailable` come from the live status; the rest
 * come from the row's `metadata.xbox_pc` (its plan, or a package choice
 * that v1 has no picker for).
 */
export type PcUnsupportedReason =
  | "disabled"
  | "status_unavailable"
  | "ea_play"
  | "ubisoft_plus"
  | "unknown_plan"
  | "needs_choice"
  | "unavailable";

const REASON_KEYS: Readonly<Record<PcUnsupportedReason, string>> = {
  disabled: "xboxPc.unsupported.disabled",
  status_unavailable: "xboxPc.unsupported.statusUnavailable",
  ea_play: "xboxPc.unsupported.eaPlay",
  ubisoft_plus: "xboxPc.unsupported.ubisoftPlus",
  unknown_plan: "xboxPc.unsupported.unknownPlan",
  needs_choice: "xboxPc.unsupported.needsChoice",
  unavailable: "xboxPc.unsupported.unavailable",
};

/** The reason line's i18n key. */
export function pcUnsupportedKey(reason: PcUnsupportedReason): string {
  return REASON_KEYS[reason];
}

/** Narrow a backend `reason`/`access` string to a known reason. */
export function asPcUnsupportedReason(
  value: string | null | undefined,
): PcUnsupportedReason | null {
  if (!value) return null;
  return value in REASON_KEYS ? (value as PcUnsupportedReason) : null;
}
