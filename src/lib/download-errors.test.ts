/**
 * A failed install must say why, in the user's language, for every store.
 *
 * Audit §3.2: Epic and Amazon folded their CLI's output tail into the error
 * (`legendary_exit_1: …`, `nile_exit_1: …`) — both were deliberate fixes for
 * bug reports about silent failures. GOG never got one. It returned five bare
 * codes, none of which is an i18n key, so `friendlyDownloadError` fell through
 * to echoing the raw string: a GOG install that died from a full disk showed
 * the literal token `download_failed` in all 16 locales, where Epic showed
 * "Not enough disk space available" for the identical cause.
 *
 * These tests assert the *rendered* result, not the emitted code. §1.1.1's
 * lesson: the two guards there each pinned one half of a payload mismatch and
 * so caught nothing between them.
 */
import { describe, it, expect } from "vitest";
import { friendlyDownloadError } from "./download-errors";

import ar from "../i18n/locales/ar-SA.json";
import de from "../i18n/locales/de-DE.json";
import en from "../i18n/locales/en-US.json";
import es from "../i18n/locales/es-ES.json";
import fr from "../i18n/locales/fr-FR.json";
import itIT from "../i18n/locales/it-IT.json";
import ja from "../i18n/locales/ja-JP.json";
import ko from "../i18n/locales/ko-KR.json";
import nl from "../i18n/locales/nl-NL.json";
import pl from "../i18n/locales/pl-PL.json";
import pt from "../i18n/locales/pt-BR.json";
import ru from "../i18n/locales/ru-RU.json";
import tr from "../i18n/locales/tr-TR.json";
import uk from "../i18n/locales/uk-UA.json";
import zhCN from "../i18n/locales/zh-CN.json";
import zhTW from "../i18n/locales/zh-TW.json";

/** Identity `t` — these tests care which key is chosen, not its text. */
const t = ((key: string, params?: unknown) =>
  params ? `${key}(${JSON.stringify(params)})` : key) as never;

/**
 * Every locale, so a mapped key cannot be untranslated in one of them.
 * Listed explicitly rather than globbed: a glob silently passes when it
 * matches nothing, which is the failure mode a coverage test exists to
 * prevent. The count assertion below is the backstop if one is added.
 */
const LOCALES: ReadonlyArray<readonly [string, Record<string, unknown>]> = [
  ["ar-SA", ar],
  ["de-DE", de],
  ["en-US", en],
  ["es-ES", es],
  ["fr-FR", fr],
  ["it-IT", itIT],
  ["ja-JP", ja],
  ["ko-KR", ko],
  ["nl-NL", nl],
  ["pl-PL", pl],
  ["pt-BR", pt],
  ["ru-RU", ru],
  ["tr-TR", tr],
  ["uk-UA", uk],
  ["zh-CN", zhCN],
  ["zh-TW", zhTW],
];

function lookup(obj: Record<string, unknown>, dotted: string): unknown {
  return dotted
    .split(".")
    .reduce<unknown>((acc, part) => (acc as Record<string, unknown> | undefined)?.[part], obj);
}

describe("friendlyDownloadError — CLI tails", () => {
  it("classifies a GOG disk-full tail the way it already did for Epic", () => {
    // The whole point of giving GOG the `gogdl_exit_N: <tail>` shape: the
    // classifier reads the CLI's own words, so this needed no new strings.
    expect(
      friendlyDownloadError("gogdl_exit_1: [cli] ERROR: Not enough available disk space!", t),
    ).toBe("errors.download.diskSpace");
    expect(
      friendlyDownloadError("legendary_exit_1: [cli] ERROR: Not enough available disk space!", t),
    ).toBe("errors.download.diskSpace");
  });

  it("classifies a dropped connection for GOG", () => {
    expect(friendlyDownloadError("gogdl_exit_2: connection reset by peer", t)).toBe(
      "errors.download.network",
    );
  });

  it("still surfaces an unrecognized tail verbatim rather than swallowing it", () => {
    expect(friendlyDownloadError("gogdl_exit_3: something weird", t)).toBe("something weird");
  });
});

describe("friendlyDownloadError — bare backend codes", () => {
  it.each([
    ["gogdl_not_found", "errors.download.toolMissing"],
    ["legendary_not_found", "errors.download.toolMissing"],
    ["nile_not_found", "errors.download.toolMissing"],
    // A tool that exists but will not exec (lost exec bit) — "try again"
    // would be the wrong advice, so it shares the missing-tool string.
    ["gogdl_spawn_failed: [Errno 13] Permission denied", "errors.download.toolMissing"],
    ["nile_spawn_failed: [Errno 13] Permission denied", "errors.download.toolMissing"],
    ["install_not_located", "errors.download.directoryNotFound"],
    ["install_path_not_found", "errors.download.directoryNotFound"],
    ["marker_write_failed", "errors.download.processFailed"],
    ["mkdir_failed: read-only file system", "errors.download.processFailed"],
    [
      "legendary_install_lock_busy: another Epic install is still holding the lock",
      "errors.download.lockConflict",
    ],
    // The stall watchdog, which fires for all three CLI stores since the
    // drain loop was shared. Both the seconds and the phase word vary, and
    // the classifier matches the text before the first colon, so the rule
    // is on the bare code `stalled` (audit register item 28).
    ["stalled: no output for 120s while downloading", "errors.download.stalled"],
    ["stalled: no output for 600s while finalizing", "errors.download.stalled"],
  ])("maps %s", (raw, key) => {
    expect(friendlyDownloadError(raw, t)).toBe(key);
  });

  it("leaves not_authenticated on the existing auth branch", () => {
    expect(friendlyDownloadError("not_authenticated", t)).toBe("errors.download.authExpired");
  });

  it("does not let a code pattern hijack a message that merely contains it", () => {
    // Only the part before the first ':' is treated as a code, so a CLI tail
    // mentioning a path must still reach the text heuristics.
    expect(
      friendlyDownloadError("gogdl_exit_1: could not open /opt/not_found: disk space exhausted", t),
    ).toBe("errors.download.diskSpace");
  });

  it("renders a store's not_supported refusal as a sentence", () => {
    // The cloud-only store refuses install / update / uninstall outright
    // (audit §3.5, register item 11). Unmapped, this would echo the bare
    // token `not_supported` — the exact defect the §3.2 pass fixed for
    // GOG's `download_failed`. Guarded even though the path is closed
    // twice over on the way in, because that is what makes it safe to
    // leave closed.
    expect(friendlyDownloadError("not_supported", t)).toBe("errors.download.generic");
  });

  it("does not swallow a longer code that merely ends in not_supported", () => {
    // Anchored both ends: a future `dlc_not_supported` is a different
    // failure and must not inherit this string. The `_not_found$` pattern
    // in this file already made the unanchored version of this mistake.
    expect(friendlyDownloadError("dlc_not_supported", t)).toBe("dlc_not_supported");
  });
});

describe("every key this maps to is translated everywhere", () => {
  // The four codes above route to keys that existed, were translated in all
  // 16 locales, and were referenced by nothing in src/ before this change —
  // strings written with no delivery path, as in audit §1.1.2. Guard against
  // the reverse mistake: a mapping to a key that is missing somewhere.
  const KEYS = [
    "errors.download.toolMissing",
    "errors.download.directoryNotFound",
    "errors.download.processFailed",
    "errors.download.lockConflict",
    "errors.download.stalled",
    "errors.download.diskSpace",
    "errors.download.network",
    "errors.download.authExpired",
    "errors.download.generic",
  ];

  it("has all 16 locales loaded", () => {
    expect(LOCALES.length).toBe(16);
  });

  it.each(KEYS)("%s exists in en-US", (key) => {
    expect(lookup(en as Record<string, unknown>, key)).toBeTruthy();
  });

  it.each(LOCALES)("%s translates every mapped key", (_name, locale) => {
    for (const key of KEYS) {
      expect(lookup(locale, key)).toBeTruthy();
    }
  });
});
