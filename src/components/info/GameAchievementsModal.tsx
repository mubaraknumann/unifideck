/**
 * GameAchievementsModal — a game's achievements list + unlock status.
 *
 * Opened from the game page for every store with achievements (GOG, Epic,
 * Xbox). Shows a progress header (unlocked / total + bar) and a scrollable
 * grid of achievement tiles — unlocked ones in colour with their unlock date,
 * locked ones dimmed (hidden-locked ones masked). Data comes from
 * {@link useAchievements} (the `get_game_achievements` RPC); a manual Refresh
 * bypasses the backend cache. Loading / empty / error states are keyed on the
 * backend error code (carried as the RpcError message).
 */
import { FC } from "react";
import { ConfirmModal, DialogButton, Focusable } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaLock, FaSyncAlt, FaTrophy } from "react-icons/fa";
import { useAchievements } from "../../hooks/useAchievements";
import { isRareAchievement, rarityPercent } from "../../lib/achievement-rarity";
import { steamText } from "../../lib/steam-bridge/native-app-details";
import type { Achievement } from "../../types/api";

interface Props {
  store: string;
  gameId: string;
  title?: string;
  closeModal: () => void;
}

/** Map a backend RpcError code (carried as the error message) to a body key. */
function errorKey(code: string | undefined): string {
  switch (code) {
    case "not_authed":
    case "auth_expired":
      return "achievementsModal.errorAuth";
    case "offline":
      return "achievementsModal.errorOffline";
    case "no_client_id":
    case "achievements_unsupported":
      return "achievementsModal.errorUnavailable";
    default:
      return "achievementsModal.error";
  }
}

const TILE_BASE: React.CSSProperties = {
  display: "flex",
  gap: 10,
  padding: 8,
  borderRadius: 6,
  background: "rgba(0,0,0,0.25)",
  alignItems: "center",
};

/** Steam's IconGlow shadow, measured from its stylesheet (2026-11-02). */
const RARE_GLOW =
  "rgba(255, 184, 78, 0.6) 0 0 2px 1px, rgba(255, 184, 78, 0.4) 0 0 16px 1px";

const AchievementTile: FC<{ ach: Achievement }> = ({ ach }) => {
  const { t } = useTranslation();
  const maskedHidden = ach.hidden && !ach.unlocked;
  const name = maskedHidden ? t("achievementsModal.hidden") : ach.name;
  const img = ach.unlocked ? ach.image_unlocked : ach.image_locked;
  const pct = rarityPercent(ach);
  return (
    <Focusable
      // A Focusable leaf (onActivate makes it a real focus target, not a
      // pass-through) so the controller can step through every achievement;
      // each focus scrolls the row into the modal's scroll viewport.
      onActivate={() => {}}
      onFocus={(e: React.FocusEvent<HTMLDivElement>) =>
        e.currentTarget.scrollIntoView({ block: "nearest" })
      }
      style={{ ...TILE_BASE, opacity: ach.unlocked ? 1 : 0.55 }}
    >
      <div style={{ position: "relative", flexShrink: 0 }}>
        {img ? (
          <img
            src={img}
            alt=""
            style={{
              width: 48,
              height: 48,
              borderRadius: 4,
              filter: ach.unlocked ? "none" : "grayscale(1)",
              // Steam's rare-achievement glow (its IconGlow shadow).
              ...(isRareAchievement(ach) ? { boxShadow: RARE_GLOW } : {}),
            }}
          />
        ) : (
          <div
            style={{
              width: 48,
              height: 48,
              borderRadius: 4,
              background: "#2a2e33",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <FaTrophy style={{ color: ach.unlocked ? "#ffc82c" : "#5a6066" }} />
          </div>
        )}
        {!ach.unlocked && (
          <FaLock
            style={{
              position: "absolute",
              right: -2,
              bottom: -2,
              color: "#8f98a0",
              fontSize: 12,
            }}
          />
        )}
      </div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: ach.unlocked ? "#ffffff" : "#acb2b8",
          }}
        >
          {name}
        </div>
        {!maskedHidden && ach.description && (
          <div
            style={{
              fontSize: 11,
              color: "#8f98a0",
              overflow: "hidden",
              textOverflow: "ellipsis",
              display: "-webkit-box",
              WebkitLineClamp: 2,
              WebkitBoxOrient: "vertical",
            }}
          >
            {ach.description}
          </div>
        )}
        {ach.unlocked && ach.unlocked_at && (
          <div style={{ fontSize: 11, color: "#59bf40", marginTop: 2 }}>
            {t("achievementsModal.unlockedOn", {
              date: new Date(ach.unlocked_at * 1000).toLocaleDateString(),
            })}
          </div>
        )}
        {pct != null && (
          <div
            style={{
              fontSize: 11,
              color: isRareAchievement(ach) ? "#ffb84e" : "#8f98a0",
            }}
          >
            {steamText("#AppDetails_PctUnlocked", `${pct}%`, pct)}
          </div>
        )}
      </div>
      {ach.gamerscore != null && (
        // Xbox's unit, written as Xbox writes it in every language.
        <div style={{ fontSize: 11, color: "#acb2b8", flexShrink: 0 }}>
          {ach.gamerscore} G
        </div>
      )}
    </Focusable>
  );
};

export const GameAchievementsModal: FC<Props> = ({
  store,
  gameId,
  title,
  closeModal,
}) => {
  const { t } = useTranslation();
  const { data, loading, error, refresh } = useAchievements(store, gameId);

  const percent = data ? Math.round(data.percent) : 0;

  return (
    <ConfirmModal
      strTitle={title || t("achievementsModal.title")}
      strDescription=""
      bHideCloseIcon={false}
      onOK={closeModal}
      onCancel={closeModal}
      strOKButtonText={t("achievementsModal.close")}
    >
      {/* flow-children="column" forces gamepad navigation to follow DOM/
          visual order (Refresh → tiles top-to-bottom → footer) instead of
          Decky's default spatial nav, which mis-orders the right-aligned
          Refresh button against the full-width list. */}
      <Focusable flow-children="column" style={{ padding: "6px 0" }}>
        {data && data.total > 0 && (
          <div style={{ marginBottom: 12 }}>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                marginBottom: 6,
              }}
            >
              <span style={{ fontSize: 14, fontWeight: 600, color: "#c7d5e0" }}>
                {t("achievementsModal.progress", {
                  unlocked: data.unlocked,
                  total: data.total,
                  percent,
                })}
              </span>
              <DialogButton
                style={{
                  width: "auto",
                  flex: "0 0 auto",
                  minWidth: 0,
                  padding: "4px 12px",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                }}
                disabled={loading}
                onClick={() => void refresh()}
              >
                <FaSyncAlt />
                {t("achievementsModal.refresh")}
              </DialogButton>
            </div>
            <div
              style={{
                height: 6,
                borderRadius: 3,
                background: "rgba(255,255,255,0.12)",
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  width: `${percent}%`,
                  height: "100%",
                  background: "#59bf40",
                }}
              />
            </div>
          </div>
        )}

        {loading && !data && (
          <div style={{ color: "#8f98a0", fontSize: 13, padding: "12px 0" }}>
            {t("achievementsModal.loading")}
          </div>
        )}

        {error && (
          <div style={{ color: "#ffc82c", fontSize: 13, padding: "12px 0" }}>
            {t(errorKey(error.message))}
          </div>
        )}

        {data && data.total === 0 && !loading && (
          <div
            style={{
              color: "#8f98a0",
              fontSize: 13,
              fontStyle: "italic",
              padding: "12px 0",
            }}
          >
            {t("achievementsModal.empty")}
          </div>
        )}

        {data && data.total > 0 && (
          <Focusable
            flow-children="column"
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 6,
              maxHeight: 360,
              overflowY: "auto",
            }}
          >
            {data.achievements.map((ach) => (
              <AchievementTile key={ach.key} ach={ach} />
            ))}
          </Focusable>
        )}
      </Focusable>
    </ConfirmModal>
  );
};
