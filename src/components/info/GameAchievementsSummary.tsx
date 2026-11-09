/**
 * GameAchievementsSummary — "last session" achievements row.
 *
 * Rendered on the game info tab for every store with achievements
 * (`supports_achievements`; nothing for the others). Reads the achievement
 * watcher's persisted last-session summary via {@link useLastSessionAchievements}
 * (a fast, network-free RPC — off the get_game_info hot path) and, when the
 * last play session unlocked anything, shows a one-line recap that opens the
 * full {@link GameAchievementsModal} on activate. Renders nothing otherwise.
 */
import { FC, useCallback } from "react";
import { Focusable, showModal } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaTrophy } from "react-icons/fa";
import { useLastSessionAchievements } from "../../hooks/useAchievements";
import { useStoreCapability } from "../../hooks/useStoreCapability";
import { GameAchievementsModal } from "./GameAchievementsModal";

interface Props {
  store: string;
  gameId: string;
  title?: string;
}

export const GameAchievementsSummary: FC<Props> = ({
  store,
  gameId,
  title,
}) => {
  const { t } = useTranslation();
  const supported = useStoreCapability(store, "supports_achievements");
  const summary = useLastSessionAchievements(store, gameId, supported);

  const open = useCallback(() => {
    showModal(
      <GameAchievementsModal
        store={store}
        gameId={gameId}
        title={title}
        closeModal={() => {}}
      />,
    );
  }, [store, gameId, title]);

  if (!supported || !summary || summary.unlocked <= 0) return null;

  const shown = summary.names.slice(0, 3).filter(Boolean);
  const extra = summary.unlocked - shown.length;
  const names = shown.join(", ") + (extra > 0 ? ` (+${extra})` : "");

  return (
    <Focusable
      className="unifideck-nav-button"
      onActivate={open}
      onFocus={(e: React.FocusEvent<HTMLDivElement>) =>
        e.currentTarget.scrollIntoView({ behavior: "smooth", block: "nearest" })
      }
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "8px 12px",
        borderRadius: 6,
        background: "rgba(0,0,0,0.25)",
        fontSize: 13,
        color: "#c7d5e0",
      }}
    >
      <FaTrophy style={{ color: "#ffc82c", flexShrink: 0 }} />
      <span>{t("gameInfoPanel.lastSession", { names })}</span>
    </Focusable>
  );
};
