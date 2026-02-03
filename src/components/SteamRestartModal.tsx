import { FC } from "react";
import { useTranslation } from "react-i18next";
import { ConfirmModal, DialogButton } from "@decky/ui";
import { FaSyncAlt, FaClock } from "react-icons/fa";

interface SteamRestartModalProps {
  store?: string;
  closeModal?: () => void;
}

export const SteamRestartModal: FC<SteamRestartModalProps> = ({
  store,
  closeModal,
}) => {
  const { t } = useTranslation();

  const handleRestartNow = () => {
    closeModal?.();

    // Use StartShutdown instead of StartRestart (safer and works better)
    // On Steam Deck, gamescope-session will automatically restart Steam
    // See: https://github.com/SteamDeckHomebrew/decky-loader/blob/main/frontend/src/steamfixes/README.md
    // StartRestart() breaks CEF debugging, StartShutdown(false) doesn't
    if (window.SteamClient?.User?.StartShutdown) {
      console.log("[SteamRestartModal] Restarting Steam via StartShutdown");
      window.SteamClient.User.StartShutdown(false);
    } else {
      console.error(
        "[SteamRestartModal] SteamClient.User.StartShutdown not available",
      );
    }
  };

  const handleLater = () => {
    closeModal?.();
  };

  return (
    <ConfirmModal
      strTitle={t("confirmModals.steamRestartTitle")}
      strDescription=""
      bHideCloseIcon={false}
      onOK={closeModal}
      onCancel={closeModal}
    >
      <div style={{ padding: "10px 0" }}>
        {/* Description */}
        <div
          style={{
            marginBottom: "20px",
            color: "#ccc",
            fontSize: "14px",
            lineHeight: "1.5",
          }}
        >
          {store
            ? t("confirmModals.steamRestartDescriptionStore", { store })
            : t("confirmModals.steamRestartDescription")}
        </div>

        {/* Action buttons */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "10px",
          }}
        >
          {/* Restart Now */}
          <DialogButton
            onClick={handleRestartNow}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: "8px",
              color: "#1a9fff",
            }}
          >
            <FaSyncAlt /> {t("confirmModals.restartNow")}
          </DialogButton>

          {/* Later */}
          <DialogButton
            onClick={handleLater}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: "8px",
            }}
          >
            <FaClock /> {t("confirmModals.later")}
          </DialogButton>
        </div>
      </div>
    </ConfirmModal>
  );
};

export default SteamRestartModal;
