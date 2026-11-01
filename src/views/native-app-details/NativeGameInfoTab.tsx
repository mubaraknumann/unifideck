/**
 * NativeGameInfoTab: the content of our Game info tab.
 *
 * Steam's `GameInfoContainer` with the about block, the GOG achievement
 * summary, the quick links (Store Page, Community Hub, …, Support) in
 * Steam's own link markup, and Steam's own "Found in these collections". The links are the same list the legacy panel
 * shows (`gameLinks`).
 *
 * On mount it also nudges the page's scroll container once. Steam decides
 * whether its tab header is "under the page header" from one measurement,
 * re-checked only on scroll; measured before the hero art lays out, the
 * header latched over the play bar (seen live 2026-11-02, fixed by one
 * scroll event).
 */
import { FC, useEffect, useRef } from "react";
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

/** Dispatch one scroll event on the nearest scrolling ancestor. */
function nudgeScrollContainer(from: HTMLElement): void {
  const view = from.ownerDocument.defaultView;
  for (let el = from.parentElement; el; el = el.parentElement) {
    const overflow = view?.getComputedStyle(el).overflowY;
    if (
      (overflow === "auto" || overflow === "scroll") &&
      el.scrollHeight > el.clientHeight
    ) {
      el.dispatchEvent(new Event("scroll"));
      return;
    }
  }
}

export const NativeGameInfoTab: FC<Props> = ({ appId, overview }) => {
  const { data: game } = useGameInfo(appId);
  const { data: meta } = useGameMetadata(appId);
  const ref = useRef<HTMLDivElement>(null);
  const ready = !!game && !!meta;

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const timer = window.setTimeout(() => nudgeScrollContainer(el), 250);
    return () => window.clearTimeout(timer);
  }, [ready]);

  const { details } = getNativeClasses();
  return (
    <div ref={ref}>
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
          {game.store === "gog" && (
            <GameAchievementsSummary
              store={game.store}
              gameId={game.id}
              title={game.title}
            />
          )}
          <QuickLinks meta={meta} />
          <NativeCollections appId={appId} />
        </Focusable>
      )}
    </div>
  );
};
