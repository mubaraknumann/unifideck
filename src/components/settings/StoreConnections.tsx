/**
 * StoreConnections — per-store login status + Sign-in / Logout action.
 *
 * Rebuilt from staging:`src/components/settings/StoreConnections.tsx`:
 * rich rows with a prominent StoreIcon (green when connected, white
 * when disconnected), the localised display name, and the new
 * `StoreAuthButton` as the action target. Driver hooks (`useStores`,
 * `useStoreAuth`) come from the current architecture so the data
 * plane is unchanged.
 */
import { FC } from "react";
import { PanelSection, Focusable } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useStores } from "../../contexts/StoreContext";
import { useStoreAuth } from "../../hooks/useStoreAuth";
import { hasStorefront } from "../../services/store/StorefrontLauncher";
import { StoreIcon } from "../shared/StoreIcon";
import { StoreAuthButton } from "./StoreAuthButton";
import { StoreStorefrontButton } from "./StoreStorefrontButton";
import { STORE_ROW_CSS } from "./storeConnections.css";
import type { StoreId } from "../../types/api";

// ROW_CONFIG lived here: a per-store map of "if the status is
// `legendary_not_installed`, show `storeConnections.legendaryNotInstalled`".
// It could never fire. `StoreStatus` is a closed union of
// "connected" | "disconnected" | "expired" | "error", so the comparison was
// against a value the type cannot hold, and no backend has ever emitted
// either string. It also covered only 2 of the 3 CLI stores — GOG gained a
// CLITool in the §3.2 pass and never got a row.
//
// Telling the user *which* bundled CLI is missing is worth having: a lost
// exec bit is a real failure mode (`scripts/ensure_executable_bits.py`), and
// since §3.5 a missing gogdl makes GOG unavailable. Rebuilding it needs a
// real reason on the status payload, for all three stores — audit register
// item 50. A mechanism that cannot work is worse than none, so this one goes.

const StoreRow: FC<{ storeId: StoreId; displayName: string }> = ({
  storeId,
  displayName,
}) => {
  const { status, busy, connect, disconnect } = useStoreAuth(storeId);
  const isConnected = status === "connected";

  return (
    <div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          flexDirection: "row",
          justifyContent: "space-between",
          padding: 0,
        }}
      >
        <div style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <StoreIcon
            store={storeId}
            size="18px"
            color={isConnected ? "#4ade80" : "#fff"}
          />
          <span style={{ fontSize: 14 }}>{displayName}</span>
        </div>
        {/* One flex box, so the row keeps exactly two direct children
            and `space-between` still reads as "label left, actions
            right". A third direct child would spread them apart. */}
        <div style={{ display: "flex", gap: 6, flex: "0 0 auto" }}>
          {hasStorefront(storeId) && (
            <StoreStorefrontButton
              store={storeId}
              isConnected={isConnected}
              busy={busy}
            />
          )}
          <StoreAuthButton
            store={storeId}
            status={status ?? "disconnected"}
            busy={busy}
            onConnect={() => void connect()}
            onDisconnect={() => void disconnect()}
          />
        </div>
      </div>
    </div>
  );
};

export const StoreConnections: FC = () => {
  const { t } = useTranslation();
  const { stores, loading } = useStores();
  if (loading) return null;
  return (
    <PanelSection title={t("storeConnections.title")}>
      {/* Rendered once for the whole section, not per row. */}
      <style>{STORE_ROW_CSS}</style>
      {/* "grid", not "row" and not one Focusable per row: six rows each
          holding two buttons resolve as a 2-D grid. With a nav container
          per row, every vertical step re-enters a fresh container and
          lands on an arbitrary column — so moving down the cart column
          would jump to a sign-out button. Per-row containers stay plain
          divs. Same reasoning as DownloadsTab. */}
      <Focusable
        flow-children="grid"
        style={{ display: "flex", flexDirection: "column", gap: 2 }}
      >
        {stores.map((s) => (
          <StoreRow
            key={s.name}
            storeId={s.name}
            displayName={s.display_name}
          />
        ))}
      </Focusable>
    </PanelSection>
  );
};
