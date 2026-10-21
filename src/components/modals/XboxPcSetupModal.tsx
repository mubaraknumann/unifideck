/**
 * XboxPcSetupModal: the four steps before an Xbox PC title can install.
 *
 *   1. Turn on Xbox PC games (through the unofficial notice).
 *   2. Microsoft account connected (the xCloud sign-in the library uses).
 *   3. Components installed (our Xodus build and runtime).
 *   4. Xbox PC sign-in.
 *
 * Opened by the install flow (`runXboxPcSetup`) before the storage picker,
 * or after `install_game` refused with a setup code. Each step reads the
 * live status, so the modal advances by itself as events arrive. Continue
 * unlocks once `isSetupReady` holds; the flow then retries the install.
 */
import { FC, ReactNode, useEffect, useState } from "react";
import { ButtonItem, ConfirmModal, Field, PanelSectionRow } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaCheckCircle, FaRegCircle } from "react-icons/fa";
import { useXboxPcStatus } from "../../hooks/useXboxPcStatus";
import { isSetupReady, runtimeUsable } from "../../lib/xbox-pc-setup";
import { xboxPcErrorText } from "../../lib/xbox-pc-errors";
import { xboxPcStore } from "../../stores/xbox-pc-store";
import { AuthDispatcher } from "../../services/auth/AuthDispatcher";
import {
  enableXboxPc,
  toastXboxPcError,
} from "../../services/store/xbox-pc-actions";
import { XboxPcRuntimeRow } from "../settings/XboxPcRuntimeRow";
import { XboxPcAccountRow } from "../settings/XboxPcAccountRow";
import type { XboxPcStatus } from "../../types/api";

interface Props {
  gameTitle?: string;
  onReady: () => void;
  closeModal?: () => void;
}

/** One step's heading, with a tick once it is done. */
const Step: FC<{ done: boolean; label: string; children?: ReactNode }> = ({
  done,
  label,
  children,
}) => (
  <>
    <PanelSectionRow>
      <Field
        icon={done ? <FaCheckCircle color="#4caf50" /> : <FaRegCircle />}
        label={label}
        bottomSeparator="none"
      />
    </PanelSectionRow>
    {!done && children}
  </>
);

/** Steps 1 and 2: turning it on, and the Microsoft sign-in. */
const FirstSteps: FC<{ status: XboxPcStatus }> = ({ status }) => {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<unknown>): Promise<void> => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };
  const connect = async (): Promise<void> => {
    try {
      const r = await AuthDispatcher.start("microsoft");
      if (!r.success) toastXboxPcError("toasts.authFailed", r.error);
    } catch (e) {
      toastXboxPcError("toasts.authFailed", String(e));
    }
    await xboxPcStore.refetch();
  };
  return (
    <>
      <Step done={status.enabled} label={t("xboxPc.setup.stepEnable")}>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={() => void run(() => enableXboxPc(status))}
          >
            {t("xboxPc.setup.enable")}
          </ButtonItem>
        </PanelSectionRow>
      </Step>
      <Step
        done={status.xcloud_signed_in}
        label={t("xboxPc.setup.stepMicrosoft")}
      >
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={() => void run(connect)}
          >
            {t("xboxPc.setup.connect")}
          </ButtonItem>
        </PanelSectionRow>
      </Step>
    </>
  );
};

export const XboxPcSetupModal: FC<Props> = ({
  gameTitle,
  onReady,
  closeModal,
}) => {
  const { t } = useTranslation();
  const { status, error } = useXboxPcStatus();
  const ready = isSetupReady(status);

  useEffect(() => {
    void xboxPcStore.refetch();
  }, []);

  const account = status?.account.state;
  const accountDone = account === "signed_in" || account === "unverified";
  return (
    <ConfirmModal
      strTitle={t("xboxPc.setup.title")}
      strOKButtonText={t("xboxPc.setup.continue")}
      strCancelButtonText={t("common.cancel")}
      bOKDisabled={!ready}
      onOK={() => {
        onReady();
        closeModal?.();
      }}
      onCancel={closeModal}
    >
      <p style={{ fontSize: 14, lineHeight: 1.5, margin: "0 0 8px" }}>
        {gameTitle
          ? t("xboxPc.setup.introGame", { title: gameTitle })
          : t("xboxPc.setup.intro")}
      </p>
      {!status && (
        <p style={{ fontSize: 13, color: "#ff6b6b" }}>
          {error ? xboxPcErrorText(error, t) : t("common.loading")}
        </p>
      )}
      {status && (
        <>
          <FirstSteps status={status} />
          <Step
            done={runtimeUsable(status.runtime.state)}
            label={t("xboxPc.setup.stepRuntime")}
          >
            <XboxPcRuntimeRow status={status} />
          </Step>
          <Step done={accountDone} label={t("xboxPc.setup.stepSignIn")}>
            <XboxPcAccountRow status={status} />
          </Step>
        </>
      )}
    </ConfirmModal>
  );
};
