import { PanelSection, PanelSectionRow, Field } from "@decky/ui";
import { loadTranslations, t } from "../../i18n";
import StoreIcon from "../StoreIcon";
import { Store } from "../../types/store";
import StoreAuthButton from "./StoreAuthButton";

loadTranslations();

interface StoreConnectionsProps {
  storeStatus: Record<Store, string>;
  onLogout: (store: Store) => void;
  onStartAuth: (store: Store) => void;
}

const STORES: {
  key: Store;
  label: string;
  notInstalledStatus?: string;
  notInstalledMessage?: string;
}[] = [
  { key: "epic", label: "storeConnections.epicGames", notInstalledStatus: "legendary_not_installed", notInstalledMessage: "storeConnections.legendaryNotInstalled" },
  { key: "gog", label: "storeConnections.gog" },
  { key: "amazon", label: "storeConnections.amazonGames", notInstalledStatus: "nile_not_installed", notInstalledMessage: "storeConnections.nileNotInstalled" },
];

const StoreConnections = ({ storeStatus, onLogout, onStartAuth }: StoreConnectionsProps) => {
  return (
    <PanelSection title={t('storeConnections.title')}>
      <div style={{ display: "flex", flexDirection: "column", gap:"2px"}}>
        {STORES.map(({ key, label, notInstalledStatus, notInstalledMessage }) => {
          const status = storeStatus[key];
          const isConnected = status === "connected";
          const isCheckingOrError = status === "checking" || status === "error" || status === notInstalledStatus;

          return (
            <div key={key}>
              {/* Status indicators */}
              <div style={{ 
                display: "flex",
                alignItems: "center",
                flexDirection: "row",
                justifyContent: "space-between",
                padding: 0
              }}>
                <div style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}>
                  <StoreIcon store={key} size="18px" color={isConnected ? "#4ade80" : "#fff"} />
                  <span style={{ fontSize:"14px" }}>{t(label)}</span>
                </div>

                {!isCheckingOrError && (
                  <StoreAuthButton
                    store={key}
                    status={status}
                    onLogout={onLogout}
                    onStartAuth={onStartAuth}
                  />
                )}
              </div>


              {/* Error/warning messages */}
              {status === notInstalledStatus && notInstalledMessage && (
                <PanelSectionRow>
                  <Field description={t(notInstalledMessage)} />
                </PanelSectionRow>
              )}
            </div>
          );
        })}
      </div>
    </PanelSection>
  );
};

export default StoreConnections;
