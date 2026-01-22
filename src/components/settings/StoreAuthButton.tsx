import { Button } from "@decky/ui";
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
        .store-auth-button {
            border: none;
            outline: none;
            color: #fff !important;
            transition: background-color 0.2s;
            background-color: #32373D !important;
        }
        .store-auth-button.connected {
            background-color: #ef4444 !important;
        }
        .store-auth-button:focus, .store-auth-button:hover {
            background-color: #fff !important;
            color: #000 !important;
            cursor: pointer;
        }
        .store-auth-button.connected:focus,
        .store-auth-button.connected:hover {
            background-color: #fff !important;
            color: #ef4444 !important;
        }
    `}</style>
      <Button
        className={`store-auth-button ${
          isConnected ? "connected" : "disconnected"
        }`}
        onClick={() => (isConnected ? onLogout(store) : onStartAuth(store))}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: "4px",
            fontSize: "10px",
          }}
        >
          {isConnected ? (
            <FiLogOut size={12} />
          ) : (
            t("storeConnections.authenticate")
          )}
        </div>
      </Button>
    </>
  );
};

export default StoreAuthButton;
