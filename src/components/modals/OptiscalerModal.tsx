/**
 * OptiscalerModal — apply/remove Frame Generation (OptiScaler) for a game.
 *
 * Opened from the "Frame Generation (OptiScaler)…" item injected into the
 * native game context menu (see
 * {@link file://./../../lib/steam-bridge/app-context-menu-patch.ts}).
 *
 * This replaces pasting Decky-Framegen's ``~/fgmod/fgmod %command%`` into a
 * Unifideck shortcut's Steam Launch Options — that never worked, because a
 * Unifideck shortcut's ``Exe`` always points at ``unifideck-launcher``, not
 * the game's own binary, so fgmod's argv-sniffing for a ``*.exe`` never
 * finds one and DLSS/FSR files ended up copied next to the launcher script
 * instead of the game folder. This modal instead calls the backend
 * (``OptiScalerRPCMixin``), which already knows the correct install
 * directory and drives Decky-Framegen's own installed ``~/fgmod/fgmod``
 * wrapper directly with that path — Decky-Framegen itself is still required
 * (this never bundles or downloads OptiScaler/DLSS-Enabler on its own).
 *
 * Environment-variable overrides for OptiScaler live in the GENERAL
 * "Environment variables…" modal ({@link GameEnvModal}), not here — one
 * place to set them, applied to both the game's own launch and the patch
 * step, instead of two separate env editors for the same game.
 */
import { FC, useState } from "react";
import { ConfirmModal, DialogButton, Focusable } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaBolt, FaCheck, FaTimes } from "react-icons/fa";
import { rpcRoutes } from "../../api/rpc-routes";
import { useRPCQuery, useRPCMutation } from "../../api/useRPC";
import { useToast } from "../../hooks/useToast";

export interface OptiscalerStatus {
  fgmod_installed: boolean;
  install_dir: string;
  patched: boolean;
  /** Read-only mirror of the general "Environment variables…" store —
   *  what WOULD be passed to fgmod on the next patch. Edited elsewhere. */
  env: Record<string, string>;
}

interface MutateResult {
  success?: boolean;
  output?: string;
  env?: Record<string, string>;
}

interface Props {
  store: string;
  gameId: string;
  gameTitle: string;
  closeModal: () => void;
}

export const OptiscalerModal: FC<Props> = ({
  store,
  gameId,
  gameTitle,
  closeModal,
}) => {
  const { t } = useTranslation();
  const toast = useToast();

  const status = useRPCQuery<[string, string], OptiscalerStatus>(
    rpcRoutes.getOptiscalerStatus,
    [store, gameId],
  );
  const patchMutation = useRPCMutation<[string, string], MutateResult>(
    rpcRoutes.applyOptiscalerPatch,
  );
  const unpatchMutation = useRPCMutation<[string, string], MutateResult>(
    rpcRoutes.removeOptiscalerPatch,
  );

  const [busy, setBusy] = useState(false);

  const working = busy || patchMutation.loading || unpatchMutation.loading;

  const applyPatch = async () => {
    setBusy(true);
    try {
      const res = await patchMutation.mutate(store, gameId);
      if (res && res.success !== false) {
        const envCount = Object.keys(res.env ?? {}).length;
        toast.success(
          envCount > 0
            ? t("play.optiscaler.patchedWithEnv", { count: envCount })
            : t("play.optiscaler.patched"),
          gameTitle,
        );
        await status.refetch();
      } else {
        toast.error(t("play.optiscaler.patchFailed"), gameTitle);
      }
    } catch {
      toast.error(t("play.optiscaler.patchFailed"), gameTitle);
    } finally {
      setBusy(false);
    }
  };

  const removePatch = async () => {
    setBusy(true);
    try {
      const res = await unpatchMutation.mutate(store, gameId);
      if (res && res.success !== false) {
        toast.success(t("play.optiscaler.unpatched"), gameTitle);
        await status.refetch();
      } else {
        toast.error(t("play.optiscaler.unpatchFailed"), gameTitle);
      }
    } catch {
      toast.error(t("play.optiscaler.unpatchFailed"), gameTitle);
    } finally {
      setBusy(false);
    }
  };

  const data = status.data;
  const fgmodMissing = !status.loading && data && !data.fgmod_installed;
  const envEntries = Object.entries(data?.env ?? {});

  return (
    <ConfirmModal
      strTitle={t("play.optiscaler.title", { game: gameTitle })}
      bAlertDialog
      strOKButtonText={t("common.close")}
      onOK={closeModal}
      onCancel={closeModal}
    >
      <div style={{ marginBottom: 8, opacity: 0.8, fontSize: "0.9em" }}>
        {t("play.optiscaler.subtitle")}
      </div>

      {status.loading && <div>{t("common.loading")}</div>}

      {fgmodMissing && (
        <div style={{ marginBottom: 12, opacity: 0.9 }}>
          {t("play.optiscaler.fgmodMissing")}
        </div>
      )}

      {data && (
        <>
          <div
            style={{
              marginBottom: 4,
              display: "flex",
              alignItems: "center",
              gap: 8,
            }}
          >
            {data.patched ? <FaCheck /> : <FaTimes style={{ opacity: 0.5 }} />}
            <span>
              {data.patched
                ? t("play.optiscaler.statusPatched")
                : t("play.optiscaler.statusNotPatched")}
            </span>
          </div>
          <div
            style={{
              marginBottom: 12,
              opacity: 0.6,
              fontSize: "0.8em",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={data.install_dir}
          >
            {t("play.optiscaler.installDir")}: {data.install_dir || "—"}
          </div>

          <Focusable style={{ display: "flex", gap: 8, marginBottom: 16 }}>
            {!data.patched ? (
              <DialogButton
                disabled={working || !data.fgmod_installed || !data.install_dir}
                onClick={() => void applyPatch()}
              >
                <FaBolt style={{ marginInlineEnd: 8 }} />
                {t("play.optiscaler.patch")}
              </DialogButton>
            ) : (
              <DialogButton
                disabled={working}
                onClick={() => void removePatch()}
              >
                <FaTimes style={{ marginInlineEnd: 8 }} />
                {t("play.optiscaler.unpatch")}
              </DialogButton>
            )}
          </Focusable>

          <div style={{ marginBottom: 4, fontWeight: 600, fontSize: "0.9em" }}>
            {t("play.optiscaler.envAppliedTitle")}
          </div>
          {envEntries.length === 0 ? (
            <div style={{ marginBottom: 8, opacity: 0.6, fontSize: "0.8em" }}>
              {t("play.optiscaler.envAppliedEmpty")}
            </div>
          ) : (
            <div
              style={{
                marginBottom: 8,
                fontFamily: "monospace",
                fontSize: "0.8em",
                color: "#c7d5e0",
              }}
            >
              {envEntries.map(([k, v]) => (
                <div key={k}>
                  {k}={v}
                </div>
              ))}
            </div>
          )}
          <div style={{ opacity: 0.6, fontSize: "0.8em" }}>
            {t("play.optiscaler.envHint")}
          </div>
        </>
      )}
    </ConfirmModal>
  );
};
