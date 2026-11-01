/**
 * ChromiumInstallModal — Microsoft Edge prereq installer.
 *
 * Browser-based OAuth (Epic / GOG / Amazon / Microsoft) needs
 * the ``com.microsoft.Edge`` flatpak installed before the
 * launcher can open the auth URL. When `store_auth` returns
 * ``error: "edge_not_installed"`` the frontend spawns this
 * modal so the user can install Edge with a single click
 * instead of dropping into a terminal.
 *
 * The "Install" button calls the `install_edge` RPC
 * (proxied by `EdgeRPCMixin` to `MicrosoftStore.install_edge`,
 * which delegates to `EdgeInstaller`). The flatpak install
 * takes 30–90 s on a fresh prefix — the spinner stays up the
 * whole time. On success, the modal closes and invokes
 * `onInstalled()` so the caller can retry the original auth
 * flow automatically.
 *
 * Restored from the staging branch (`src893/staging`) where it
 * lived before the F1-F8 refactor erroneously dropped it.
 */
import { FC, useState } from "react";
import { ConfirmModal, Spinner } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { call } from "@decky/api";
import { rpcRoutes } from "../../api/rpc-routes";
import { unwrapRpcEnvelope } from "../../api/useRPC";
import { useToast } from "../../hooks/useToast";
import { toastText, headerText } from "../../services/toast-text";

interface Props {
  /** Optional callback after a successful install — typically
   *  the original auth flow that triggered the modal. */
  onInstalled?: () => void;
  closeModal?: () => void;
}

interface InstallEdgeResponse {
  installed: boolean;
  error?: string | null;
}

/** Three-state install UI : idle (Install / Cancel), in-flight
 *  (Spinner), and result (toast + close). Buttons are disabled
 *  while a call is in flight so the user can't double-click. */
export const ChromiumInstallModal: FC<Props> = ({
  onInstalled,
  closeModal,
}) => {
  const { t } = useTranslation();
  const toast = useToast();
  const [installing, setInstalling] = useState(false);
  const [installed, setInstalled] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleInstall = async (): Promise<void> => {
    setInstalling(true);
    setError(null);
    try {
      const raw = await call<[], unknown>(rpcRoutes.installEdge);
      const result = unwrapRpcEnvelope<InstallEdgeResponse>(raw, {
        route: rpcRoutes.installEdge,
        throwing: false,
      });
      if (result?.installed) {
        setInstalled(true);
        toast.success(
          headerText("toasts.header.edge"),
          toastText("toasts.edgeInstalled"),
        );
        setTimeout(() => {
          closeModal?.();
          onInstalled?.();
        }, 1500);
      } else {
        const msg = result?.error ?? t("microsoft.chromiumInstallFailed");
        setError(msg);
        toast.error(
          headerText("toasts.header.edge"),
          toastText("toasts.edgeInstallFailed"),
        );
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      setError(message);
      toast.error(
        headerText("toasts.header.edge"),
        toastText("toasts.edgeInstallFailed"),
      );
    } finally {
      setInstalling(false);
    }
  };

  return (
    <ConfirmModal
      strTitle={t("microsoft.chromiumRequired")}
      strOKButtonText={
        installed
          ? t("microsoft.chromiumInstalled")
          : installing
          ? t("microsoft.chromiumInstalling")
          : t("microsoft.chromiumInstallButton")
      }
      strCancelButtonText={t("common.cancel")}
      onOK={installed ? closeModal : handleInstall}
      onCancel={closeModal}
      bOKDisabled={installing}
      bHideCloseIcon={installing}
    >
      <div style={{ padding: "8px 0" }}>
        {!installing && !installed && !error && (
          <p style={{ fontSize: 14, lineHeight: 1.5 }}>
            {t("microsoft.chromiumRequiredMessage")}
          </p>
        )}
        {installing && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 12,
              padding: "16px 0",
            }}
          >
            <Spinner width={24} height={24} />
            <span style={{ fontSize: 14 }}>
              {t("microsoft.chromiumInstalling")}
            </span>
          </div>
        )}
        {installed && (
          <p style={{ fontSize: 14, color: "#4ade80" }}>
            {t("microsoft.chromiumInstalled")}
          </p>
        )}
        {error && <p style={{ fontSize: 14, color: "#ef4444" }}>{error}</p>}
      </div>
    </ConfirmModal>
  );
};
