/**
 * GameVaultConnectModal — pick how to connect GameVault.
 *
 * GameVault is the one store with two genuinely different shapes of
 * connection, so Sign In asks which before it asks for anything else:
 *
 *  - a **remote server** the user hosts somewhere (URL + account), or
 *  - a **local folder** on this device that they drop game archives into.
 *
 * The two are mutually exclusive: one GameVault connection, one mode.
 * Switching is a disconnect followed by a reconnect, which is why this
 * chooser is the entry point rather than a toggle inside one big form.
 *
 * Callbacks, not the injected ``closeModal``: Steam's modal manager
 * overwrites that prop, so a component that routes its own logic through it
 * silently loses the callback. ``pickStorageForInstall`` established the
 * pattern — own props plus the caller's ``showModal`` handle.
 *
 * ``ModalRoot`` rather than ``ConfirmModal``: picking an option *is* the
 * action, so there is nothing for an OK button to confirm. This used to be a
 * ``ConfirmModal`` with ``bOKDisabled``, which rendered a permanently greyed
 * "Confirm" next to a Cancel that duplicated the B button. ``ModalRoot``
 * keeps the close icon and routes B / Esc / background-dismiss to
 * ``onCancel``, so the flow still settles exactly once.
 */
import { FC } from "react";
import { DialogButton, Focusable, ModalRoot } from "@decky/ui";
import { useTranslation } from "react-i18next";

export type GameVaultMode = "remote" | "local";

interface Props {
  onPick: (mode: GameVaultMode) => void;
  onCancel: () => void;
}

const optionStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "flex-start",
  gap: 2,
  padding: "12px 14px",
  height: "auto",
  width: "100%",
  textAlign: "left",
};

const hintStyle: React.CSSProperties = {
  fontSize: "12px",
  opacity: 0.7,
  whiteSpace: "normal",
};

const titleStyle: React.CSSProperties = {
  fontSize: "22px",
  fontWeight: 500,
  marginBottom: "14px",
};

export const GameVaultConnectModal: FC<Props> = ({ onPick, onCancel }) => {
  const { t } = useTranslation();

  return (
    <ModalRoot onCancel={onCancel}>
      <div style={titleStyle}>{t("gamevault.connectTitle")}</div>

      <Focusable
        style={{ display: "flex", flexDirection: "column", gap: "10px" }}
      >
        <DialogButton style={optionStyle} onClick={() => onPick("remote")}>
          <div>{t("gamevault.modeRemote")}</div>
          <div style={hintStyle}>{t("gamevault.modeRemoteDescription")}</div>
        </DialogButton>

        <DialogButton style={optionStyle} onClick={() => onPick("local")}>
          <div>{t("gamevault.modeLocal")}</div>
          <div style={hintStyle}>{t("gamevault.modeLocalDescription")}</div>
        </DialogButton>
      </Focusable>
    </ModalRoot>
  );
};

export default GameVaultConnectModal;
