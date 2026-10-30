/**
 * toast-text — the text of a toast, and the budget it is held to.
 *
 * A Decky toast shows its title and body on one line each, cut with an
 * ellipsis at 276px. Toast strings are held to a budget well inside that:
 * 16 characters per field, or for Chinese, Japanese and Korean 22 width
 * units (a CJK character is one unit, anything else 0.6). The budget is
 * enforced on the translations by `scripts/check_toast_length.py`, which
 * implements the same width model, and enforced here on the rendered text,
 * because interpolated values (a game title) are only known at runtime.
 *
 * Placeholders sit at the end of a toast string, so clamping cuts the
 * game title rather than the words around it.
 *
 * A toast's title is a header (`headerText`): the feature's name, shared by
 * every toast about it. Its body (`toastText`) says what happened.
 *
 * Every toast must go through `showToast` (`show-toast.ts`), which clamps
 * it; ESLint bans `toaster.toast` anywhere else. This module stays free of
 * `@decky/api`, so pure helpers can use it under test.
 */
import i18n from "i18next";

/** A toast string key. Toast text lives under `toasts.*` and nowhere else. */
export type ToastKey = `toasts.${string}`;

/**
 * A toast header key. The header names the feature (Capture Logs, Cloud
 * Saves, Battle.net, ...) and is shared by every toast about it; the body
 * says what happened.
 */
export type HeaderKey = `toasts.header.${string}`;

/**
 * Text that came from a toast key. A toast field accepts nothing else, so
 * a raw error message or a key from another namespace fails to compile.
 */
export type ToastString = string & { readonly __toastString: true };

/** Text that came from a header key: the only thing a toast title accepts. */
export type ToastHeader = string & { readonly __toastHeader: true };

/** Locales measured by width rather than by character count. */
const WIDE_LOCALES = new Set(["ja-JP", "ko-KR", "zh-CN", "zh-TW"]);

/** Characters per field in every other locale. */
export const TOAST_FIELD_CHARS = 16;
/** Width units per field in a wide locale. */
export const TOAST_FIELD_UNITS = 22;
/** Width of a narrow character in a wide locale. */
const NARROW_UNIT = 0.6;
const ELLIPSIS = "…";

// Kept identical to WIDE_RANGES in scripts/check_toast_length.py.
const WIDE_CHAR =
  /[\u1100-\u11FF\u3000-\u303F\u3040-\u309F\u30A0-\u30FF\u3130-\u318F\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF\uFF00-\uFF60\uFFE0-\uFFE6]/;

// The project's TS lib predates Intl.Segmenter; CEF has it.
type GraphemeSegmenter = new () => {
  segment(text: string): Iterable<{ segment: string }>;
};

function graphemes(text: string): string[] {
  const Segmenter = (Intl as { Segmenter?: GraphemeSegmenter }).Segmenter;
  if (!Segmenter) return Array.from(text);
  return Array.from(new Segmenter().segment(text), (s) => s.segment);
}

function isWideLocale(locale: string): boolean {
  return WIDE_LOCALES.has(locale);
}

function unitsOf(grapheme: string, wide: boolean): number {
  if (!wide) return 1;
  return WIDE_CHAR.test(grapheme) ? 1 : NARROW_UNIT;
}

/** Cut `text` to the field budget of `locale`, ending in an ellipsis. */
export function clampToastField(
  text: string,
  locale: string = i18n.language,
): string {
  const wide = isWideLocale(locale);
  const budget = wide ? TOAST_FIELD_UNITS : TOAST_FIELD_CHARS;
  const parts = graphemes(text);
  let used = 0;
  for (const part of parts) used += unitsOf(part, wide);
  if (used <= budget) return text;

  const room = budget - unitsOf(ELLIPSIS, wide);
  let kept = "";
  used = 0;
  for (const part of parts) {
    const next = used + unitsOf(part, wide);
    if (next > room) break;
    kept += part;
    used = next;
  }
  return kept.trimEnd() + ELLIPSIS;
}

/** Translate a toast key. */
export function toastText(
  key: ToastKey,
  params?: Record<string, string | number>,
): ToastString {
  return String(i18n.t(key, params)) as ToastString;
}

/** Translate a header key. */
export function headerText(
  key: HeaderKey,
  params?: Record<string, string | number>,
): ToastHeader {
  return String(i18n.t(key, params)) as ToastHeader;
}

/** A header key the backend sent; one outside `toasts.header.*` is logged. */
export function backendHeaderText(
  key: string,
  params?: Record<string, string>,
): ToastHeader {
  if (!key.startsWith("toasts.header.")) {
    console.error(`[Toast] backend sent a non-header title: ${key}`);
  }
  return String(i18n.t(key, params)) as ToastHeader;
}

/**
 * A key the backend sent. The Python side is held to `toasts.*` by
 * `check_toast_length.py`; a key outside it still renders, but is logged.
 */
export function backendToastText(
  key: string,
  params?: Record<string, string>,
): ToastString {
  if (!key.startsWith("toasts.")) {
    console.error(`[Toast] backend sent a non-toast key: ${key}`);
  }
  return String(i18n.t(key, params)) as ToastString;
}
