/**
 * GameVaultCredentialsModal — connection form for a self-hosted
 * GameVault server.
 *
 * Fields:
 *  serverUrl   — HTTP(S) URL of the GameVault server
 *  username    — GameVault account username
 *  password    — GameVault account password
 *  verifySsl   — toggle to skip TLS certificate validation
 *                (useful for self-signed certs on LAN servers)
 *  downloadDir — *separate* temporary directory for archive
 *                downloads.  The game archive is placed here
 *                while downloading, then extracted to the final
 *                install location and deleted.  Keeping this on
 *                a different drive/partition lets users install
 *                to an SSD that doesn't have enough space for
 *                both the archive AND the extracted game at the
 *                same time.
 */
import { FC, useState } from "react";
import {
  ConfirmModal,
  DialogButton,
  Field,
  TextField,
  ToggleField,
} from "@decky/ui";
import { useTranslation } from "react-i18next";

import { StoragePathPicker } from "./StoragePathPicker";

interface Props {
  /** Rejecting keeps the modal open with the message shown inline, so a
   *  wrong password can be corrected without retyping the server URL. The
   *  caller closes the modal on success — Steam's modal manager overwrites
   *  any injected ``closeModal``, so routing our own flow through that prop
   *  loses the callback (see ``pickStorageForInstall``). */
  onSubmit: (
    serverUrl: string,
    username: string,
    password: string,
    verifySsl: boolean,
    downloadDir: string,
  ) => Promise<void>;
  onCancel: () => void;
  /** Pre-fill values (e.g. when re-opening an already-configured connection) */
  initialServerUrl?: string;
  initialUsername?: string;
  initialDownloadDir?: string;
  initialVerifySsl?: boolean;
}

export const GameVaultCredentialsModal: FC<Props> = ({
  onSubmit,
  onCancel,
  initialServerUrl = "http://",
  initialUsername = "",
  initialDownloadDir = "",
  initialVerifySsl = true,
}) => {
  const { t } = useTranslation();

  const [serverUrl, setServerUrl] = useState(initialServerUrl);
  const [username, setUsername] = useState(initialUsername);
  const [password, setPassword] = useState("");
  const [verifySsl, setVerifySsl] = useState(initialVerifySsl);
  const [downloadDir, setDownloadDir] = useState(initialDownloadDir);
  const [browsing, setBrowsing] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleConnect = async () => {
    if (!serverUrl || serverUrl === "http://" || serverUrl === "https://") {
      setError(t("gamevault.errorServerUrlRequired"));
      return;
    }
    if (!username) {
      setError(t("gamevault.errorUsernameRequired"));
      return;
    }
    if (!password) {
      setError(t("gamevault.errorPasswordRequired"));
      return;
    }

    setError(null);
    setLoading(true);
    try {
      await onSubmit(serverUrl, username, password, verifySsl, downloadDir);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg || t("gamevault.errorConnection"));
    } finally {
      setLoading(false);
    }
  };

  // Folder-picker mode — replaces the form body, so the picker gets the
  // whole dialog instead of a scrolling box below the credentials.
  if (browsing) {
    return (
      <ConfirmModal
        strTitle={t("gamevault.downloadDir")}
        bAlertDialog
        strOKButtonText={t("common.cancel")}
        onOK={() => setBrowsing(false)}
        onCancel={() => setBrowsing(false)}
      >
        <StoragePathPicker
          startPath={downloadDir || "/home/deck"}
          onConfirm={(path) => {
            setDownloadDir(path);
            setBrowsing(false);
          }}
        />
      </ConfirmModal>
    );
  }

  return (
    <ConfirmModal
      strTitle={t("gamevault.connectTitle")}
      strOKButtonText={
        loading ? t("gamevault.connecting") : t("gamevault.connect")
      }
      strCancelButtonText={t("gamevault.cancel")}
      bOKDisabled={loading}
      onOK={handleConnect}
      onCancel={onCancel}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
        {/* ── Server URL ───────────────────────────────────────── */}
        <TextField
          label={t("gamevault.serverUrl")}
          value={serverUrl}
          onChange={(e) => setServerUrl(e.target.value)}
        />

        {/* ── Credentials ──────────────────────────────────────── */}
        <TextField
          label={t("gamevault.username")}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />

        <TextField
          label={t("gamevault.password")}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          bIsPassword
        />

        {/* ── TLS toggle ───────────────────────────────────────── */}
        <ToggleField
          label={t("gamevault.verifySsl")}
          description={t("gamevault.verifySslDescription")}
          checked={verifySsl}
          onChange={setVerifySsl}
        />

        {/* ── Temp download directory ──────────────────────────── */}
        {/* Browsed with the shared StoragePathPicker, not
            ``openFilePicker``: this directory is most useful when it is on a
            different drive from the install target, which means the device
            list from ``get_browseable_devices`` is the whole point. */}
        <Field
          label={t("gamevault.downloadDir")}
          description={`${t("gamevault.downloadDirDescription")} (${t(
            "gamevault.downloadDirPlaceholder",
          )})`}
          bottomSeparator="none"
        >
          <DialogButton onClick={() => setBrowsing(true)}>
            {t("gamevault.browse")}
          </DialogButton>
        </Field>

        {downloadDir && (
          <div style={{ fontSize: "12px", wordBreak: "break-all" }}>
            {downloadDir}
          </div>
        )}

        {/* ── Error banner ─────────────────────────────────────── */}
        {error && (
          <div
            style={{
              color: "#ef4444",
              fontSize: "12px",
              padding: "4px 0",
            }}
          >
            {error}
          </div>
        )}
      </div>
    </ConfirmModal>
  );
};

export default GameVaultCredentialsModal;
