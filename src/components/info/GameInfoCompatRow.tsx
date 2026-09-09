/**
 * GameInfoCompatRow — top action row of the game info panel.
 *
 * Renders the compatibility badge for the running device, the Details and
 * Synopsis toggle buttons, the Install / Uninstall / Cancel
 * action button (live progress when downloading), and the
 * comma-dot-separated genre tags underneath.
 *
 * The action button drives:
 *  - install via {@link useInstallFlow} (handles GOG language picker);
 *  - uninstall via {@link useGameActions} + {@link UninstallConfirmModal};
 *  - cancel via {@link useGameActions} + a {@link ConfirmModal} dialog.
 *
 * Live progress comes from the {@link useDownloads} queue snapshot
 * — no per-second polling.
 */
import { FC, useCallback, useMemo } from "react";
import { DialogButton, Focusable, showModal } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useGameInfo } from "../../hooks/useGameInfo";
import { useGameActions } from "../../hooks/useGameActions";
import { useDownloads } from "../../contexts/DownloadContext";
import { useToast } from "../../hooks/useToast";
import { SteamBridge } from "../../lib/steam-bridge";
import { UninstallConfirmModal } from "../modals/UninstallConfirmModal";
import { GameInfoDetailsModal } from "./GameInfoDetailsModal";
import { CompatBadge, asCategory } from "./CompatBadge";
import { GameAchievementsModal } from "./GameAchievementsModal";
import { useStoreCapability } from "../../hooks/useStoreCapability";
import type { GameMetadata } from "../../types/api";

interface Props {
  appId: number;
  meta: GameMetadata;
  synopsisOpen: boolean;
  onToggleSynopsis: () => void;
  bridge?: SteamBridge;
}

/** Inline-flex sizing for every DialogButton in the panel.
 *  @decky/ui's DialogButton defaults to ``width: 100%`` (settings-
 *  menu styling) — without ``flex: 0 0 auto`` the buttons stretch
 *  to fill their parent flex line. */
const buttonStyle = {
  padding: "4px 12px",
  fontSize: 12,
  minWidth: 0,
  width: "auto",
  flex: "0 0 auto",
  display: "inline-flex",
  alignItems: "center",
} as const;

const defaultBridge = new SteamBridge();

export const GameInfoCompatRow: FC<Props> = ({
  appId,
  meta,
  synopsisOpen,
  onToggleSynopsis,
  bridge = defaultBridge,
}) => {
  const { t } = useTranslation();
  const { data: game } = useGameInfo(appId);
  const downloads = useDownloads();
  const actions = useGameActions(bridge);
  const toast = useToast();
  // Was `game?.store === "gog" || game?.store === "epic"` inline — the exact
  // twin of `_ACHIEVEMENT_STORES` in rpc/mixins/achievements.py with nothing
  // linking them (audit register item 31). Adding a store to one side only
  // was silent in both directions: no button, or a button that raises
  // `achievements_unsupported`.
  const supportsAchievements = useStoreCapability(
    game?.store,
    "supports_achievements",
  );

  const activeDownload = useMemo(() => {
    if (!game || !downloads.queue) return null;
    const all = [downloads.queue.current, ...downloads.queue.queued];
    return (
      all.find(
        (d) => d != null && d.store === game.store && d.game_id === game.id,
      ) ?? null
    );
  }, [downloads.queue, game]);

  const isDownloading = activeDownload != null;

  const showUninstallConfirm = useCallback(() => {
    if (!game) return;
    showModal(
      <UninstallConfirmModal
        gameId={appId}
        gameTitle={game.title}
        onConfirm={async (deletePrefix) => {
          const r = await actions.uninstall(appId, deletePrefix);
          if (r?.success) toast.success(t("toasts.uninstallComplete"));
        }}
        closeModal={() => {}}
      />,
    );
  }, [actions, appId, game, t, toast]);

  const onDetails = useCallback(() => {
    showModal(<GameInfoDetailsModal meta={meta} closeModal={() => {}} />);
  }, [meta]);

  const onAchievements = useCallback(() => {
    if (!game) return;
    showModal(
      <GameAchievementsModal
        store={game.store}
        gameId={game.id}
        title={game.title}
        closeModal={() => {}}
      />,
    );
  }, [game]);

  // The compat row only exposes Uninstall — Install / Cancel are
  // already covered by the main play section directly above. Show
  // it only when the game is installed and not mid-(re)download.
  //
  // A third clause used to exclude Microsoft games tagged
  // `not_compatible`. No backend ever emitted that tag — it existed
  // only in this expression (audit §2.8's phantom-vocabulary class),
  // so the clause could never be false and the `as never` cast was
  // there because the tag is absent from `GameTag` too. `is_installed`
  // is the real gate: cloud titles are never installed.
  const showAction = !!game && Boolean(game.is_installed) && !isDownloading;

  const compatTrack = meta.compat_device;
  const compatCategory = asCategory(meta.compat?.[compatTrack]?.category);

  return (
    // "grid" rather than "row" — this Focusable lays out as a COLUMN and its
    // buttons sit in a wrapping inner div, so a single-logical-row hint was
    // never true here. See GameInfoNavButtons for why the container-level
    // scrollIntoView is gone.
    <Focusable
      flow-children="grid"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: 8,
        }}
      >
        <CompatBadge
          category={compatCategory}
          track={compatTrack}
          style={{
            padding: "4px 12px",
            fontWeight: 700,
            letterSpacing: 0.5,
          }}
        />
        <DialogButton
          className="unifideck-nav-button"
          style={buttonStyle}
          onClick={onDetails}
        >
          {t("gameInfoPanel.buttons.details")}
        </DialogButton>
        {meta.description && (
          <DialogButton
            className="unifideck-nav-button"
            style={{
              ...buttonStyle,
              ...(synopsisOpen
                ? { background: "#1a9fff", color: "#ffffff" }
                : null),
            }}
            onClick={onToggleSynopsis}
          >
            {t("gameInfoPanel.buttons.synopsis")}
          </DialogButton>
        )}
        {supportsAchievements && (
          <DialogButton
            className="unifideck-nav-button"
            style={buttonStyle}
            onClick={onAchievements}
          >
            {t("gameInfoPanel.buttons.achievements")}
          </DialogButton>
        )}
        {showAction && (
          <DialogButton
            className="unifideck-nav-button unifideck-install-button uninstall-state"
            style={buttonStyle}
            disabled={actions.isWorking}
            onClick={showUninstallConfirm}
          >
            {t("gameInfoPanel.buttons.uninstall")}
          </DialogButton>
        )}
      </div>
      {meta.genres.length > 0 && (
        <div style={{ color: "#8f98a0", fontSize: 13 }}>
          {meta.genres.join(" • ")}
        </div>
      )}
    </Focusable>
  );
};
