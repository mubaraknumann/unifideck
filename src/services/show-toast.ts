/**
 * show-toast — the one path every toast takes to the screen.
 *
 * Every field is clamped to the toast budget (`toast-text.ts`). ESLint bans
 * `toaster.toast` anywhere but here.
 */
import { toaster } from "@decky/api";
import {
  clampToastField,
  type ToastString,
  type ToastHeader,
} from "./toast-text";

export interface ToastOptions {
  title: ToastHeader;
  body?: ToastString;
  /** The action's label, shown under the body when the toast is clickable. */
  subtext?: ToastString;
  duration: number;
  onClick?: () => void;
}

/** Show a toast, every field clamped to the toast budget. */
export function showToast(options: ToastOptions): void {
  const title = clampToastField(options.title);
  const body = options.body ? clampToastField(options.body) : "";
  const subtext = options.subtext ? clampToastField(options.subtext) : "";
  try {
    toaster.toast({
      title,
      body,
      duration: options.duration,
      ...(options.onClick ? { onClick: options.onClick } : {}),
      ...(subtext ? { subtext } : {}),
    });
  } catch {
    console.log(`[Toast] ${title}: ${body}`);
  }
}
