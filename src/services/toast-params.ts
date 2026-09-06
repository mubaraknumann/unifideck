/**
 * Build the i18n interpolation params for a backend-emitted toast.
 *
 * Shared by the two renderers — `boot-event-listener` (plugin bus) and
 * `launcherToasts` (launcher bridge file) — which run in different processes
 * and had drifted into two copies of this logic.
 *
 * Two resolutions happen here, both of which produce a visibly broken string
 * when they are missing:
 *
 * 1. **`gameTitle`** — a top-level payload field, but the strings interpolate
 *    it as `{{gameTitle}}`. Without merging it, every launcher toast rendered
 *    with the placeholder unfilled ("Starting  through Battle.net…").
 *
 * 2. **`error`** — the cloud-sync failure strings interpolate `{{error}}`, but
 *    the backend deliberately sends a machine **code** plus the i18n key that
 *    translates it (`error_i18n_key: "cloudSync.error.disk_full"`), because a
 *    backend that sent English would be untranslatable. Nothing resolved that
 *    key, so `{{error}}` would have rendered empty: *"Cloud save upload failed
 *    for gog (). Your progress is stored locally only."*
 *
 * That second one is why this file exists rather than an inline spread. The
 * whole cloud-failure module sat unimported (audit register item 37), so its
 * payload contract had never been rendered by anything — wiring it up without
 * resolving the key would have shipped the placeholder bug on the first real
 * failure. Audit §2.9's lesson: wiring dead code is a behaviour change and
 * needs the same scrutiny as a fix.
 */
import i18n from "i18next";

interface ToastLikePayload {
  game_title?: unknown;
  i18n_params?: unknown;
}

export function buildToastParams(
  payload: ToastLikePayload,
): Record<string, string> {
  const raw = (payload.i18n_params ?? {}) as Record<string, unknown>;
  const params: Record<string, string> = {
    ...(payload.game_title ? { gameTitle: String(payload.game_title) } : {}),
    ...(raw as Record<string, string>),
  };

  // Resolve the machine error code into the human string the message
  // interpolates. Falls back to the raw code rather than leaving the
  // placeholder empty — an untranslated code still tells the user (and a
  // bug report) more than a blank pair of brackets.
  const errorKey = raw.error_i18n_key;
  if (typeof errorKey === "string" && errorKey && !params.error) {
    const translated = String(i18n.t(errorKey));
    params.error =
      translated === errorKey ? String(raw.error_code ?? errorKey) : translated;
  }

  return params;
}
