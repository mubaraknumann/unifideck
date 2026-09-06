/**
 * GameEnvModal — set environment-variable overrides applied to a game's
 * OWN launch (MangoHud, DXVK_HUD, PROTON_* tweaks, ...).
 *
 * Opened from the "Environment variables…" item injected into the native
 * game context menu (see
 * {@link file://./../../lib/steam-bridge/app-context-menu-patch.ts}), next
 * to "Frame Generation (OptiScaler)…".
 *
 * Functionally the same as Steam's ``VAR=value %command%`` Launch Options
 * convention (see ``docs/launch-options.md``), but persists across a Force
 * Sync (which resets Launch Options back to plain ``store:game_id``) and
 * doesn't require typing into Steam's launch-options field with a gamepad.
 *
 * Also doubles as the OptiScaler config-var editor ({@link OptiscalerModal}
 * reads/merges the SAME ``games.<store>:<game_id>.env_overrides`` store) —
 * one place to set env vars per game, not two.
 *
 * Row-based add/remove, NOT a
 * multi-line ``<textarea>``: a plain HTML ``<textarea>``/``<input>`` never
 * gets focus or triggers Steam's on-screen keyboard in this CEF gamepad UI
 * — only decky-ui's own ``TextField`` wires that up. A textarea LOOKS like
 * an input field but is simply unusable with a controller.
 */
import { FC, useState } from "react";
import { ConfirmModal, DialogButton, Focusable, TextField } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaPlus, FaTrash } from "react-icons/fa";
import { rpcRoutes } from "../../api/rpc-routes";
import { useRPCQuery, useRPCMutation } from "../../api/useRPC";
import { useToast } from "../../hooks/useToast";

export interface GameEnvStatus {
  env: Record<string, string>;
}

interface EnvMutateResult {
  success?: boolean;
  env?: Record<string, string>;
}

interface Props {
  store: string;
  gameId: string;
  gameTitle: string;
  closeModal: () => void;
}

const rowStyle = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 8,
  width: "100%",
  // BOTH minWidth: 0 on the row AND
  // flex: "0 0 auto" on the trash button are required — without either,
  // the filename/key text gets squeezed to zero visible width by
  // DialogButton's own flex-grow: 1 default in this CEF build.
  minWidth: 0,
} as const;

export const GameEnvModal: FC<Props> = ({
  store,
  gameId,
  gameTitle,
  closeModal,
}) => {
  const { t } = useTranslation();
  const toast = useToast();

  const status = useRPCQuery<[string, string], GameEnvStatus>(
    rpcRoutes.getGameEnv,
    [store, gameId],
  );
  const envMutation = useRPCMutation<
    [string, string, Record<string, string>],
    EnvMutateResult
  >(rpcRoutes.setGameEnv);

  const [busy, setBusy] = useState(false);
  const [pendingKey, setPendingKey] = useState("");
  const [pendingValue, setPendingValue] = useState("");

  const env = status.data?.env ?? {};
  const working = busy || envMutation.loading;

  const persist = async (next: Record<string, string>) => {
    setBusy(true);
    try {
      const res = await envMutation.mutate(store, gameId, next);
      if (res && res.success !== false) {
        await status.refetch();
        return true;
      }
      toast.error(t("play.gameEnv.saveFailed"), gameTitle);
      return false;
    } catch {
      toast.error(t("play.gameEnv.saveFailed"), gameTitle);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const addOne = async () => {
    const key = pendingKey.trim();
    if (!key) return;
    const next = { ...env, [key]: pendingValue };
    const ok = await persist(next);
    if (ok) {
      toast.success(t("play.gameEnv.saved"), `${key}=${pendingValue}`);
      setPendingKey("");
      setPendingValue("");
    }
  };

  const removeOne = async (key: string) => {
    const next = { ...env };
    delete next[key];
    const ok = await persist(next);
    if (ok) toast.success(t("play.gameEnv.saved"), key);
  };

  const entries = Object.entries(env);

  return (
    <ConfirmModal
      strTitle={t("play.gameEnv.title", { game: gameTitle })}
      bAlertDialog
      strOKButtonText={t("common.close")}
      onOK={closeModal}
      onCancel={closeModal}
    >
      <div style={{ marginBottom: 8, opacity: 0.8, fontSize: "0.9em" }}>
        {t("play.gameEnv.subtitle")}
      </div>

      {status.loading && <div>{t("common.loading")}</div>}

      {!status.loading && entries.length === 0 && (
        <div style={{ opacity: 0.7, marginBottom: 8 }}>
          {t("play.gameEnv.empty")}
        </div>
      )}

      <Focusable style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {entries.map(([key, value]) => (
          <div key={key} style={rowStyle}>
            <span
              style={{
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                flex: 1,
                minWidth: 0,
                color: "#c7d5e0",
              }}
              title={`${key}=${value}`}
            >
              {key}
              <span
                style={{
                  opacity: 0.6,
                  marginInlineStart: 8,
                  fontSize: "0.85em",
                }}
              >
                ={value}
              </span>
            </span>
            <DialogButton
              disabled={working}
              onClick={() => void removeOne(key)}
              style={{ padding: "4px 8px", minWidth: 0, flex: "0 0 auto" }}
            >
              <FaTrash />
            </DialogButton>
          </div>
        ))}
      </Focusable>

      <Focusable
        style={{ display: "flex", gap: 8, marginTop: 12, alignItems: "center" }}
      >
        <TextField
          value={pendingKey}
          onChange={(e) => setPendingKey(e.target.value)}
          label={t("play.gameEnv.keyLabel")}
        />
        <TextField
          value={pendingValue}
          onChange={(e) => setPendingValue(e.target.value)}
          label={t("play.gameEnv.valueLabel")}
        />
        <DialogButton
          disabled={working || !pendingKey.trim()}
          onClick={() => void addOne()}
        >
          <FaPlus style={{ marginInlineEnd: 8 }} />
          {t("play.gameEnv.add")}
        </DialogButton>
      </Focusable>
    </ConfirmModal>
  );
};
