/**
 * StoreAuthButton — compact connect / disconnect button used in
 * `StoreConnections` per-store rows.
 *
 * Two visual states: disconnected (standard button with the
 * "authenticate" label) and connected (red background with a
 * logout icon; hover/focus inverts to white-on-red).
 *
 * **A button is always rendered.** This used to return null for
 * `"checking"` / `"error"`, which made the whole row blank whenever a
 * backend `STORE_AUTH_FAILED` reached `auth-store` — the status is sticky,
 * so the row lost its only affordance for the rest of the session (and
 * across reloads, via event replay). `wrapper_auth_monitor` states the rule
 * this now enforces: the one answer worse than a button that does nothing is
 * a button that is not there at all. `"error"` and `"expired"` mean "not
 * usably signed in", so they render the disconnected variant — a working
 * Connect button, which is the action that resolves both.
 *
 * (`"checking"` was never in the `StoreStatus` union and could not be
 * reached; it is gone rather than carried forward.)
 *
 * Ported from `staging:src/components/settings/StoreAuthButton.tsx`.
 */
import { FC } from "react";
import { DialogButton } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FiLogOut } from "react-icons/fi";
import type { StoreId } from "../../types/api";

interface Props {
  store: StoreId;
  status: string;
  onConnect: (store: StoreId) => void;
  onDisconnect: (store: StoreId) => void;
  busy?: boolean;
}

export const StoreAuthButton: FC<Props> = ({
  store,
  status,
  onConnect,
  onDisconnect,
  busy,
}) => {
  const { t } = useTranslation();
  const isConnected = status === "connected";
  // Styles live in `storeConnections.css.ts` and are rendered once by
  // `StoreConnections`. They used to be an inline `<style>` here, which
  // meant the same block was parsed once per store row.
  return (
    <>
      <DialogButton
        disabled={busy}
        className={`unifideck-store-auth-button ${
          isConnected ? "connected" : "disconnected"
        }`}
        onClick={() => (isConnected ? onDisconnect(store) : onConnect(store))}
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          padding: "4px 10px",
          fontSize: 10,
          height: 28,
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
