import { FC } from "react";
import StoreConnections from "./StoreConnections";
import LibrarySync from "./LibrarySync";
import { LanguageSelector } from "../LanguageSelector";
import { CleanupSection } from "./CleanupSection";
import type { SyncProgress } from "../../types";

interface SettingsTabProps {
  storeStatus: Record<string, string>;
  onStartAuth: (store: string) => void;
  onLogout: (store: string) => Promise<void>;
  syncing: boolean;
  syncCooldown: boolean;
  cooldownSeconds: number;
  syncProgress: SyncProgress | null;
  handleManualSync: () => void;
  handleCancelSync: () => void;
  showModal: any;
  checkStoreStatus: () => void;
}

export const SettingsTab: FC<SettingsTabProps> = ({
  storeStatus,
  onStartAuth,
  onLogout,
  syncing,
  syncCooldown,
  cooldownSeconds,
  syncProgress,
  handleManualSync,
  handleCancelSync,
  showModal,
  checkStoreStatus,
}) => {
  return (
    <>
      <StoreConnections
        storeStatus={storeStatus}
        onStartAuth={onStartAuth}
        onLogout={onLogout}
      />

      <LibrarySync
        syncing={syncing}
        syncCooldown={syncCooldown}
        cooldownSeconds={cooldownSeconds}
        syncProgress={syncProgress}
        storeStatus={storeStatus}
        handleManualSync={handleManualSync}
        handleCancelSync={handleCancelSync}
        showModal={showModal}
        checkStoreStatus={checkStoreStatus}
      />

      <LanguageSelector />

      <CleanupSection syncing={syncing} syncCooldown={syncCooldown} />
    </>
  );
};
