/**
 * NativeAchievements: the Achievements section at the top of Your stuff.
 *
 * Steam's Your stuff opens with "Achievements": a highlight row
 * ("You've unlocked 3/44 (7%)" + bar) and a carousel of achievement icons
 * in which the focused one expands to name, description and rarity. For a
 * shortcut Steam has no achievements. This draws that section with
 * Steam's own section component and carousel markup, from OUR store's
 * achievements (`get_game_achievements`). Activating a tile opens our full
 * achievements view, as Steam's tile opens Steam's.
 *
 * Steam's words come from Steam's own localization (`steamText`); the
 * numbers are ours.
 */
import { FC, useState } from "react";
import { DialogButton, Focusable, showModal } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useAchievements } from "../../hooks/useAchievements";
import { useGameInfo } from "../../hooks/useGameInfo";
import { useStoreCapability } from "../../hooks/useStoreCapability";
import {
  cx,
  getAppDetailsSection,
  getNativeClasses,
  steamText,
} from "../../lib/steam-bridge/native-app-details";
import { GameAchievementsModal } from "../../components/info/GameAchievementsModal";
import type { Achievement, Game } from "../../types/api";

/** Measured on Steam's own bar (2026-11-02): these two colours come from
 *  generic utility classes that cannot be found by name. */
const BAR_TRACK = "rgb(6, 10, 14)";
const BAR_FILL = "rgb(26, 159, 255)";
/** Same source: the bar's height sits on Steam's inner utility element. */
const BAR_HEIGHT = 10;

function openAll(game: Game): void {
  showModal(
    <GameAchievementsModal
      store={game.store}
      gameId={game.id}
      title={game.title}
      closeModal={() => {}}
    />,
  );
}

const Tile: FC<{
  ach: Achievement;
  detailed: boolean;
  onFocus: () => void;
  onActivate: () => void;
}> = ({ ach, detailed, onFocus, onActivate }) => {
  const { t } = useTranslation();
  const { achievements: a, achievementIcon: i } = getNativeClasses();
  const masked = ach.hidden && !ach.unlocked;
  const name = masked ? t("achievementsModal.hidden") : ach.name;
  const description = masked ? "" : ach.description;
  const img = ach.unlocked ? ach.image_unlocked : ach.image_locked;
  let footer: string | null = null;
  if (ach.unlocked && ach.unlocked_at) {
    const date = new Date(ach.unlocked_at * 1000).toLocaleDateString();
    footer = steamText(
      "#AppDetails_Achievements_Unlocked",
      t("achievementsModal.unlockedOn", { date }),
      date,
    );
  } else if (ach.rarity != null) {
    const pct = Math.round(ach.rarity * 10) / 10;
    footer = steamText("#AppDetails_PctUnlocked", `${pct}%`, pct);
  }
  return (
    <Focusable
      className={cx(a.AchievementCarouselItem, detailed && a.Detailed)}
      onFocus={(e: React.FocusEvent<HTMLDivElement>) => {
        onFocus();
        e.currentTarget.scrollIntoView({ block: "nearest", inline: "nearest" });
      }}
      onActivate={onActivate}
      onClick={onActivate}
      aria-label={detailed ? undefined : name}
    >
      <div
        className={cx(
          i.AchievementIconWrapper,
          a.CarouselIcon,
          a.Prioritized,
          i.RareAchievementNoAnimation,
        )}
      >
        {img && <img className={i.Icon} src={img} alt="" />}
      </div>
      {detailed && (
        <div className={a.AchivementCarouselItemDetails}>
          <div className={a.Name}>{name}</div>
          <div className={a.Description}>{description}</div>
          {footer && <div className={a.Achieved}>{footer}</div>}
        </div>
      )}
    </Focusable>
  );
};

/** One row of tiles; the focused tile shows its details. */
const Carousel: FC<{ items: Achievement[]; game: Game }> = ({
  items,
  game,
}) => {
  const [focused, setFocused] = useState(0);
  return (
    <Focusable
      flow-children="row"
      style={{
        display: "flex",
        gap: 27,
        overflow: "hidden",
        padding: "0 0 4px",
      }}
    >
      {items.map((ach, idx) => (
        <Tile
          key={ach.key}
          ach={ach}
          detailed={idx === focused}
          onFocus={() => setFocused(idx)}
          onActivate={() => openAll(game)}
        />
      ))}
    </Focusable>
  );
};

const Section: FC<{ game: Game }> = ({ game }) => {
  const { t } = useTranslation();
  const { data } = useAchievements(game.store, game.id);
  const SteamSection = getAppDetailsSection();
  if (!SteamSection || !data || data.total <= 0) return null;
  const { section: s, achievements: a, detailsButton: b } = getNativeClasses();
  const pct = Math.round(data.percent);
  const unlocked = data.achievements.filter((x) => x.unlocked);
  const locked = data.achievements.filter((x) => !x.unlocked);

  const highlight = (
    <div className={cx(s.Highlight, a.HighlightDiv)}>
      <div className={a.UnlockedLabel}>
        <span>
          {steamText(
            "#AppDetails_PlayerUnlockedPercent",
            `${data.unlocked}/${data.total}`,
            data.unlocked,
            data.total,
          )}
        </span>{" "}
        <span className={a.UnlockedLabelPercent}>({pct}%)</span>
      </div>
      <div className={a.ProgressRow}>
        <div className={a.ProgressBar}>
          <div
            style={{
              height: BAR_HEIGHT,
              borderRadius: 8,
              overflow: "hidden",
              background: BAR_TRACK,
            }}
          >
            <div
              style={{ width: `${pct}%`, height: "100%", background: BAR_FILL }}
            />
          </div>
        </div>
      </div>
    </div>
  );

  return (
    <SteamSection
      label={steamText(
        "#AppDetails_SectionTitle_Achievements",
        t("achievementsModal.title"),
      )}
      highlight={highlight}
      rightColumnSection
      availableOffline
    >
      <div className={cx(s.Body, a.BasicAppDetailsAchievementsSectionBody)}>
        {unlocked.length > 0 && <Carousel items={unlocked} game={game} />}
        {locked.length > 0 && (
          <>
            <h3 className={a.LockedAchievementsLabel}>
              {steamText("#AppDetails_Achievements_Locked", "")}
            </h3>
            <Carousel items={locked} game={game} />
          </>
        )}
        <DialogButton
          className={cx(b.AppDetailsButton, b.BottomRight)}
          onClick={() => openAll(game)}
        >
          {steamText(
            "#AppDetails_ViewAllAchievements",
            t("gameInfoPanel.buttons.achievements"),
          )}
        </DialogButton>
      </div>
    </SteamSection>
  );
};

export const NativeAchievements: FC<{ appId: number }> = ({ appId }) => {
  const { data: game } = useGameInfo(appId);
  const supported = useStoreCapability(game?.store, "supports_achievements");
  return game && supported ? <Section game={game} /> : null;
};
