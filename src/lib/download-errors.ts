/**
 * download-errors — map a backend download-failure code/message to a
 * friendly, localized string.
 *
 * The backend folds the store CLI's real error tail into
 * `error_message` (e.g. `insufficient_space:need=66.4GB,free=43.5GB` from
 * the size-aware preflight, or `legendary_exit_1: … Not enough available
 * disk space! …` when legendary itself aborts). Both the failure toast
 * (`download-store`) and the failed-row detail (`DownloadItemRow`) render
 * through this one helper so they always agree.
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

/** Pull `need`/`free` GB out of an `insufficient_space:…` code. */
const SPACE_RE = /need=([\d.]+)GB,free=([\d.]+)GB/;
/** Strip a leading machine prefix like `legendary_exit_1: ` / `gogdl_exit_2: `. */
const EXIT_PREFIX_RE = /^\w+_exit_-?\d+:\s*/;

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
    /^(gogdl|legendary|nile)_not_found$|_spawn_failed$/,
    "errors.download.toolMissing",
  ],
  [
    /^install_not_located$|^install_path_not_found$/,
    "errors.download.directoryNotFound",
  ],
  // legendary refuses a second concurrent install and exits 0 while doing it.
  [/^legendary_install_lock_busy$/, "errors.download.lockConflict"],
  [/^marker_write_failed$|^mkdir_failed$/, "errors.download.processFailed"],
  // The stall watchdog's own message (`InstallStalledError`), which reads
  // `stalled: no output for 120s while downloading`. It is raised for all
  // three CLI stores since the drain loop was shared, and until now it was
  // the one failure that reached the user as raw English — the seconds and
  // the phase are diagnostic detail, not something a user can act on, so a
  // single key carries it. Matched as the bare code `stalled`, because these
  // patterns are tested against the text *before* the first colon, not the
  // whole message. Audit register item 28.
  [/^stalled$/, "errors.download.stalled"],
  // A store that cannot install at all (the cloud-only store refuses every
  // install / update / uninstall — audit §3.5, register item 11). Mapped to
  // the generic string rather than given its own key: the path is guarded
  // twice over on the way in, so this exists to guarantee that if it is ever
  // reached the user gets a sentence instead of the token `not_supported` —
  // which is exactly the defect the §3.2 pass fixed for GOG. Give it a
  // precise string of its own the day something can actually reach it.
  [/^not_supported$/, "errors.download.generic"],
];

export function friendlyDownloadError(
  raw: string | undefined,
  t: TFunction,
): string {
  if (!raw || !raw.trim()) {
    return t("errors.download.generic");
  }

  const lower = raw.toLowerCase();
  const code = raw.split(":", 1)[0].trim();
  for (const [pattern, key] of CODE_KEYS) {
    if (pattern.test(code)) {
      return t(key);
    }
  }

  if (raw.startsWith("insufficient_space:")) {
    const m = SPACE_RE.exec(raw);
    if (m) {
      return t("errors.download.insufficientSpace", {
        need: m[1],
        free: m[2],
      });
    }
    return t("errors.download.diskSpace");
  }

  if (
    lower.includes("disk space") ||
    lower.includes("no space") ||
    lower.includes("disk full") ||
    raw.startsWith("low_space:")
  ) {
    return t("errors.download.diskSpace");
  }

  if (lower.includes("network") || lower.includes("connection")) {
    return t("errors.download.network");
  }

  if (lower.includes("login") || lower.includes("auth")) {
    return t("errors.download.authExpired");
  }

  // Unknown failure — surface the real tail so nothing is ever silent.
  return raw.replace(EXIT_PREFIX_RE, "").trim() || t("errors.download.generic");
}
