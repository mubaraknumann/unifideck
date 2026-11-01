/**
 * InstalledGameRow — one installed game in the Downloads tab's
 * "Installed" section.
 *
 * The section this belongs to exists because the tab used to list only
 * downloads *this plugin had performed in its capped history*, so a game
 * installed before the plugin — or eleven downloads ago — was invisible, and
 * the only way to make it appear was to uninstall and reinstall it. The list
 * is now derived from actual install state, so it always reflects the disk.
 *
 * Play and Uninstall both need a Steam appId, which we resolve from the
 * shortcut cache; a game with no shortcut yet (synced but not written) is
 * rendered without actions rather than hidden, so the list still matches the
 * user's library.
 */
import { FC, useMemo, useState } from "react";
import { DialogButton, showModal } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useGameActions } from "../../hooks/useGameActions";
import { useGameUpdate } from "../../hooks/useGameUpdate";
import { useToast } from "../../hooks/useToast";
import { downloadErrorToast } from "../../lib/download-errors";
import { toastText, headerText } from "../../services/toast-text";
import { SteamBridge } from "../../lib/steam-bridge";
import { resolveAppIdFromStoreGame } from "../../lib/library-filters";
import { StoreIcon } from "../shared/StoreIcon";
import { UninstallConfirmModal } from "../modals/UninstallConfirmModal";
import { UpdateAvailableModal } from "../modals/UpdateAvailableModal";
import { formatBytes } from "../../utils";
import type { Game } from "../../types/api";
import type { InstalledDiskInfo } from "../../types/downloads";

interface Props {
  game: Game;
  /** On-disk size + storage location, from the tab's bulk
   *  `get_installed_disk_info` query. Undefined while that query is still in
   *  flight, or for a game whose install directory couldn't be resolved —
   *  the meta line is then simply omitted. */
  disk?: InstalledDiskInfo;
  /** Called after a successful uninstall so the list can re-derive. */
  onUninstalled: () => void;
}

const bridge = new SteamBridge();

const ACTION_BTN_STYLE = {
  fontSize: 11,
  padding: "2px 10px",
  borderRadius: 3,
  fontWeight: 600,
  width: "auto",
  minWidth: 0,
  height: "auto",
  flex: "0 0 auto",
  border: "none",
} as const;

/** Dimmed sub-line under the title. ~10px is the smallest size that stays
 *  legible on a Deck screen at arm's length; the grey is Steam's own
 *  secondary-text tone so it reads as metadata, not as a second title. */
const META_STYLE = {
  fontSize: 10,
  lineHeight: 1.2,
  color: "#8b949e",
  marginTop: 2,
} as const;

export const InstalledGameRow: FC<Props> = ({ game, disk, onUninstalled }) => {
  const { t } = useTranslation();
  const actions = useGameActions(bridge);
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  // "12.4 GB · Internal". Each half is independently optional: an
  // unresolvable size comes back as 0 and an unstat-able install directory as
  // a null location, and showing "0 B" or a guessed location would be worse
  // than showing neither.
  const meta = useMemo(() => {
    if (!disk) return "";
    const parts: string[] = [];
    if (disk.size_bytes > 0) parts.push(formatBytes(disk.size_bytes));
    if (disk.location) {
      parts.push(
        disk.location === "internal"
          ? t("downloads.locationInternal")
          : t("downloads.locationExternal"),
      );
    }
    return parts.join(" · ");
  }, [disk, t]);

  // The shortcut cache is keyed by STORE_GAME_ID (see `updateUnifideckCache`),
  // which is not always the same string as `Game.id`.
  const appId = useMemo(
    () => resolveAppIdFromStoreGame(game.store, game.store_game_id),
    [game.store, game.store_game_id],
  );

  // Same source of truth as the App-Details Play section, so the two can
  // never disagree about whether a game is out of date.
  const hasUpdate = useGameUpdate(game.store, game.store_game_id);

  const queueUpdate = async () => {
    if (appId == null) return;
    setBusy(true);
    try {
      const result = await actions.update(
        appId,
        game.store,
        game.store_game_id,
      );
      if (result?.success) {
        toast.success(
          headerText("toasts.header.update"),
          toastText("toasts.updateQueued"),
        );
      } else {
        toast.error(
          headerText("toasts.header.update"),
          downloadErrorToast(result?.error),
        );
      }
    } finally {
      setBusy(false);
    }
  };

  // Mirrors InstalledButtons: ask before launching a stale build rather
  // than blocking it.
  const onPlay = () => {
    if (appId == null) return;
    if (!hasUpdate) {
      actions.launch(appId);
      return;
    }
    showModal(
      <UpdateAvailableModal
        gameTitle={game.title}
        onUpdate={queueUpdate}
        onPlayAnyway={() => actions.launch(appId)}
        closeModal={() => {}}
      />,
    );
  };

  const confirmUninstall = () => {
    if (appId == null) return;
    showModal(
      <UninstallConfirmModal
        gameId={appId}
        gameTitle={game.title}
        onConfirm={async (deletePrefix: boolean) => {
          setBusy(true);
          try {
            const result = await actions.uninstall(appId, deletePrefix);
            if (result.success) {
              onUninstalled();
            } else {
              // A refusal (an Xbox PC game still running, ...) must say so.
              toast.error(
                headerText("toasts.header.uninstall"),
                downloadErrorToast(result.error),
              );
            }
          } finally {
            setBusy(false);
          }
        }}
        closeModal={() => {}}
      />,
    );
  };

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: 6,
        minWidth: 0,
      }}
    >
      {/* flexShrink guard: without it the icon is squeezed by a long
          wrapping title (an svg is shrinkable in a flex row). */}
      <span style={{ display: "inline-flex", flexShrink: 0 }}>
        <StoreIcon store={game.store} size={14} />
      </span>
      {/* Title + meta share one column so the meta line sits UNDER the title
          rather than competing with it for width — the QAM panel is narrow
          enough that an inline "12.4 GB · Internal" would push most titles
          onto an extra line. A plain div, like the button wrapper below: only
          the DialogButtons should be nodes in the tab's nav grid. */}
      <div style={{ flex: 1, minWidth: 0 }}>
        {/* Wraps instead of truncating. The QAM panel is narrow and the two
            action buttons take a fixed slice of it, so `nowrap` + ellipsis cut
            most titles to a few characters ("Alex Kidd in …", "Beyond Goo…") —
            useless for telling two games apart. Letting the title use as many
            lines as it needs costs a little height and keeps every name
            readable; `break-word` handles a single token longer than the
            column. */}
        <div
          style={{
            fontWeight: 500,
            overflowWrap: "anywhere",
            wordBreak: "break-word",
            lineHeight: 1.3,
          }}
        >
          {game.title}
        </div>
        {meta !== "" && <div style={META_STYLE}>{meta}</div>}
      </div>
      {appId != null && (
        // A PLAIN div, not a Focusable: the whole Installed list is wrapped in
        // one `flow-children="grid"` container (see DownloadsTab), and a
        // per-row nav node would break that grid into isolated rows. Plain
        // divs are transparent to the nav tree, so every Play/Uninstall button
        // becomes a direct child of the one grid — which is what makes DOWN
        // from Uninstall land on the next Uninstall instead of jumping column.
        <div style={{ display: "flex", gap: 6, flex: "0 0 auto" }}>
          {/* Update REPLACES Play when one is pending — the same rule the
              App-Details Play section follows, so a game reads the same way
              wherever the user meets it. Blue rather than the Play green:
              at this size the label alone is easy to skim past. */}
          <DialogButton
            className={
              hasUpdate
                ? "unifideck-download-update-btn"
                : "unifideck-download-play-btn"
            }
            style={{
              ...ACTION_BTN_STYLE,
              background: hasUpdate ? "#3b82f6" : "#22c55e",
              color: hasUpdate ? "#f8fafc" : "#0f172a",
            }}
            disabled={busy || actions.isWorking}
            onClick={hasUpdate ? queueUpdate : onPlay}
          >
            {hasUpdate ? t("downloads.update") : t("downloads.play")}
          </DialogButton>
          <DialogButton
            className="unifideck-download-uninstall-btn"
            style={{ ...ACTION_BTN_STYLE }}
            disabled={busy || actions.isWorking}
            onClick={confirmUninstall}
          >
            {t("downloads.uninstall")}
          </DialogButton>
        </div>
      )}
    </div>
  );
};
