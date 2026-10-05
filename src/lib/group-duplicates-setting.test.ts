// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  isGroupDuplicatesEnabled,
  resetGroupDuplicatesCache,
  setGroupDuplicatesEnabled,
} from "./group-duplicates-setting";

const KEY = "unifideck:group-duplicates.enabled";

beforeEach(() => {
  window.localStorage.removeItem(KEY);
  resetGroupDuplicatesCache();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("group-duplicates-setting", () => {
  it("reads storage once, not on every filter pass", () => {
    window.localStorage.setItem(KEY, "1");
    const getItem = vi.spyOn(Storage.prototype, "getItem");
    for (let i = 0; i < 1000; i++) expect(isGroupDuplicatesEnabled()).toBe(true);
    expect(getItem).toHaveBeenCalledTimes(1);
  });

  it("a change is visible at once and persisted", () => {
    expect(isGroupDuplicatesEnabled()).toBe(false);
    setGroupDuplicatesEnabled(true);
    expect(isGroupDuplicatesEnabled()).toBe(true);
    expect(window.localStorage.getItem(KEY)).toBe("1");
    resetGroupDuplicatesCache();
    expect(isGroupDuplicatesEnabled()).toBe(true);
  });

  it("unreadable storage means off", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(isGroupDuplicatesEnabled()).toBe(false);
  });
});
