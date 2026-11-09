/**
 * ClaimRequiredModal — "claim this game on the store's site first".
 *
 * Shown before installing a game the user paid for but has not claimed
 * yet (an itch.io bundle game). Buying a bundle on itch.io does not add its
 * games to the library; each one is claimed on the bundle's page. The
 * claim is the user's to make, so the install flow opens that page rather
 * than claiming through the API (`services/store/claim-flow.tsx`).
 *
 * Pure presentational: the caller opens the window and checks the claim.
 */
import { FC } from "react";
import { ConfirmModal } from "@decky/ui";
import { useTranslation } from "react-i18next";

interface Props {
  gameTitle: string;
  /** The bundle the game is in; empty when the backend could not tell. */
  bundleTitle: string;
  onConfirm: () => void;
  closeModal?: () => void;
}

export const ClaimRequiredModal: FC<Props> = ({
  gameTitle,
  bundleTitle,
  onConfirm,
  closeModal,
}) => {
  const { t } = useTranslation();
  return (
    <ConfirmModal
      strTitle={t("claimModal.title")}
      strDescription={
        bundleTitle
          ? t("claimModal.description", {
              title: gameTitle,
              bundle: bundleTitle,
            })
          : t("claimModal.descriptionNoBundle", { title: gameTitle })
      }
      strOKButtonText={t("claimModal.open")}
      strCancelButtonText={t("common.cancel")}
      onOK={() => {
        onConfirm();
        closeModal?.();
      }}
      onCancel={closeModal}
      bHideCloseIcon={false}
    />
  );
};
