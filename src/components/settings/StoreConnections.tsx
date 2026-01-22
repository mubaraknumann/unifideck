import { PanelSection, PanelSectionRow, ButtonItem, Field } from "@decky/ui";
import { loadTranslations, t } from "../../i18n";
import StoreIcon from "../StoreIcon";
import { Store } from "../../types/store";

// Load translations on startup
loadTranslations();

interface StoreConnectionsProps {
  storeStatus: Record<Store, string>;
  onLogout: (store: Store) => void;
  onStartAuth: (store: Store) => void;
}

const STORES: { key: Store; label: string }[] = [
  { key: "epic", label: "storeConnections.epicGames" },
  { key: "gog", label: "storeConnections.gog" },
  { key: "amazon", label: "storeConnections.amazonGames" },
];

/**
 * Store Connections Settings Component
 */
const StoreConnections = ({ storeStatus, onLogout, onStartAuth }: StoreConnectionsProps) => {
  return (
    <PanelSection title={t('storeConnections.title')}>
      {/* Status indicators */}
      <PanelSectionRow>
        <Field
          description={
            <div style={{ display: "flex", flexDirection: "column", gap: "6px", fontSize: "13px" }}>
              {STORES.map(({ key, label }) => {
                const isConnected = storeStatus[key] === "connected";
                return (
                  <div key={key} style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                    {/* Status dot */}
                    <div
                      style={{
                        width: "8px",
                        height: "8px",
                        borderRadius: "50%",
                        backgroundColor: isConnected ? "#4ade80" : "#888",
                        flexShrink: 0,
                      }}
                    />
                    {/* Store icon */}
                    <StoreIcon store={key} size="18px" />
                    {/* Label */}
                    <span>
                      {t(label)} {isConnected ? "✓" : ""}
                    </span>
                  </div>
                );
              })}
            </div>
          }
        />
      </PanelSectionRow>

      {/* Action buttons */}
      {storeStatus.epic !== 'checking' && storeStatus.epic !== 'legendary_not_installed' && storeStatus.epic !== 'error' && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => storeStatus.epic === 'connected' ? onLogout('epic') : onStartAuth('epic')}
          >
            {storeStatus.epic === 'connected' 
              ? t('storeConnections.logout', { store: t('storeConnections.epicGames') }) 
              : t('storeConnections.authenticate', { store: t('storeConnections.epicGames') })}
          </ButtonItem>
        </PanelSectionRow>
      )}

      {storeStatus.gog !== 'checking' && storeStatus.gog !== 'error' && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => storeStatus.gog === 'connected' ? onLogout('gog') : onStartAuth('gog')}
          >
            {storeStatus.gog === 'connected' 
              ? t('storeConnections.logout', { store: t('storeConnections.gog') }) 
              : t('storeConnections.authenticate', { store: t('storeConnections.gog') })}
          </ButtonItem>
        </PanelSectionRow>
      )}

      {storeStatus.amazon !== 'checking' && storeStatus.amazon !== 'nile_not_installed' && storeStatus.amazon !== 'error' && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => storeStatus.amazon === 'connected' ? onLogout('amazon') : onStartAuth('amazon')}
          >
            {storeStatus.amazon === 'connected' 
              ? t('storeConnections.logout', { store: t('storeConnections.amazonGames') }) 
              : t('storeConnections.authenticate', { store: t('storeConnections.amazonGames') })}
          </ButtonItem>
        </PanelSectionRow>
      )}

      {/* Error/warning messages */}
      {storeStatus.epic === 'legendary_not_installed' && (
        <PanelSectionRow>
          <Field description={t('storeConnections.legendaryNotInstalled')} />
        </PanelSectionRow>
      )}
      {storeStatus.amazon === 'nile_not_installed' && (
        <PanelSectionRow>
          <Field description={t('storeConnections.nileNotInstalled')} />
        </PanelSectionRow>
      )}
    </PanelSection>
  );
};

export default StoreConnections;
