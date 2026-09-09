/**
 * Tests for the QAM tab-pill focus rule.
 *
 * The behaviour under test is the one that was intermittently broken:
 * a focus signal must switch the tab when it is the user's, and must not
 * when it is Steam's own mount pass or our own imperative focus grab.
 * The old rule was a one-shot latch, so the "user's first move gets
 * swallowed" case below is the regression this locks down.
 */
import { describe, it, expect } from "vitest";
import { shouldSwitchTab, TAB_SETTLE_MS, type ActiveTab } from "./tab-focus";

/** Signal defaults: a settled panel, showing Settings, user-driven. */
const signal = (over: Partial<Parameters<typeof shouldSwitchTab>[0]> = {}) => ({
  next: "downloads" as ActiveTab,
  current: "settings" as ActiveTab,
  programmatic: false,
  now: 10_000,
  settleUntil: 1_000,
  ...over,
});

describe("shouldSwitchTab", () => {
  it("switches when the user moves onto the other tab after settling", () => {
    expect(shouldSwitchTab(signal())).toBe(true);
  });

  it("ignores our own imperative focus grab", () => {
    expect(shouldSwitchTab(signal({ programmatic: true }))).toBe(false);
  });

  it("ignores a signal naming the tab already on screen", () => {
    expect(shouldSwitchTab(signal({ next: "settings" }))).toBe(false);
  });

  it("ignores Steam's mount pass aimed at the other tab", () => {
    // Steam's automatic pass lands within a frame or two of mount, i.e.
    // well inside the settle window.
    const now = 1_000;
    expect(
      shouldSwitchTab(signal({ now, settleUntil: now + TAB_SETTLE_MS, next: "downloads" })),
    ).toBe(false);
  });

  it("honours a user move the instant the settle window closes", () => {
    const settleUntil = 5_000;
    expect(shouldSwitchTab(signal({ now: settleUntil, settleUntil }))).toBe(true);
    expect(shouldSwitchTab(signal({ now: settleUntil - 1, settleUntil }))).toBe(false);
  });

  it("does not consume the guard — the window is time-bounded, not one-shot", () => {
    // The regression: a one-shot latch was spent by whichever focus event
    // arrived first, so when Steam's pass and our grab both missed, the
    // user's FIRST stick move was the one it swallowed. A time window
    // cannot be spent, so repeated post-settle moves all switch.
    const settled = signal({ now: 9_000, settleUntil: 400 });
    expect(shouldSwitchTab(settled)).toBe(true);
    expect(shouldSwitchTab(settled)).toBe(true);
    expect(shouldSwitchTab({ ...settled, next: "settings", current: "downloads" })).toBe(true);
  });

  it("blocks a programmatic signal even after the window has closed", () => {
    expect(shouldSwitchTab(signal({ programmatic: true, settleUntil: 0 }))).toBe(false);
  });
});
