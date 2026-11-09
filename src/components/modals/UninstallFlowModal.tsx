/**
 * UninstallFlowModal: the uninstall confirmation, openable from anywhere.
 *
 * Steam's gear menu renders outside our React tree, so a menu item cannot
 * reach the hooks the play section uses. This modal brings its own
 * `DownloadProvider` and runs the same confirm logic
 * (`useUninstallConfirm`), so the menu's Uninstall behaves exactly like the
 * play section's.
 */
import { FC } from "react";
import { DownloadProvider } from "../../contexts/DownloadContext";
import { useGameInfo } from "../../hooks/useGameInfo";
import { useUninstallConfirm } from "../../hooks/usePlayActions";
import { UninstallConfirmModal } from "./UninstallConfirmModal";

interface Props {
  appId: number;
  /** Injected by `showModal`. */
  closeModal?: () => void;
}

const UninstallFlow: FC<Required<Props>> = ({ appId, closeModal }) => {
  const { data: game } = useGameInfo(appId);
  const onConfirm = useUninstallConfirm(appId);
  return (
    <UninstallConfirmModal
      gameId={appId}
      gameTitle={game?.title ?? ""}
      onConfirm={onConfirm}
      closeModal={closeModal}
    />
  );
};

export const UninstallFlowModal: FC<Props> = ({ appId, closeModal }) => (
  <DownloadProvider>
    <UninstallFlow appId={appId} closeModal={closeModal ?? (() => {})} />
  </DownloadProvider>
);
