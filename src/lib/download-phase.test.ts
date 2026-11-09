/**
 * What a running download row says in each queue phase.
 *
 * The bug that prompted this: an Xbox PC download showed "Setting up game…
 * Initializing prefix and syncing cloud saves…" before its first byte. The
 * backend sent `preparing`, which is the other stores' post-download prefix
 * setup, for the license check and the package read. Nothing built a
 * prefix. Those steps now arrive as `starting`, and the write-out after the
 * last byte as `finalizing` rather than `verifying`.
 *
 * The phase list is a `Record` over the union, so adding a phase without
 * adding it here fails `tsc`, and the locale check below then covers it.
 */
import { describe, expect, it } from "vitest";
import type { DownloadPhase } from "../types/downloads";
import { detailKey, isIndeterminate, showsPercent, statusLabelKey } from "./download-phase";

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

const PHASES: Record<DownloadPhase, true> = {
  downloading: true,
  extracting: true,
  verifying: true,
  complete: true,
  manual: true,
  preparing: true,
  starting: true,
  finalizing: true,
};
const ALL_PHASES = Object.keys(PHASES) as DownloadPhase[];

/** Listed, not globbed: a glob that matches nothing passes silently. */
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

describe("the pre-download phase", () => {
  it("reads as preparing the download, not as setting up the game", () => {
    expect(statusLabelKey("running", "starting", false)).toBe("downloadsTab.preparingDownload");
    expect(detailKey("starting")).toBe("downloadsTab.startingMessage");
  });

  it("says nothing about a prefix or cloud saves", () => {
    expect(statusLabelKey("running", "starting", false)).not.toBe(
      statusLabelKey("running", "preparing", false),
    );
    expect(detailKey("starting")).not.toBe(detailKey("preparing"));
    const text = String(lookup(en, detailKey("starting"))).toLowerCase();
    expect(text).not.toMatch(/prefix|cloud|save/);
  });

  it("is indeterminate and leaves the 0% off the Cancel button", () => {
    expect(isIndeterminate("starting")).toBe(true);
    expect(showsPercent("starting")).toBe(false);
  });
});

describe("the write-out after the last byte", () => {
  it("reads as finalizing, not verifying", () => {
    expect(statusLabelKey("running", "finalizing", false)).toBe("downloadsTab.finalizingLabel");
    expect(detailKey("finalizing")).toBe("downloadsTab.finalizingInstallation");
    expect(isIndeterminate("finalizing")).toBe(true);
    expect(showsPercent("finalizing")).toBe(false);
  });
});

describe("the other stores' phases are unchanged", () => {
  it.each([
    ["preparing", "downloadsTab.preparingLabel", "downloadsTab.preparingMessage"],
    ["extracting", "downloadsTab.extractingLabel", "downloadsTab.extractingMessage"],
    ["verifying", "downloadsTab.verifyingLabel", "downloadsTab.verifyingMessage"],
    ["manual", "downloadsTab.installingViaClientLabel", "downloadsTab.installingViaClientMessage"],
  ] as const)("%s", (phase, label, detail) => {
    expect(statusLabelKey("running", phase, false)).toBe(label);
    expect(detailKey(phase)).toBe(detail);
    expect(isIndeterminate(phase)).toBe(true);
  });

  it("downloading shows bytes, and the label follows status and update", () => {
    expect(isIndeterminate("downloading")).toBe(false);
    expect(isIndeterminate(undefined)).toBe(false);
    expect(statusLabelKey("running", "downloading", false)).toBe("downloadsTab.downloadingLabel");
    expect(statusLabelKey("running", "downloading", true)).toBe(
      "downloadsTab.downloadingUpdateLabel",
    );
    expect(statusLabelKey("queued", "downloading", false)).toBe("downloadsTab.downloadQueuedLabel");
    expect(statusLabelKey("queued", undefined, true)).toBe("downloadsTab.updateQueuedLabel");
  });

  it("the Cancel button shows a percentage where it did before", () => {
    const withPercent = ALL_PHASES.filter((p) => showsPercent(p)).sort();
    expect(withPercent).toEqual(["complete", "downloading", "verifying"]);
    expect(showsPercent(undefined)).toBe(true);
  });
});

describe("every key a phase picks exists in every locale", () => {
  it("covers all 16 locales", () => {
    expect(LOCALES).toHaveLength(16);
  });

  const keys = new Set<string>();
  for (const phase of ALL_PHASES) {
    keys.add(statusLabelKey("running", phase, false));
    keys.add(statusLabelKey("queued", phase, true));
    if (isIndeterminate(phase)) keys.add(detailKey(phase));
  }

  it.each(LOCALES)("%s", (_code, locale) => {
    for (const key of keys) {
      const value = lookup(locale, key);
      expect(typeof value, key).toBe("string");
      expect(String(value).length, key).toBeGreaterThan(0);
    }
  });
});
