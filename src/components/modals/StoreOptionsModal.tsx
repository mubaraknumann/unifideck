/**
 * StoreOptionsModal — the actions for one store row (or the "All stores"
 * row) in Settings → Stores: sync games, sync images, open the store,
 * sign in / out.
 *
 * A shell, not a feature: the caller builds the action buttons and wraps
 * each onClick so it closes the modal first, then acts. Modals render
 * outside the plugin's React providers, so the buttons are built by the
 * row, which can read the contexts, and handed in as `children`. Every
 * action closes the modal, so the result shows in the row's status line.
 * That also keeps a sign-in's own modal from stacking on this one.
 *
 * Callback, not the injected ``closeModal``: Steam's modal manager
 * overwrites that prop (see ``GameVaultConnectModal``). B / Esc /
 * background-dismiss reach ``onCancel``.
 *
 * The row styles travel with the modal: it is its own document, so the
 * stylesheet the row rendered does not reach it.
 */
import { FC, ReactNode } from "react";
import { Focusable, ModalRoot } from "@decky/ui";
import { STORE_ROW_CSS } from "../settings/storeConnections.css";
import type { StoreStatusLine } from "../settings/storeStatus";

interface Props {
  icon: ReactNode;
  title: string;
  status: StoreStatusLine;
  onCancel: () => void;
  /** The action buttons. */
  children: ReactNode;
}

export const StoreOptionsModal: FC<Props> = ({
  icon,
  title,
  status,
  onCancel,
  children,
}) => {
  const toneClass = status.tone === "error" ? " error" : "";
  return (
    <ModalRoot onCancel={onCancel}>
      <style>{STORE_ROW_CSS}</style>
      <div className="unifideck-store-modal-title">
        {icon}
        <span className="unifideck-store-row-name">{title}</span>
      </div>
      <div className={`unifideck-store-row-detail${toneClass}`}>
        {status.text}
        {status.detail ? ` — ${status.detail}` : ""}
      </div>
      <Focusable flow-children="column" className="unifideck-store-row-actions">
        {children}
      </Focusable>
    </ModalRoot>
  );
};

export default StoreOptionsModal;
