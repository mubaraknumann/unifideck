/**
 * Xbox PC sign-in: the account Xodus installs and launches games with.
 *
 * It is a second sign-in on top of the Microsoft (xCloud) one, through our
 * own `xodus-cli login`. The backend owns the protocol and drives the page
 * over Edge's debugging port (`stores/microsoft/xodus/auth.py`). The
 * frontend only:
 *
 *   1. warns first that Microsoft may send an Authenticator approval
 *      (I-009), and that it must be the same account;
 *   2. calls `start_xbox_pc_sign_in`, which answers
 *      `{pending, launch_shortcut, reason}` (no `success` key). On
 *      `edge_not_installed` it offers `ChromiumInstallModal` and retries;
 *      any other reason is a toast;
 *   3. opens the Edge window through the Microsoft auth shortcut with the
 *      `xbox-pc-auth` action, and cancels the backend session if that
 *      launch fails;
 *   4. waits for the account state to leave `signing_in`, through the
 *      status event, with the window's exit as the backstop: once it
 *      closes, a short grace, then one refresh decides.
 *
 * On success it toasts and resyncs Microsoft (`prepareForSync` then
 * `requestAuthSync`), the same as `AuthDispatcher` does for a store sign-in.
 * Kept out of `AuthDispatcher` because that one keys everything by store,
 * and this is a second sign-in for a store that is already connected.
 */
import { showModal, ConfirmModal } from "@decky/ui";
import i18n from "i18next";
import { FC } from "react";
import { useTranslation } from "react-i18next";
import { rpcRoutes } from "../../api/rpc-routes";
import { ChromiumInstallModal } from "../../components/modals/ChromiumInstallModal";
import { watchAppStopped } from "../../lib/steam-bridge/shortcut-types";
import { signInOutcome } from "../../lib/xbox-pc-setup";
import {
  callXboxPc,
  xboxPcStore,
  type XboxPcCallResult,
} from "../../stores/xbox-pc-store";
import { launchXboxPcSignInViaShortcut } from "../../utils/authShortcutLaunch";
import {
  syncAfterXboxPc,
  toastXboxPcError,
  xboxPcToast,
} from "../store/xbox-pc-actions";
import type { XboxPcStatus } from "../../types/api";

/** How long after the window closes the backend gets to report. */
const CLOSED_GRACE_MS = 20_000;
/** The backend gives up at 600 s; this is the frontend's own ceiling. */
const SIGN_IN_CEILING_MS = 11 * 60 * 1000;

/** `start_xbox_pc_sign_in` answer. Never carries a `success` key. */
interface StartAnswer {
  pending?: boolean;
  launch_shortcut?: string | null;
  reason?: string | null;
  error?: string | null;
  /** The status once the sign-in started (it may already have ended). */
  status?: XboxPcStatus;
}

const SignInConfirm: FC<{ onConfirm: () => void; closeModal?: () => void }> = ({
  onConfirm,
  closeModal,
}) => {
  const { t } = useTranslation();
  return (
    <ConfirmModal
      strTitle={t("xboxPc.signIn.confirmTitle")}
      strOKButtonText={t("xboxPc.signIn.confirm")}
      strCancelButtonText={t("common.cancel")}
      onOK={() => {
        onConfirm();
        closeModal?.();
      }}
      onCancel={closeModal}
    >
      <p style={{ fontSize: 14, lineHeight: 1.5 }}>
        {t("xboxPc.signIn.confirmBody")}
      </p>
      <p style={{ fontSize: 14, lineHeight: 1.5 }}>
        {t("xboxPc.notice.authenticator")}
      </p>
    </ConfirmModal>
  );
};

function confirmSignIn(): Promise<boolean> {
  return new Promise((resolve) => {
    let ok = false;
    const handle = showModal(
      <SignInConfirm
        onConfirm={() => {
          ok = true;
          resolve(true);
        }}
        closeModal={() => {
          handle?.Close();
          if (!ok) resolve(false);
        }}
      />,
    );
  });
}

/** The refusal code in a start answer, or null when the sign-in began. */
function startRefusal(r: XboxPcCallResult<StartAnswer>): string | null {
  if (!r.ok) return r.error;
  if (r.data?.pending) return null;
  return r.data?.reason || r.data?.error || "unknown";
}

/**
 * Wait until the sign-in ends; resolves with the status that ended it.
 *
 * Settles on the first status that is not `signing_in` and is not the
 * `before` status the store held when the sign-in was started, or after the
 * window closed plus {@link CLOSED_GRACE_MS} and a refresh, or at the
 * ceiling. It does not wait to see `signing_in` first: the throttled event
 * can skip that state when the sign-in ends quickly.
 */
function waitForSignIn(
  appId: number | undefined,
  before: XboxPcStatus | null,
): Promise<XboxPcStatus | null> {
  return new Promise((resolve) => {
    const cleanup: (() => void)[] = [];
    let settled = false;
    const settle = (status: XboxPcStatus | null): void => {
      if (settled) return;
      settled = true;
      for (const fn of cleanup) fn();
      resolve(status);
    };
    const check = (): void => {
      const status = xboxPcStore.getSnapshot().status;
      if (status === before || status?.account.state === "signing_in") return;
      settle(status);
    };
    cleanup.push(xboxPcStore.subscribe(check));
    check();
    const ceiling = setTimeout(
      () => settle(xboxPcStore.getSnapshot().status),
      SIGN_IN_CEILING_MS,
    );
    cleanup.push(() => clearTimeout(ceiling));
    if (appId == null) return;
    cleanup.push(
      watchAppStopped(appId, () => {
        const grace = setTimeout(() => {
          void xboxPcStore
            .refresh()
            .then(() => settle(xboxPcStore.getSnapshot().status));
        }, CLOSED_GRACE_MS);
        cleanup.push(() => clearTimeout(grace));
      }),
    );
  });
}

/**
 * Run the whole Xbox PC sign-in. Resolves true once signed in.
 *
 * `confirm` is false for the automatic retry after Edge was installed:
 * the user already accepted the notice for this attempt.
 */
export async function signInXboxPc(confirm = true): Promise<boolean> {
  if (confirm && !(await confirmSignIn())) return false;
  const before = xboxPcStore.getSnapshot().status;
  const start = await callXboxPc<StartAnswer>(rpcRoutes.startXboxPcSignIn);
  if (start.ok && start.data?.status) xboxPcStore.apply(start.data.status);
  const refusal = startRefusal(start);
  if (refusal === "edge_not_installed") {
    showModal(
      <ChromiumInstallModal onInstalled={() => void signInXboxPc(false)} />,
    );
    return false;
  }
  if (refusal) {
    toastXboxPcError("toasts.xboxPcSignInFailed", refusal);
    return false;
  }
  let appId: number | undefined;
  if (start.ok && start.data?.launch_shortcut) {
    const launched = await launchXboxPcSignInViaShortcut();
    if (!launched.success) {
      await callXboxPc(rpcRoutes.cancelXboxPcSignIn);
      toastXboxPcError("toasts.xboxPcSignInFailed", "window_not_opened");
      return false;
    }
    appId = launched.app_id ?? launched.appId;
  }
  const outcome = signInOutcome(await waitForSignIn(appId, before));
  if (!outcome?.ok) {
    toastXboxPcError("toasts.xboxPcSignInFailed", outcome?.error);
    return false;
  }
  const gamertag = xboxPcStore.getSnapshot().status?.account.gamertag;
  xboxPcToast(
    i18n.t("toasts.xboxPcSignedIn"),
    gamertag
      ? i18n.t("toasts.xboxPcSignedInAs", { gamertag })
      : i18n.t("toasts.xboxPcSignedInBody"),
  );
  syncAfterXboxPc();
  return true;
}

/** Sign the Xbox PC account out. The Microsoft (xCloud) sign-in stays. */
export async function signOutXboxPc(): Promise<boolean> {
  const r = await xboxPcStore.signOut();
  if (!r.ok) {
    toastXboxPcError("toasts.xboxPcSignOutFailed", r.error);
    return false;
  }
  xboxPcToast(
    i18n.t("toasts.xboxPcSignedOut"),
    i18n.t("toasts.xboxPcSignedOutBody"),
  );
  syncAfterXboxPc();
  return true;
}
