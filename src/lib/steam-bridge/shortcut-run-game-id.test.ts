// @vitest-environment jsdom
/**
 * `RunGame` must be handed the shortcut's OWN 64-bit gameID.
 *
 * Field case (2026-08-25 bundle): Ys I and Trails in the Sky launched, ran
 * and played audio, but sat behind Steam's loading screen with only *Abort*.
 * Steam's own logs show why — the launch went out under a bare Steam appid
 * belonging to a different app:
 *
 *   Adding process 3011 for gameID 223810
 *   AppID 223810 adding PID 3011 ... reaper SteamLaunch AppId=3969905431
 *
 * 223810 is the Steam store appid of Ys I&II Chronicles+, which the tester
 * also owns on Steam. Every non-Unifideck shortcut on the same machine
 * launched with a proper 64-bit gameid. Steam therefore waited on a window
 * for an app that never started.
 *
 * `getShortcutRunGameId` trusted `appStore.m_mapApps.get(appId).gameid`
 * unconditionally. It now verifies the id actually belongs to this shortcut
 * before using it.
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { getShortcutRunGameId } from "./shortcut-types";

const YS_SIGNED = -325061865;
const YS_UNSIGNED = 3969905431;
/** The gameID Steam uses for this shortcut: (appid << 32) | 0x02000000. */
const YS_GAMEID = ((BigInt(YS_UNSIGNED) << 32n) | 0x02000000n).toString();
/** Steam store appid of Ys I&II Chronicles+ — the wrong answer. */
const YS_STEAM_APPID = "223810";

/** Install a fake `appStore` whose map holds the given entries. */
function withAppStore(entries: Record<number, unknown> | null): void {
  const win = window as unknown as { appStore?: unknown };
  if (entries === null) {
    delete win.appStore;
    return;
  }
  const map = new Map<number, unknown>(Object.entries(entries).map(([k, v]) => [Number(k), v]));
  win.appStore = { m_mapApps: map };
}

afterEach(() => {
  withAppStore(null);
  vi.restoreAllMocks();
});

describe("getShortcutRunGameId", () => {
  it("uses the stored gameid when it belongs to this shortcut", () => {
    withAppStore({ [YS_SIGNED]: { gameid: YS_GAMEID } });
    expect(getShortcutRunGameId(YS_UNSIGNED)).toBe(YS_GAMEID);
  });

  it("rejects a gameid belonging to a different app and recomputes", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    withAppStore({ [YS_SIGNED]: { gameid: YS_STEAM_APPID } });
    expect(getShortcutRunGameId(YS_UNSIGNED)).toBe(YS_GAMEID);
  });

  it("warns when it rejects one, so the next bundle shows it", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    withAppStore({ [YS_SIGNED]: { gameid: YS_STEAM_APPID } });
    getShortcutRunGameId(YS_UNSIGNED);
    expect(warn).toHaveBeenCalledOnce();
  });

  it("computes the gameid when Steam has not filled one in", () => {
    withAppStore({ [YS_SIGNED]: {} });
    expect(getShortcutRunGameId(YS_UNSIGNED)).toBe(YS_GAMEID);
  });

  it("computes the gameid when the shortcut is not in the map at all", () => {
    withAppStore({});
    expect(getShortcutRunGameId(YS_UNSIGNED)).toBe(YS_GAMEID);
  });

  it("falls back cleanly on a non-numeric gameid", () => {
    withAppStore({ [YS_SIGNED]: { gameid: "not-a-number" } });
    expect(getShortcutRunGameId(YS_UNSIGNED)).toBe(YS_GAMEID);
  });

  it("finds the entry whether the map is keyed signed or unsigned", () => {
    withAppStore({ [YS_UNSIGNED]: { gameid: YS_GAMEID } });
    expect(getShortcutRunGameId(YS_UNSIGNED)).toBe(YS_GAMEID);
  });

  it("accepts the appid itself in signed form", () => {
    withAppStore({ [YS_SIGNED]: { gameid: YS_GAMEID } });
    expect(getShortcutRunGameId(YS_SIGNED)).toBe(YS_GAMEID);
  });

  it("survives a missing appStore entirely", () => {
    withAppStore(null);
    expect(getShortcutRunGameId(YS_UNSIGNED)).toBe(YS_GAMEID);
  });
});
