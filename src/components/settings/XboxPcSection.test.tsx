/**
 * The small line under the Xbox PC toggle: the runtime version once
 * installed, the download percentage while installing, and the error
 * (naming where it looked) when the components could not be installed.
 */
import { describe, it, expect, vi } from "vitest";
import type { TFunction } from "i18next";

vi.mock("@decky/ui", () => ({}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("../../hooks/useXboxPcStatus", () => ({}));
vi.mock("../../stores/xbox-pc-store", () => ({}));
vi.mock("../../services/store/xbox-pc-actions", () => ({}));

import { runtimeLine } from "./XboxPcSection";
import type { XboxPcStatus } from "../../types/api";

const t = ((key: string, vars?: Record<string, unknown>) =>
  vars ? `${key} ${JSON.stringify(vars)}` : key) as unknown as TFunction;

function runtime(over: Partial<XboxPcStatus["runtime"]>): XboxPcStatus["runtime"] {
  return {
    state: "missing",
    version: null,
    source_dir: "github.com/mubaraknumann/unifideck",
    degraded: [],
    ...over,
  };
}

describe("runtimeLine", () => {
  it("shows the installed version", () => {
    expect(runtimeLine(runtime({ state: "installed", version: "0.1.11" }), t)).toBe(
      'xboxPc.section.runtimeVersion {"version":"0.1.11"}',
    );
  });

  it("says an update is available", () => {
    expect(runtimeLine(runtime({ state: "outdated", version: "0.1.10" }), t)).toBe(
      'xboxPc.section.runtimeUpdateAvailable {"version":"0.1.10"}',
    );
  });

  it("shows the download percentage, then installing", () => {
    const downloading = runtime({
      state: "installing",
      progress: { stage: "downloading", done_bytes: 421, total_bytes: 1000 },
    });
    expect(runtimeLine(downloading, t)).toBe('xboxPc.section.runtimeDownloading {"percent":42}');
    const extracting = runtime({
      state: "installing",
      progress: { stage: "extracting", done_bytes: 1, total_bytes: 2 },
    });
    expect(runtimeLine(extracting, t)).toBe("xboxPc.section.runtimeInstalling");
  });

  it("names where it looked when the release can't be reached", () => {
    const failed = runtime({
      state: "failed",
      error: "release_unreachable:github.com/mubaraknumann/unifideck",
    });
    expect(runtimeLine(failed, t)).toContain("xboxPc.runtime.errors.releaseUnreachable");
    expect(runtimeLine(failed, t)).toContain("github.com/mubaraknumann/unifideck");
  });

  it("is empty when nothing is installed and nothing failed", () => {
    expect(runtimeLine(runtime({}), t)).toBeUndefined();
  });
});
