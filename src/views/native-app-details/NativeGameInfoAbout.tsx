/**
 * NativeGameInfoAbout: the top block of our Game info tab.
 *
 * Steam's own Game info tab shows, for a Steam game: box art, the short
 * description, Developer / Publisher / Release Date, a features list and a
 * Deck-compatibility row. For a shortcut it shows a "non-Steam game"
 * notice instead. This renders the same markup with Steam's classes, from
 * our metadata:
 *
 *   [box art]  description                      Steam's feature rows
 *              Developer: · Publisher: · Genres:     (icons)
 *              Released: · Store: · Size: · Metacritic:
 *   <Device> Compatibility  [Steam's icon] LABEL  Details
 */
import { FC, ReactNode, useEffect, useMemo, useState } from "react";
import { showModal } from "@decky/ui";
import { useTranslation } from "react-i18next";
import {
  AppAssetType,
  cx,
  getAppImage,
  fetchSteamTopTags,
  getFeatureIds,
  getFeatureRow,
  getNativeClasses,
  withOurFeatures,
  type OurFeatureFacts,
} from "../../lib/steam-bridge/native-app-details";
import { useGameSize } from "../../hooks/useGameSize";
import { useStoreCapability } from "../../hooks/useStoreCapability";
import { useAchievements } from "../../hooks/useAchievements";
import { formatBytes } from "../../components/play/PlayMeta";
import { StoreIcon } from "../../components/shared/StoreIcon";
import { asCategory } from "../../components/info/CompatBadge";
import { GameInfoDetailsModal } from "../../components/info/GameInfoDetailsModal";
import { NativeCompatRow } from "./NativeCompat";
import { STORE_VISUALS } from "../../types/store";
import type { Game, GameMetadata } from "../../types/api";

interface Props {
  appId: number;
  overview: object;
  game: Game;
  meta: GameMetadata;
}

/** Steam's "Label: value" association row. */
const Association: FC<{ label: string; children: ReactNode }> = ({
  label,
  children,
}) => {
  const c = getNativeClasses().about;
  return (
    <div className={c.AssociationList}>
      <div className={c.Label}>{label}</div>
      <div className={c.Association}>
        <span className={c.Name}>{children}</span>
      </div>
    </div>
  );
};

/**
 * Steam's own feature rows (icon + name), in Steam's order, answered from
 * our facts through `withOurFeatures`: Achievements and Cloud Saves from
 * our store, no Steam-only services. Nothing when Steam's row component
 * is not found on this build.
 */
const SteamFeatureRows: FC<{ overview: object; facts: OurFeatureFacts }> = ({
  overview,
  facts,
}) => {
  const Row = getFeatureRow();
  const ids = getFeatureIds();
  if (!Row || !ids) return null;
  const view = withOurFeatures(overview, facts);
  return (
    <>
      {ids.map((id) => (
        <Row
          key={id}
          overview={view}
          feature={id}
          minimode={false}
          suppresstooltip
        />
      ))}
    </>
  );
};

/** Entries in the Genres line, ours and Steam's together, at most. */
const GENRES_SHOWN = 5;

/**
 * Our genres, then the Steam store's own top tags for the matched Steam
 * app (most-voted first, in the user's Steam language) that we do not
 * already list, up to {@link GENRES_SHOWN} in all. Our genres show at
 * once; Steam's tags join when its store answers.
 */
function useGenres(meta: GameMetadata): string[] {
  const [steamTags, setSteamTags] = useState<string[]>([]);
  const steamAppId = meta.has_steam_store_page ? meta.steam_app_id : 0;
  useEffect(() => {
    if (steamAppId <= 0) return;
    let cancelled = false;
    void fetchSteamTopTags(steamAppId).then((tags) => {
      if (!cancelled) setSteamTags(tags);
    });
    return () => {
      cancelled = true;
    };
  }, [steamAppId]);
  return useMemo(() => {
    const seen = new Set(meta.genres.map((g) => g.toLowerCase()));
    const extra = steamTags.filter((tag) => !seen.has(tag.toLowerCase()));
    return [...meta.genres, ...extra].slice(0, GENRES_SHOWN);
  }, [meta.genres, steamTags]);
}

function metacriticColor(score: number): string {
  if (score >= 75) return "#66cc33";
  if (score >= 50) return "#ffcc33";
  return "#ff0000";
}

export const NativeGameInfoAbout: FC<Props> = ({
  appId,
  overview,
  game,
  meta,
}) => {
  const { t } = useTranslation();
  const { about: c, aboutFrame } = getNativeClasses();
  const AppImage = getAppImage();
  const size =
    useGameSize(appId, Boolean(game.is_installed)) ?? game.size_bytes;
  const supportsAchievements = useStoreCapability(
    game.store,
    "supports_achievements",
  );
  // The store supporting achievements is not this game having any (GOG's
  // Bastion has none). Ask for the game's own list, the same source the
  // Your stuff section uses; the row appears only once it says total > 0.
  const achievementList = useAchievements(
    game.store,
    game.id,
    supportsAchievements,
  );
  const hasAchievements =
    supportsAchievements && (achievementList.data?.total ?? 0) > 0;
  const genres = useGenres(meta);
  const track = meta.compat_device;
  const category = asCategory(meta.compat?.[track]?.category);
  const storeName = STORE_VISUALS[game.store]?.display_name ?? game.store;

  const openDetails = (): void => {
    showModal(<GameInfoDetailsModal meta={meta} closeModal={() => {}} />);
  };
  return (
    <div
      className={cx(
        aboutFrame.AppGameInfoContainer,
        aboutFrame.AppDetailsExpanded,
        aboutFrame.Glassy,
      )}
    >
      <div className={c.Container}>
        <div className={c.InnerContainer}>
          <div className={c.Portrait}>
            {AppImage && (
              <AppImage
                app={overview}
                eAssetType={AppAssetType.Portrait}
                className={c.BoxArt}
              />
            )}
          </div>
          {meta.description && (
            <div className={cx(c.Description, c.SectionContainer)}>
              <div className={c.GameDescription}>{meta.description}</div>
            </div>
          )}
          <div className={cx(c.Stats, c.SectionContainer)}>
            {meta.developer && (
              <Association label={t("gameInfoPanel.labels.developer")}>
                {meta.developer}
              </Association>
            )}
            {meta.publisher && (
              <Association label={t("gameInfoPanel.labels.publisher")}>
                {meta.publisher}
              </Association>
            )}
            {genres.length > 0 && (
              <Association label={t("gameInfoPanel.labels.genres")}>
                {genres.join(", ")}
              </Association>
            )}
            {meta.release_date && (
              <div className={c.Release}>
                <div className={c.Label}>
                  {t("gameInfoPanel.labels.released")}
                </div>
                <div className={c.Date}>{meta.release_date}</div>
              </div>
            )}
            <Association label={t("gameInfoPanel.labels.store")}>
              <span
                style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
              >
                <StoreIcon store={game.store} size={14} />
                {storeName}
              </span>
            </Association>
            {size != null && size > 0 && (
              <Association label={t("gameInfoPanel.labels.size")}>
                {formatBytes(size)}
              </Association>
            )}
            {meta.metacritic != null && (
              <Association label={t("gameInfoPanel.labels.metacritic")}>
                <span
                  style={{
                    padding: "0 6px",
                    borderRadius: 2,
                    background: metacriticColor(meta.metacritic),
                    color: "#000000",
                    fontWeight: 700,
                  }}
                >
                  {meta.metacritic}
                </span>
              </Association>
            )}
          </div>
          <div className={cx(c.FeaturesList, c.SectionContainer)}>
            <SteamFeatureRows
              overview={overview}
              facts={{
                achievements: hasAchievements,
                cloudSaves: meta.cloud_saves === true,
              }}
            />
          </div>
        </div>
        <NativeCompatRow
          track={track}
          category={category}
          onDetails={openDetails}
        />
      </div>
      <div className={aboutFrame.GameInfoShadow} />
    </div>
  );
};
