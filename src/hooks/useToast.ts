/**
 * useToast — typed wrapper around {@link showToast}.
 *
 * Three semantics-bearing methods (`info`, `success`, `error`) and a base
 * `show` for full control. The title is a `ToastHeader` (`headerText`), the
 * feature's shared name; the body a `ToastString` (`toastText`), what
 * happened. `showToast` clamps both to the toast budget.
 */
import { useCallback } from "react";
import { DEFAULT_DURATION, ERROR_DURATION } from "../services/toast-duration";
import { type ToastHeader, type ToastString } from "../services/toast-text";
import { showToast } from "../services/show-toast";

interface ToastOptions {
  duration?: number;
  body?: ToastString;
  onClick?: () => void;
}

/**
 * Shape returned by {@link useToast}. The single field is
 * a stable `showToast(args)` callback — no other state.
 */
export interface UseToastResult {
  show: (
    title: ToastHeader,
    body?: ToastString,
    options?: ToastOptions,
  ) => void;
  info: (title: ToastHeader, body?: ToastString) => void;
  success: (title: ToastHeader, body?: ToastString) => void;
  error: (title: ToastHeader, body?: ToastString) => void;
}

/**
 * Hook returning a memoised `showToast` callback that
 * forwards to Decky Loader's toast API. Centralised
 * here so styling / duration defaults stay consistent
 * across the codebase and so test code can mock a
 * single boundary.
 */
export function useToast(): UseToastResult {
  const show = useCallback(
    (title: ToastHeader, body?: ToastString, options: ToastOptions = {}) => {
      showToast({
        title,
        body: body ?? options.body,
        duration: options.duration ?? DEFAULT_DURATION,
        ...(options.onClick ? { onClick: options.onClick } : {}),
      });
    },
    [],
  );

  const info = useCallback(
    (title: ToastHeader, body?: ToastString) => show(title, body),
    [show],
  );

  const success = useCallback(
    (title: ToastHeader, body?: ToastString) => show(title, body),
    [show],
  );

  const error = useCallback(
    (title: ToastHeader, body?: ToastString) =>
      show(title, body, { duration: ERROR_DURATION }),
    [show],
  );

  return { show, info, success, error };
}
