/**
 * Xbox PC actions the QAM toggle, the link step and the sign-in flow
 * share: turning the feature on and off, the post-setup resync, and the
 * failure toast.
 */
import { call, toaster } from "@decky/api";
import { showModal } from "@decky/ui";
import i18n from "i18next";
import { rpcRoutes } from "../../api/rpc-routes";
import { prepareForSync } from "../../lib/steam-bridge/prepare-sync";
import { xboxPcErrorText } from "../../lib/xbox-pc-errors";
import { xboxPcStore } from "../../stores/xbox-pc-store";
import { XboxPcNoticeModal } from "../../components/modals/XboxPcNoticeModal";
import { linkXboxPc } from "./xbox-pc-link";
import type { StoreId, XboxPcStatus } from "../../types/api";

/** A toast through Decky; logged where the toaster is missing. */
export function xboxPcToast(title: string, body: string): void {
  try {
    toaster.toast({ title, body, duration: 7000 });
  } catch (e) {
    console.warn("[XboxPc] toast failed:", e);
  }
}

/** Toast an Xbox PC failure code under `titleKey` (rule 8). */
export function toastXboxPcError(
  titleKey: string,
  code: string | undefined,
): void {
  const status = xboxPcStore.getSnapshot().status;
  xboxPcToast(
    i18n.t(titleKey),
    xboxPcErrorText(code, i18n.t.bind(i18n), status?.runtime.source_dir),
  );
}

/**
 * Resync Microsoft so the PC rows appear (or go).
 *
 * `prepareForSync` first: a bare `requestAuthSync` skips the active-user
 * upload and the restart-modal arming, and PC-only rows create shortcuts
 * that need a Steam restart. Fire-and-forget, like `AuthDispatcher`.
 */
export function syncAfterXboxPc(): void {
  void prepareForSync()
    .then(() =>
      call<[StoreId], unknown>(rpcRoutes.requestAuthSync, "microsoft"),
    )
    .catch((e) => console.error("[XboxPc] requestAuthSync failed:", e));
}

/** Show the unofficial notice; resolves true when the user accepts. */
function confirmNotice(): Promise<boolean> {
  return new Promise((resolve) => {
    let accepted = false;
    const handle = showModal(
      <XboxPcNoticeModal
        onConfirm={() => {
          accepted = true;
          resolve(true);
        }}
        closeModal={() => {
          handle?.Close();
          if (!accepted) resolve(false);
        }}
      />,
    );
  });
}

/**
 * Turn the feature on, through the notice when it was not acknowledged.
 *
 * Resolves true once the backend has it on. With Microsoft connected it
 * then installs the components and signs in by itself (`linkXboxPc`); a
 * signed-in account resyncs at once.
 */
export async function enableXboxPc(status: XboxPcStatus): Promise<boolean> {
  const needsNotice = status.notice_ack_version < status.notice_version;
  if (needsNotice && !(await confirmNotice())) return false;
  const r = await xboxPcStore.setEnabled(true, status.notice_version);
  if (!r.ok) {
    toastXboxPcError("toasts.xboxPcEnableFailed", r.error);
    return false;
  }
  if (!r.data?.xcloud_signed_in) {
    // Nothing to link yet: connecting Microsoft runs `linkXboxPc`.
    xboxPcToast(
      i18n.t("toasts.xboxPcEnabled"),
      i18n.t("xboxPc.section.microsoftNeeded"),
    );
  } else if (r.data.account?.state === "signed_in") {
    syncAfterXboxPc();
  } else {
    void linkXboxPc();
  }
  return true;
}

/**
 * Turn the feature off. Nothing is deleted: installed games stay listed,
 * playable once it is back on, and uninstallable either way. The resync
 * drops the PC-only rows that are not installed.
 */
export async function disableXboxPc(): Promise<boolean> {
  const r = await xboxPcStore.setEnabled(false);
  if (!r.ok) {
    toastXboxPcError("toasts.xboxPcEnableFailed", r.error);
    return false;
  }
  syncAfterXboxPc();
  return true;
}
