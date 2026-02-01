import { DialogButton } from "@decky/ui";
import React from "react";
import { Store } from "../../types/store";
import { FiLogOut } from "react-icons/fi";
import { t } from "../../i18n";

interface StoreAuthButtonProps {
  store: Store;
  status: string;
  onLogout: (store: Store) => void;
  onStartAuth: (store: Store) => void;
}

const StoreAuthButton: React.FC<StoreAuthButtonProps> = ({
  store,
  status,
  onLogout,
  onStartAuth,
}) => {
  const isConnected = status === "connected";
  const isInactive = status === "checking" || status === "error";

  if (isInactive) return null;

  return (
    <>
      <style>{`
        .store-auth-button.connected {
            background-color: #ef4444 !important;
            color: #fff;
        }
        .store-auth-button.connected:focus,
        .store-auth-button.connected:hover {
            color: #ef4444 !important;
            background-color: #fff !important;
        }
    `}</style>
      <DialogButton
        className={`store-auth-button ${
          isConnected ? "connected" : "disconnected"
        }`}
        onClick={() => (isConnected ? onLogout(store) : onStartAuth(store))}
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          padding: "4px 10px",
          fontSize: "10px",
          height: "28px",
          width: "fit-content",
          minWidth: "unset",
        }}
      >
        {isConnected ? (
          <FiLogOut size={12} />
        ) : (
          t("storeConnections.authenticate")
        )}
      </DialogButton>
    </>
  );
};

export default StoreAuthButton;
