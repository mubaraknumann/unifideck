/**
 * XboxPcNoticeModal: what the user agrees to before turning on Xbox PC
 * games.
 *
 * Shown by `enableXboxPc` when the acknowledged notice version is older
 * than the one the backend requires (`notice_ack_version < notice_version`).
 * It says four things:
 *   - the feature is unofficial and experimental (our own Xodus build);
 *   - Microsoft may ask for an Authenticator approval at sign-in (I-009);
 *   - the Xbox PC sign-in must be the same account as the Microsoft one;
 *   - the components take disk space.
 *
 * Confirm sends the version back with `set_xbox_pc_enabled`, which is what
 * records the acknowledgement.
 */
import { FC } from "react";
import { ConfirmModal } from "@decky/ui";
import { useTranslation } from "react-i18next";

interface Props {
  onConfirm: () => void;
  closeModal?: () => void;
}

const paragraph = { fontSize: 14, lineHeight: 1.5, margin: "0 0 10px" };

export const XboxPcNoticeModal: FC<Props> = ({ onConfirm, closeModal }) => {
  const { t } = useTranslation();
  return (
    <ConfirmModal
      strTitle={t("xboxPc.notice.title")}
      strOKButtonText={t("xboxPc.notice.confirm")}
      strCancelButtonText={t("common.cancel")}
      onOK={() => {
        onConfirm();
        closeModal?.();
      }}
      onCancel={closeModal}
    >
      <div style={{ padding: "4px 0" }}>
        <p style={paragraph}>{t("xboxPc.notice.unofficial")}</p>
        <p style={paragraph}>{t("xboxPc.notice.authenticator")}</p>
        <p style={paragraph}>{t("xboxPc.notice.sameAccount")}</p>
        <p style={{ ...paragraph, margin: 0 }}>{t("xboxPc.notice.disk")}</p>
      </div>
    </ConfirmModal>
  );
};
