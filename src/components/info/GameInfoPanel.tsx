/**
 * GameInfoPanel — top-level container for the game info view.
 *
 * Mirrors staging's three-row layout exactly:
 *
 *   1. CompatRow — Deck-compat badge · Details / Synopsis /
 *      Install-Uninstall-Cancel buttons · genre tags.
 *   2. InfoRow — store icon + title + Size · Developer ·
 *      Publisher · Released · Metacritic, inline in a dark card.
 *   3. SynopsisSection — toggled by the Synopsis button.
 *   4. NavButtons — Store Page / DLC / Community / Points /
 *      Discussions / Guides / Support.
 *
 * No header strip, no cover, no in-panel view-mode toggle: the
 * native Steam header above the panel already shows the hero
 * image and title, and the view-mode toggle lives in the QAM
 * settings tab ({@link GameDetailsViewModeToggle}). When the QAM
 * toggle is set to `compact`, the panel returns null entirely
 * (matches staging's "simple" semantics).
 */
import { FC, useState } from "react";
import { Focusable } from "@decky/ui";
import { useGameInfo } from "../../hooks/useGameInfo";
import { useGameMetadata } from "../../hooks/useGameMetadata";
import { useViewMode } from "../../hooks/useViewMode";
import { GameInfoCompatRow } from "./GameInfoCompatRow";
import { GameInfoInfoRow } from "./GameInfoInfoRow";
import { GameInfoSynopsisSection } from "./GameInfoSynopsisSection";
import { GameInfoNavButtons } from "./GameInfoNavButtons";
import { GameAchievementsSummary } from "./GameAchievementsSummary";

interface Props {
  appId: number;
}

/** Focus-state CSS shared by every interactive element in the
 *  panel. Injected once at panel mount because the panel is
 *  rendered into Steam's spliced React tree — there is no app-
 *  level <head> we can collocate this with. */
const PANEL_FOCUS_CSS = `
@keyframes unifideck-focus-breathe {
  0%, 100% { box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.35); }
  50%      { box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.7); }
}
.unifideck-nav-button.gpfocus,
.unifideck-nav-button:hover {
  animation: unifideck-focus-breathe 1.5s ease-in-out infinite;
}
.unifideck-install-button.install-state   { background-color: #1a9fff; }
.unifideck-install-button.uninstall-state { background-color: #d32f2f; }
.unifideck-install-button.cancel-state    { background-color: #d32f2f; }
.unifideck-game-info-row.gpfocus,
.unifideck-synopsis-section.gpfocus {
  animation: unifideck-focus-breathe 1.5s ease-in-out infinite;
}
`;

export const GameInfoPanel: FC<Props> = ({ appId }) => {
  const { data: game } = useGameInfo(appId);
  const { data: meta } = useGameMetadata(appId);
  const { mode } = useViewMode();
  const [synopsisOpen, setSynopsisOpen] = useState(false);

  // QAM-driven hide: matches staging's "simple" mode (panel absent).
  if (mode === "compact") return null;

  // Wait for both fetches before rendering anything — staging's
  // panel has no transitional / spinner / error UI, and a partial
  // render would flash the wrong layout.
  if (!game || !meta) return null;

  return (
    <>
      <style>{PANEL_FOCUS_CSS}</style>
      <Focusable
        flow-children="column"
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 16,
          padding: 12,
        }}
      >
        <GameInfoCompatRow
          appId={appId}
          meta={meta}
          synopsisOpen={synopsisOpen}
          onToggleSynopsis={() => setSynopsisOpen((v) => !v)}
        />
        <GameInfoInfoRow appId={appId} game={game} meta={meta} />
        <GameAchievementsSummary
          store={game.store}
          gameId={game.id}
          title={game.title}
        />
        {synopsisOpen && meta.description && (
          <GameInfoSynopsisSection description={meta.description} />
        )}
        <GameInfoNavButtons meta={meta} />
      </Focusable>
    </>
  );
};
