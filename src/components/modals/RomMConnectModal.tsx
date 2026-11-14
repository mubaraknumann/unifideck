/**
 * RomMConnectModal: the whole RomM connection in one dialog.
 *
 * The server form, device pairing and optional platform choices share one
 * modal. The connection is committed only at `onComplete`: after validation
 * succeeds, and after the platform choice when there is one to make.
 *
 * The modal owns UI state and pairing polls; `connectRomM` owns the backend
 * attempt and RPCs. `onBegin` and `onComplete` throw on failure so the modal
 * can show the error inline and allow a retry.
 */
import { FC, useCallback, useEffect, useState } from "react";
import { encode } from "uqr";
import {
  ConfirmModal,
  DialogButton,
  Field,
  TextField,
  ToggleField,
} from "@decky/ui";
import { useTranslation } from "react-i18next";

import { StoragePathPicker } from "./StoragePathPicker";

export interface RomMVariant {
  platform_slug: string;
  fs_slug: string;
  display_name: string;
}

/**
 * Variants the user can choose between: those of a canonical platform that
 * the server hosts under more than one filesystem folder. A platform with a
 * single folder is always imported and needs no checkbox.
 */
export function choosableVariants(variants: RomMVariant[]): RomMVariant[] {
  const perPlatform = new Map<string, number>();
  for (const v of variants) {
    perPlatform.set(
      v.platform_slug,
      (perPlatform.get(v.platform_slug) ?? 0) + 1,
    );
  }
  return variants.filter((v) => (perPlatform.get(v.platform_slug) ?? 0) > 1);
}

export interface RomMConnectValues {
  serverUrl: string;
  token: string;
  verifySsl: boolean;
  downloadDir: string;
}

export interface RomMPairing {
  user_code: string;
  verification_url: string;
  expires_in: number;
  interval: number;
}

export function pairingQr(
  value: string,
): { size: number; path: string } | null {
  try {
    const { data, size } = encode(value, { ecc: "M", border: 4 });
    const path = data
      .flatMap((row, y) =>
        row.flatMap((dark, x) => (dark ? [`M${x},${y}h1v1h-1z`] : [])),
      )
      .join("");
    return { size, path };
  } catch {
    return null;
  }
}

interface Props {
  onBegin: (values: RomMConnectValues) => Promise<RomMVariant[] | RomMPairing>;
  onPoll: () => Promise<RomMVariant[] | null>;
  /** `excludedFsSlugs` lists the variants the user switched off. */
  onComplete: (excludedFsSlugs: string[]) => Promise<void>;
  onCancel: () => void;
}

export const RomMConnectModal: FC<Props> = ({
  onBegin,
  onPoll,
  onComplete,
  onCancel,
}) => {
  const { t } = useTranslation();

  const [serverUrl, setServerUrl] = useState("http://");
  const [token, setToken] = useState("");
  const [useToken, setUseToken] = useState(false);
  const [pairing, setPairing] = useState<RomMPairing | null>(null);
  const [verifySsl, setVerifySsl] = useState(true);
  const [downloadDir, setDownloadDir] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [variants, setVariants] = useState<RomMVariant[] | null>(null);
  const [selected, setSelected] = useState<Record<string, boolean>>({});

  const acceptVariants = useCallback(
    async (found: RomMVariant[]) => {
      setPairing(null);
      if (found.length === 0) {
        setError(t("romm.errorNoPlatforms"));
        return;
      }
      setToken("");
      setSelected(Object.fromEntries(found.map((v) => [v.fs_slug, true])));
      setVariants(found);
      if (choosableVariants(found).length === 0) {
        try {
          await onComplete([]);
        } catch (err) {
          setError(
            err instanceof Error ? err.message : t("romm.errorConnection"),
          );
        }
      }
    },
    [onComplete, t],
  );

  useEffect(() => {
    if (!pairing) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const found = await onPoll();
        if (!active) return;
        if (found) {
          await acceptVariants(found);
          return;
        }
        timer = setTimeout(() => void poll(), pairing.interval * 1000);
      } catch (err) {
        if (!active) return;
        setPairing(null);
        setError(
          err instanceof Error ? err.message : t("romm.errorConnection"),
        );
      }
    };
    timer = setTimeout(() => void poll(), pairing.interval * 1000);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [pairing, onPoll, acceptVariants, t]);

  const handleBegin = async () => {
    if (loading) return;
    if (!serverUrl || serverUrl === "http://" || serverUrl === "https://") {
      setError(t("romm.errorServerUrlRequired"));
      return;
    }
    const pasted = useToken ? token.trim() : "";
    if (useToken && !pasted) {
      setError(t("romm.errorTokenRequired"));
      return;
    }
    setError(null);
    setLoading(true);
    try {
      const found = await onBegin({
        serverUrl,
        token: pasted,
        verifySsl,
        downloadDir,
      });
      if (Array.isArray(found)) await acceptVariants(found);
      else setPairing(found);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg || t("romm.errorConnection"));
    } finally {
      setLoading(false);
    }
  };

  const handleComplete = async () => {
    if (loading || !variants) return;
    const excluded = choosableVariants(variants)
      .filter((v) => !selected[v.fs_slug])
      .map((v) => v.fs_slug);
    if (excluded.length === variants.length) {
      setError(t("romm.errorSelectOne"));
      return;
    }
    setError(null);
    setLoading(true);
    try {
      await onComplete(excluded);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg || t("romm.errorConnection"));
    } finally {
      setLoading(false);
    }
  };

  if (browsing) {
    return (
      <ConfirmModal
        strTitle={t("romm.downloadDir")}
        bAlertDialog
        strOKButtonText={t("romm.cancel")}
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

  const errorBanner = error && (
    <div style={{ color: "#ef4444", fontSize: "12px", padding: "4px 0" }}>
      {error}
    </div>
  );

  if (pairing) {
    const qr = pairingQr(pairing.verification_url);
    return (
      <ConfirmModal
        strTitle={t("romm.pairingTitle")}
        strOKButtonText={t("romm.waitingApproval")}
        strCancelButtonText={t("romm.cancel")}
        bOKDisabled
        onCancel={onCancel}
      >
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: "8px",
          }}
        >
          <div>{t("romm.pairingIntro")}</div>
          {qr && (
            <svg
              role="img"
              aria-label={t("romm.pairingQrLabel")}
              width={200}
              height={200}
              viewBox={`0 0 ${qr.size} ${qr.size}`}
              shapeRendering="crispEdges"
            >
              <rect width={qr.size} height={qr.size} fill="white" />
              <path d={qr.path} fill="black" />
            </svg>
          )}
          <strong style={{ fontSize: "24px", letterSpacing: "2px" }}>
            {pairing.user_code}
          </strong>
          <div style={{ fontSize: "12px", wordBreak: "break-all" }}>
            {pairing.verification_url}
          </div>
          <div style={{ fontSize: "12px" }}>
            {t("romm.pairingExpiry", {
              minutes: Math.ceil(pairing.expires_in / 60),
            })}
          </div>
        </div>
      </ConfirmModal>
    );
  }

  if (variants) {
    return (
      <ConfirmModal
        strTitle={t("romm.platformsTitle")}
        strOKButtonText={loading ? t("romm.connecting") : t("romm.finish")}
        strCancelButtonText={t("romm.cancel")}
        bOKDisabled={loading}
        onOK={handleComplete}
        onCancel={onCancel}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          <div style={{ fontSize: "13px" }}>{t("romm.platformsIntro")}</div>
          {variants.length === 1 ? (
            <div>{variants[0].display_name}</div>
          ) : (
            choosableVariants(variants).map((v) => (
              <ToggleField
                key={v.fs_slug}
                label={v.display_name}
                description={v.fs_slug}
                checked={!!selected[v.fs_slug]}
                disabled={loading}
                onChange={(on) =>
                  setSelected((prev) => ({ ...prev, [v.fs_slug]: on }))
                }
              />
            ))
          )}
          {errorBanner}
        </div>
      </ConfirmModal>
    );
  }

  return (
    <ConfirmModal
      strTitle={t("romm.connectTitle")}
      strOKButtonText={loading ? t("romm.connecting") : t("romm.connect")}
      strCancelButtonText={t("romm.cancel")}
      bOKDisabled={loading}
      onOK={handleBegin}
      onCancel={onCancel}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
        <div style={{ fontSize: "12px" }}>{t("romm.intro")}</div>
        <TextField
          label={t("romm.serverUrl")}
          value={serverUrl}
          disabled={loading}
          onChange={(e) => setServerUrl(e.target.value)}
        />
        <ToggleField
          label={t("romm.useToken")}
          checked={useToken}
          disabled={loading}
          onChange={setUseToken}
        />
        {useToken && (
          <TextField
            label={t("romm.token")}
            description={t("romm.tokenDescription")}
            value={token}
            disabled={loading}
            onChange={(e) => setToken(e.target.value)}
            bIsPassword
          />
        )}
        <ToggleField
          label={t("romm.verifySsl")}
          description={t("romm.verifySslDescription")}
          checked={verifySsl}
          disabled={loading}
          onChange={setVerifySsl}
        />
        <Field
          label={t("romm.downloadDir")}
          description={`${t("romm.downloadDirDescription")} (${t(
            "romm.downloadDirPlaceholder",
          )})`}
          bottomSeparator="none"
        >
          <DialogButton disabled={loading} onClick={() => setBrowsing(true)}>
            {t("romm.browse")}
          </DialogButton>
        </Field>
        {downloadDir && (
          <div style={{ fontSize: "12px", wordBreak: "break-all" }}>
            {downloadDir}
          </div>
        )}
        {errorBanner}
      </div>
    </ConfirmModal>
  );
};

export default RomMConnectModal;
