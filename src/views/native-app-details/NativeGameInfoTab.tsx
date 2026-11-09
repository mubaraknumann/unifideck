/**
 * NativeGameInfoTab: the content of our Game info tab.
 *
 * Steam's `GameInfoContainer` with the about block, the last-session
 * achievement summary, the quick links (Store Page, Community Hub, …, Support) in
 * Steam's own link markup, and Steam's own "Found in these collections".
 * The links are the same list the legacy panel shows (`gameLinks`).
 *
 * `HeaderLatchNudge` keeps Steam's tab header from latching over the play
 * bar when this tab is the one the page opens on.
 */
import { FC } from "react";
import { Focusable } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useGameInfo } from "../../hooks/useGameInfo";
import { useGameMetadata } from "../../hooks/useGameMetadata";
import {
  cx,
  getNativeClasses,
} from "../../lib/steam-bridge/native-app-details";
import { GameAchievementsSummary } from "../../components/info/GameAchievementsSummary";
import { gameLinks, openUrl } from "../../components/info/GameInfoNavButtons";
import { NativeGameInfoAbout } from "./NativeGameInfoAbout";
import { HeaderLatchNudge } from "./HeaderLatchNudge";
import { NativeCollections } from "./NativeCollections";
import type { GameMetadata } from "../../types/api";

interface Props {
  appId: number;
  overview: object;
}

const QuickLinks: FC<{ meta: GameMetadata }> = ({ meta }) => {
  const { t } = useTranslation();
  const { details, links } = getNativeClasses();
  return (
    <Focusable
      className={details.GameInfoQuickLinks}
      flow-children="row"
      style={{ display: "flex", flexWrap: "wrap", gap: 8 }}
    >
      {gameLinks(meta).map((link) => (
        <Focusable
          key={link.key}
          className={links.Anchor}
          onActivate={() => openUrl(link.url)}
          onClick={() => openUrl(link.url)}
        >
          <div className={links.Link}>
            <span className={links.Text}>
              {t(`gameInfoPanel.buttons.${link.labelKey}`)}
            </span>
          </div>
        </Focusable>
      ))}
    </Focusable>
  );
};

export const NativeGameInfoTab: FC<Props> = ({ appId, overview }) => {
  const { data: game } = useGameInfo(appId);
  const { data: meta } = useGameMetadata(appId);
  const ready = !!game && !!meta;

  const { details } = getNativeClasses();
  return (
    <div>
      {ready && <HeaderLatchNudge />}
      {ready && (
        <Focusable
          className={cx(details.GameInfoContainer)}
          flow-children="column"
        >
          <NativeGameInfoAbout
            appId={appId}
            overview={overview}
            game={game}
            meta={meta}
          />
          <GameAchievementsSummary
            store={game.store}
            gameId={game.id}
            title={game.title}
          />
          <QuickLinks meta={meta} />
          <NativeCollections appId={appId} />
        </Focusable>
      )}
    </div>
  );
};
