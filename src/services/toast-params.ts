/**
 * Build the i18n interpolation params for a backend-emitted toast.
 *
 * Shared by the backend toast renderer (`stage-toast`), which both
 * `boot-event-listener` (plugin bus) and `launcherToasts` (launcher bridge
 * file) call.
 *
 * **`gameTitle`** is a top-level payload field, but the strings interpolate
 * it as `{{gameTitle}}`. Without merging it, every launcher toast rendered
 * with the placeholder unfilled ("Starting  through Battle.net…"). An
 * explicit `i18n_params` entry still wins, since a caller that named the
 * variable meant it.
 *
 * A failure's reason is not a param: the backend sends it as the toast body
 * key (`toasts.cloudError.diskFull`), because a toast has no room for an
 * error string.
 */

interface ToastLikePayload {
  game_title?: unknown;
  i18n_params?: unknown;
}

export function buildToastParams(
  payload: ToastLikePayload,
): Record<string, string> {
  const raw = (payload.i18n_params ?? {}) as Record<string, string>;
  return {
    ...(payload.game_title ? { gameTitle: String(payload.game_title) } : {}),
    ...raw,
  };
}
