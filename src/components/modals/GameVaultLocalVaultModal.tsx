/**
 * GameVaultLocalVaultModal — choose the folder you drop game archives into.
 *
 * One setting, deliberately. An earlier version also asked where games
 * should be *installed*, which was a second answer to a question the plugin
 * already asks: `useInstallFlow` runs `pickStorageForInstall` before every
 * install, for every store, and that picker is the one that knows about SD
 * cards and USB drives. A GameVault-only install-location setting would have
 * disagreed with it the first time a user changed one of them.
 *
 * The folder browser is the shared `StoragePathPicker` for the same reason —
 * it is backed by `get_browseable_devices`, so real mount points (SD card,
 * USB) are one tap away, and it brings Up-navigation, a hidden-files toggle,
 * sorting and folder creation. `openFilePicker` from `@decky/api` has none
 * of that, and a vault on an SD card is the common case on a Deck.
 *
 * The path is shown, not typed. There used to be a `TextField` beside the
 * Browse button answering the same question twice; since the picker can
 * reach any mount point and create folders, the field only added a way to
 * type a path that does not exist. Browsing swaps the whole modal body for
 * the picker, the way `CloudSaveModal` does, rather than expanding inline.
 *
 * The vault folder is created on connect, with a README and a marker file.
 * The marker is what lets a later sync tell "the vault is empty" from "the
 * drive did not mount", so an SD card that is slow to appear never looks
 * like a library the user deleted.
 */
import { FC, useState } from "react";
import { ConfirmModal, DialogButton, Field } from "@decky/ui";
import { useTranslation } from "react-i18next";

import { StoragePathPicker } from "./StoragePathPicker";

interface Props {
  /** Resolve/reject decides whether the caller closes the modal. Rejecting
   *  keeps it open with the message shown inline. */
  onSubmit: (vaultDir: string) => Promise<void>;
  onCancel: () => void;
  /** Pre-fill, when a vault is already configured. */
  initialVaultDir?: string;
}

export const GameVaultLocalVaultModal: FC<Props> = ({
  onSubmit,
  onCancel,
  initialVaultDir = "",
}) => {
  const { t } = useTranslation();

  const [vaultDir, setVaultDir] = useState(initialVaultDir);
  const [browsing, setBrowsing] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleConnect = async () => {
    // May be left blank: the backend falls back to the configured default
    // and creates the folder. "Connect" on an untouched form is the
    // intended happy path.
    setError(null);
    setLoading(true);
    try {
      await onSubmit(vaultDir.trim());
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg || t("gamevault.errorConnection"));
    } finally {
      setLoading(false);
    }
  };

  // Folder-picker mode — replaces the form body, so the picker gets the
  // whole dialog instead of a scrolling box inside it.
  if (browsing) {
    return (
      <ConfirmModal
        strTitle={t("gamevault.vaultDir")}
        bAlertDialog
        strOKButtonText={t("common.cancel")}
        onOK={() => setBrowsing(false)}
        onCancel={() => setBrowsing(false)}
      >
        <StoragePathPicker
          startPath={vaultDir || "/home/deck"}
          onConfirm={(path) => {
            setVaultDir(path);
            setBrowsing(false);
          }}
        />
      </ConfirmModal>
    );
  }

  return (
    <ConfirmModal
      strTitle={t("gamevault.localTitle")}
      strOKButtonText={
        loading ? t("gamevault.connecting") : t("gamevault.connect")
      }
      strCancelButtonText={t("gamevault.cancel")}
      bOKDisabled={loading}
      onOK={handleConnect}
      onCancel={onCancel}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
        <div style={{ fontSize: "12px", opacity: 0.8 }}>
          {t("gamevault.localIntro")}
        </div>

        <Field
          label={t("gamevault.vaultDir")}
          description={t("gamevault.vaultDirDescription")}
          bottomSeparator="none"
        >
          <DialogButton onClick={() => setBrowsing(true)}>
            {t("gamevault.browse")}
          </DialogButton>
        </Field>

        {vaultDir && (
          <div style={{ fontSize: "12px", wordBreak: "break-all" }}>
            {vaultDir}
          </div>
        )}

        {error && (
          <div style={{ color: "#ef4444", fontSize: "12px", padding: "4px 0" }}>
            {error}
          </div>
        )}
      </div>
    </ConfirmModal>
  );
};

export default GameVaultLocalVaultModal;
