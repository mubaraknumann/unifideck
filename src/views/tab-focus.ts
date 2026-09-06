/**
 * tab-focus — the rule deciding whether a focus signal on a QAM tab
 * pill is the *user* moving onto that tab, or Steam moving focus for
 * its own reasons.
 *
 * The QAM tabs switch on focus rather than on an A press (see
 * QuickAccessPanel). That only works if we can tell the two apart,
 * because Steam fires focus at whichever nav node it picks whenever the
 * panel mounts — and the panel remounts on every Quick-Access open and,
 * in Gaming Mode, every time a modal tears down the QAM popup window.
 * Left unfiltered, that pass drags the panel back to Settings.
 *
 * The previous rule was a one-shot latch: swallow the FIRST focus event
 * of a mount if it named a tab we were not on. That was consumed by
 * whichever event happened to arrive first, which is a race between
 * Steam's mount pass and our own imperative focus grab. When both of
 * those missed — `HTMLElement.focus()` is a no-op on an element still
 * hidden behind the QAM open animation — the latch was still armed for
 * the user's first stick move, and ate exactly that move.
 *
 * So the rule is expressed as two bounded, independently-named guards
 * instead, kept here as a pure function so it can be unit-tested (React
 * is stubbed out in this repo's vitest setup, so the component itself
 * cannot be rendered under test).
 */

/** The two Quick-Access tabs. */
export type ActiveTab = "settings" | "downloads";

/**
 * How long after mount a focus signal may only *confirm* the tab we are
 * already showing, never change it.
 *
 * Covers Steam's automatic focus pass, which lands within a frame or
 * two of mount, plus our own retrying grab. Short enough that a user
 * cannot realistically have moved the stick within it — the panel is
 * still animating in.
 */
export const TAB_SETTLE_MS = 400;

export interface TabFocusSignal {
  /** Tab whose pill received the focus signal. */
  next: ActiveTab;
  /** Tab currently on screen. */
  current: ActiveTab;
  /** True while we are calling `.focus()` ourselves. */
  programmatic: boolean;
  /** `performance.now()` at signal time. */
  now: number;
  /** End of the post-mount settle window (`performance.now()` scale). */
  settleUntil: number;
}

/**
 * Whether a focus signal on a tab pill should switch the displayed tab.
 *
 * Rejects three things:
 *
 *  - our own imperative grab (`programmatic`) — `focusin` dispatches
 *    synchronously from `.focus()`, so a flag set around the call is
 *    exact rather than a guess;
 *  - a signal naming the tab already on screen — nothing to switch, and
 *    saying so here keeps the caller free of a redundant `setState`;
 *  - a tab *change* inside the settle window — that is Steam's mount
 *    pass, not the user.
 *
 * Everything else is the user, and switches the tab.
 */
export function shouldSwitchTab(signal: TabFocusSignal): boolean {
  if (signal.programmatic) return false;
  if (signal.next === signal.current) return false;
  return signal.now >= signal.settleUntil;
}
