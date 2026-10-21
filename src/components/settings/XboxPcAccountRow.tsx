/**
 * XboxPcAccountRow: the Xbox PC sign-in (the account Xodus uses).
 *
 * Needs the components first: the sign-in runs our own `xodus-cli`. A
 * state of `unknown` (nothing cached since the plugin started) triggers
 * one `refresh_xbox_pc_status` on mount, so a user who is in fact signed
 * in is not sent through another sign-in and another Authenticator push.
 *
 * Used in the QAM section and in the setup modal.
 */
import { FC, useEffect, useState } from "react";
import { ButtonItem, Field, PanelSectionRow } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { rpcRoutes } from "../../api/rpc-routes";
import { xboxPcErrorText } from "../../lib/xbox-pc-errors";
import { runtimeUsable } from "../../lib/xbox-pc-setup";
import { callXboxPc, xboxPcStore } from "../../stores/xbox-pc-store";
import {
  signInXboxPc,
  signOutXboxPc,
} from "../../services/auth/xbox-pc-sign-in";
import type { TFunction } from "i18next";
import type { XboxPcStatus } from "../../types/api";

type Action = "sign_in" | "sign_out" | "cancel";

const ACTION_FOR: Readonly<Record<string, Action | undefined>> = {
  signed_out: "sign_in",
  error: "sign_in",
  mismatch: "sign_in",
  signing_in: "cancel",
  signed_in: "sign_out",
  unverified: "sign_out",
};

const ACTION_KEYS: Readonly<Record<Action, string>> = {
  sign_in: "xboxPc.account.signIn",
  sign_out: "xboxPc.account.signOut",
  cancel: "xboxPc.account.cancel",
};

/** The one-line account description. */
function describe(status: XboxPcStatus, t: TFunction): string {
  const a = status.account;
  switch (a.state) {
    case "signed_in":
      return a.gamertag
        ? t("xboxPc.account.signedInAs", { gamertag: a.gamertag })
        : t("xboxPc.account.signedIn");
    case "signing_in":
      return t("xboxPc.account.signingIn");
    case "unknown":
      return t("xboxPc.account.unknown");
    case "unverified":
      return t("xboxPc.account.unverified");
    case "mismatch":
      return t("xboxPc.account.errors.mismatch");
    case "error":
      return xboxPcErrorText(a.error, t, status.runtime.source_dir);
    default:
      return t("xboxPc.account.signedOut");
  }
}

async function runAction(action: Action): Promise<void> {
  if (action === "sign_in") await signInXboxPc();
  else if (action === "sign_out") await signOutXboxPc();
  else await callXboxPc(rpcRoutes.cancelXboxPcSignIn);
}

export const XboxPcAccountRow: FC<{ status: XboxPcStatus }> = ({ status }) => {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const runtimeReady = runtimeUsable(status.runtime.state);
  const unknown = status.account.state === "unknown";

  useEffect(() => {
    if (runtimeReady && unknown) void xboxPcStore.refresh();
  }, [runtimeReady, unknown]);

  const action = runtimeReady ? ACTION_FOR[status.account.state] : undefined;
  const onAction = async (): Promise<void> => {
    if (!action) return;
    setBusy(true);
    try {
      await runAction(action);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PanelSectionRow>
        <Field
          label={t("xboxPc.account.label")}
          description={
            runtimeReady
              ? describe(status, t)
              : t("xboxPc.account.needsRuntime")
          }
          bottomSeparator="none"
        />
      </PanelSectionRow>
      {action && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy && action !== "cancel"}
            onClick={() => void onAction()}
          >
            {t(ACTION_KEYS[action])}
          </ButtonItem>
        </PanelSectionRow>
      )}
    </>
  );
};
