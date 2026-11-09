import { describe, expect, it } from "vitest";
import { resolveNativeAction } from "./native-action";
import { DisplayStatus } from "./overview-proxy";
import type { PlaySectionState } from "../../../hooks/resolvePlaySection";
import type { DownloadItem } from "../../../types/downloads";

const download = { id: "gog:1", game_id: "1" } as unknown as DownloadItem;

const states: Record<string, PlaySectionState> = {
  loading: { kind: "steam-native", shouldOverride: false },
  unavailable: { kind: "unavailable", shouldOverride: false },
  notInstalled: { kind: "not-installed", shouldOverride: true, installable: true },
  downloading: { kind: "downloading", shouldOverride: true, download },
  installed: { kind: "installed", shouldOverride: true, appId: 3000000001 },
  browser: {
    kind: "browser",
    shouldOverride: true,
    appId: 3000000001,
    variant: "stream",
    url: "https://x",
  },
  pcOff: {
    kind: "pc-unsupported",
    shouldOverride: true,
    appId: 3000000001,
    reason: "disabled",
    installed: true,
  },
  pcBlocked: {
    kind: "pc-unsupported",
    shouldOverride: true,
    appId: 3000000001,
    reason: "needs_choice",
    installed: false,
  },
};

describe("resolveNativeAction", () => {
  it.each([1, 4, 36])("leaves Steam in charge while Steam's live status is %i", (live) => {
    for (const state of Object.values(states)) {
      expect(resolveNativeAction(state, "ready", live, false)).toEqual({
        status: null,
        intent: { kind: "native" },
      });
    }
  });

  it("never touches a shortcut that is not ours", () => {
    expect(resolveNativeAction(states.loading, "missing", 9, false).intent).toEqual({
      kind: "native",
    });
  });

  it("lets Steam's Play run while our answer is pending", () => {
    for (const s of [states.loading, states.unavailable]) {
      expect(resolveNativeAction(s, "loading", DisplayStatus.Play, false)).toEqual({
        status: null,
        intent: { kind: "native" },
      });
    }
  });

  it("swallows Steam's Install while our answer is pending", () => {
    // Steam's own Install would open its install wizard for a shortcut.
    for (const s of [states.loading, states.unavailable]) {
      expect(resolveNativeAction(s, "loading", DisplayStatus.Install, false)).toEqual({
        status: DisplayStatus.Install,
        intent: { kind: "none" },
      });
    }
  });

  it("maps each of our states to Steam's status and our click", () => {
    expect(resolveNativeAction(states.notInstalled, "ready", 9, false)).toEqual({
      status: DisplayStatus.Install,
      intent: { kind: "install" },
    });
    expect(resolveNativeAction(states.downloading, "ready", 9, false)).toEqual({
      status: DisplayStatus.Cancel,
      intent: { kind: "cancel", download },
    });
    expect(resolveNativeAction(states.installed, "ready", 11, false)).toEqual({
      status: DisplayStatus.Play,
      intent: { kind: "play" },
    });
    expect(resolveNativeAction(states.installed, "ready", 11, true)).toEqual({
      status: DisplayStatus.Update,
      intent: { kind: "update" },
    });
    expect(resolveNativeAction(states.browser, "ready", 9, false)).toEqual({
      status: DisplayStatus.Play,
      intent: { kind: "browser", variant: "stream", url: "https://x" },
    });
  });

  it("shows a disabled button for an Xbox PC title that cannot install or run", () => {
    expect(resolveNativeAction(states.pcOff, "ready", 11, false)).toEqual({
      status: DisplayStatus.PlayUnavailable,
      intent: { kind: "none" },
    });
    expect(resolveNativeAction(states.pcBlocked, "ready", 9, false)).toEqual({
      status: DisplayStatus.InstallUnavailable,
      intent: { kind: "none" },
    });
  });
});
