/**
 * CompanionExecutablesModal — attach extra programs to launch with a game.
 *
 * Opened from the "Companion executables…" item injected into the native
 * game context menu (see {@link file://./../../lib/steam-bridge/app-context-menu-patch.ts}),
 * right next to "Change executable…".
 *
 * This replaces relying on a third-party Decky plugin (e.g. CheatDeck) to
 * inject a trainer/cheat tool via Steam's launch options: each companion
 * here is launched by the backend in the SAME Proton prefix/env as the main
 * game (see ``launcher.proton.infrastructure.companions``), so it needs no
 * ``%command%`` editing and can never break the ``store:game_id`` argv the
 * launcher dispatcher depends on. A companion failing to start never blocks
 * or fails the game itself.
 */
import { FC, useState } from "react";
import { ConfirmModal, DialogButton, Focusable, TextField } from "@decky/ui";
import { openFilePicker, FileSelectionType } from "@decky/api";
import { useTranslation } from "react-i18next";
import { FaPlus, FaTrash } from "react-icons/fa";
import { rpcRoutes } from "../../api/rpc-routes";
import { useRPCQuery, useRPCMutation } from "../../api/useRPC";
import { useToast } from "../../hooks/useToast";

export interface CompanionEntry {
  path: string;
  delay_seconds: number;
}

interface ListResult {
  companions: CompanionEntry[];
}

interface MutateResult {
  success?: boolean;
  companions?: CompanionEntry[];
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
  // minWidth: 0 is load-bearing: without it, a flex item with
  // `overflow: hidden` + `text-overflow: ellipsis` refuses to shrink below
  // its own intrinsic (unwrapped) text width in this CEF build — a long
  // trainer filename then overflows the row's actual pixel width and gets
  // clipped by ConfirmModal's own overflow-hidden body, rendering as
  // nothing at all while the fixed-size trash button (which never needs to
  // shrink) stays visible next to it. That is what looked like "just a
  // trash can, no filename" even after the text got an explicit color.
  minWidth: 0,
} as const;

function basename(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

export const CompanionExecutablesModal: FC<Props> = ({
  store,
  gameId,
  gameTitle,
  closeModal,
}) => {
  const { t } = useTranslation();
  const toast = useToast();

  const list = useRPCQuery<[string, string], ListResult>(
    rpcRoutes.listCompanionExecutables,
    [store, gameId],
  );
  const addMutation = useRPCMutation<
    [string, string, string, number],
    MutateResult
  >(rpcRoutes.addCompanionExecutable);
  const removeMutation = useRPCMutation<[string, string, string], MutateResult>(
    rpcRoutes.removeCompanionExecutable,
  );
  const [busy, setBusy] = useState(false);
  const [pendingDelay, setPendingDelay] = useState("0");

  const companions = list.data?.companions ?? [];
  const working = busy || addMutation.loading || removeMutation.loading;

  const addOne = async () => {
    let delaySeconds = 0;
    const parsed = Number(pendingDelay);
    if (Number.isFinite(parsed) && parsed >= 0) delaySeconds = parsed;

    let picked: { realpath?: string; path?: string } | undefined;
    try {
      // See ChangeExecutableModal for why `filter` must stay undefined
      // (a RegExp can't cross the JS→Python RPC bridge). Companions
      // aren't restricted to any install dir, so no base path is passed.
      picked = await openFilePicker(
        FileSelectionType.FILE,
        "/home/deck",
        true,
        true,
        undefined,
        ["exe"],
        false,
        true,
      );
    } catch {
      return; // user cancelled the picker
    }
    const abs = picked?.realpath || picked?.path;
    if (!abs) return;

    setBusy(true);
    try {
      const res = await addMutation.mutate(store, gameId, abs, delaySeconds);
      if (res && res.success !== false) {
        toast.success(t("play.companions.added"), basename(abs));
        setPendingDelay("0");
        await list.refetch();
      } else {
        toast.error(t("play.companions.addFailed"), basename(abs));
      }
    } catch {
      toast.error(t("play.companions.addFailed"), basename(abs));
    } finally {
      setBusy(false);
    }
  };

  const removeOne = async (path: string) => {
    setBusy(true);
    try {
      const res = await removeMutation.mutate(store, gameId, path);
      if (res && res.success !== false) {
        toast.success(t("play.companions.removed"), basename(path));
        await list.refetch();
      } else {
        toast.error(t("play.companions.removeFailed"), basename(path));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <ConfirmModal
      strTitle={t("play.companions.title", { game: gameTitle })}
      bAlertDialog
      strOKButtonText={t("common.close")}
      onOK={closeModal}
      onCancel={closeModal}
    >
      <div style={{ marginBottom: 8, opacity: 0.8, fontSize: "0.9em" }}>
        {t("play.companions.subtitle")}
      </div>
      <div style={{ marginBottom: 8, opacity: 0.6, fontSize: "0.8em" }}>
        {t("play.companions.delayHint")}
      </div>

      {list.loading && <div>{t("common.loading")}</div>}

      {!list.loading && companions.length === 0 && (
        <div style={{ opacity: 0.7, marginBottom: 8 }}>
          {t("play.companions.empty")}
        </div>
      )}

      <Focusable style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {companions.map((c) => (
          <div key={c.path} style={rowStyle}>
            <span
              style={{
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                flex: 1,
                minWidth: 0,
                // Explicit color: a plain <span> inside ConfirmModal's body
                // has no guaranteed text color (unlike DialogButton content,
                // which brings its own), so without this the filename was
                // invisible while the trash-icon button next to it rendered
                // fine.
                color: "#c7d5e0",
              }}
              title={c.path}
            >
              {basename(c.path)}
              {c.delay_seconds > 0 && (
                <span
                  style={{
                    opacity: 0.6,
                    marginInlineStart: 8,
                    fontSize: "0.8em",
                  }}
                >
                  {t("play.companions.delaySeconds", {
                    seconds: c.delay_seconds,
                  })}
                </span>
              )}
            </span>
            <DialogButton
              disabled={working}
              onClick={() => void removeOne(c.path)}
              style={{
                padding: "4px 8px",
                minWidth: 0,
                // DialogButton defaults to flex-grow: 1 in this CEF build's
                // gamepad-dialog CSS. With BOTH the filename <span> (flex: 1)
                // and this button competing for space in a space-between
                // row, the button's flex-grow (plus its flex-shrink: 0
                // intrinsic-content behavior) won out and squeezed the
                // <span> down to a zero/near-zero rendered width — it was
                // never a color problem, the text was there but had no
                // room. Pinning the button to its own content size lets
                // the <span> actually claim the row's remaining space.
                flex: "0 0 auto",
              }}
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
          value={pendingDelay}
          onChange={(e) => setPendingDelay(e.target.value)}
          label={t("play.companions.delaySeconds", { seconds: "" })}
        />
        <DialogButton disabled={working} onClick={() => void addOne()}>
          <FaPlus style={{ marginInlineEnd: 8 }} />
          {t("play.companions.add")}
        </DialogButton>
      </Focusable>
    </ConfirmModal>
  );
};
