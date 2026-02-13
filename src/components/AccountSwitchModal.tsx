import { FC } from "react";
import { useTranslation } from "react-i18next";
import { ConfirmModal, DialogButton } from "@decky/ui";
import { FaExchangeAlt, FaTrashAlt, FaForward } from "react-icons/fa";

interface AccountSwitchModalProps {
  hasRegistry: boolean;
  hasAuthTokens: boolean;
  onMigrate: () => void;
  onClearAuths: () => void;
  onSkip: () => void;
  closeModal?: () => void;
}

export const AccountSwitchModal: FC<AccountSwitchModalProps> = ({
  hasRegistry,
  hasAuthTokens,
  onMigrate,
  onClearAuths,
  onSkip,
  closeModal,
}) => {
  const { t } = useTranslation();

  return (
    <>
      {/* Hide the default Confirm/Cancel button row */}
      <style>{`
        .account-switch-modal + div { display: none !important; }
        .DialogFooter { display: none !important; }
      `}</style>
      <ConfirmModal
        strTitle={t("accountSwitch.title")}
        strDescription=""
        bHideCloseIcon={false}
        onOK={closeModal}
        onCancel={closeModal}
      >
        <div className="account-switch-modal" style={{ padding: "10px 0" }}>
          {/* Description */}
          <div style={{ marginBottom: "20px", color: "#ccc", fontSize: "14px", lineHeight: "1.5" }}>
            {t("accountSwitch.description")}
          </div>

          {/* Action buttons */}
          <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
            {/* Migrate — only shown if registry has entries */}
            {hasRegistry && (
              <DialogButton
                onClick={() => { closeModal?.(); onMigrate(); }}
                style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }}
              >
                <FaExchangeAlt /> {t("accountSwitch.migrate")}
              </DialogButton>
            )}

            {/* Fresh Start — only shown if auth tokens exist */}
            {hasAuthTokens && (
              <DialogButton
                onClick={() => { closeModal?.(); onClearAuths(); }}
                style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px", color: "#ef4444" }}
              >
                <FaTrashAlt /> {t("accountSwitch.freshStart")}
              </DialogButton>
            )}

            {/* Skip */}
            <DialogButton
              onClick={() => { closeModal?.(); onSkip(); }}
              style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px", opacity: 0.7 }}
            >
              <FaForward /> {t("accountSwitch.skip")}
            </DialogButton>
          </div>
        </div>
      </ConfirmModal>
    </>
  );
};

export default AccountSwitchModal;
