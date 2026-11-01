/**
 * stage-toast — render a backend `LAUNCHER_STAGE` toast payload.
 *
 * Shared by the two renderers, `boot-event-listener` (plugin bus) and
 * `launcherToasts` (launcher bridge file). They used to build the toast
 * separately and disagreed when there was no title key: one put the action
 * label in both the body and the subtext.
 *
 * `i18n_title_key` is the header (a `toasts.header.*` key naming the
 * feature) and `i18n_key` the body. The action's label, when the toast is
 * clickable, is the subtext.
 */
import type { ToastActionPayload } from "../types/events";
import { resolveToastAction } from "./toast-action";
import { resolveToastDuration } from "./toast-duration";
import { buildToastParams } from "./toast-params";
import {
  backendToastText,
  backendHeaderText,
  type ToastString,
} from "./toast-text";
import { showToast } from "./show-toast";

export function showStageToast(payload: ToastActionPayload): void {
  const params = buildToastParams(payload);
  const body = payload.i18n_key
    ? backendToastText(payload.i18n_key, params)
    : ("" as ToastString);
  if (!payload.i18n_title_key) {
    // Every backend toast names its feature (`check_toast_length.py`); one
    // that does not still shows, under the generic header.
    console.error(`[Toast] no header for ${payload.i18n_key ?? "(no key)"}`);
  }
  const title = backendHeaderText(
    payload.i18n_title_key ?? "toasts.header.unifideck",
    params,
  );

  const onClick = resolveToastAction(payload.action);
  const labelKey = payload.action?.i18n_label_key;
  showToast({
    title,
    body,
    ...(onClick && labelKey ? { subtext: backendToastText(labelKey) } : {}),
    duration: resolveToastDuration(payload.duration_ms, payload.severity),
    ...(onClick ? { onClick } : {}),
  });
}
