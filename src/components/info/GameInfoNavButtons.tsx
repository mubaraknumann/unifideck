/**
 * GameInfoNavButtons — Steam navigation + support buttons.
 *
 * Bottom row of the panel. Five Steam-hosted destinations are
 * gated on ``meta.has_steam_store_page`` because the URLs only
 * resolve for shortcuts that map to a real Steam Store entry —
 * showing them for a generic Epic / GOG / Amazon game would
 * just 404 in the Steam client.
 *
 * "Store Page" and "Support" are always shown: the former
 * falls back to the third-party ``meta.store_url`` and the
 * latter to a store-keyed support URL map.
 */
import { FC } from "react";
import { DialogButton, Focusable } from "@decky/ui";
import { useTranslation } from "react-i18next";
import type { GameMetadata, StoreId } from "../../types/api";

interface Props {
  meta: GameMetadata;
}

const SUPPORT_URLS: Record<string, string> = {
  epic: "https://www.epicgames.com/help/assistant",
  gog: "https://support.gog.com/hc/en-us?product=gog",
  amazon:
    "https://www.amazon.in/gp/help/customer/display.html?nodeId=GA5ZHN5T2JX8UGF7",
  ubisoft: "https://www.ubisoft.com/en-us/help",
  ea: "https://help.ea.com/en/",
  battlenet: "https://us.battle.net/support/en/",
  itch: "https://itch.io/support",
  microsoft:
    "https://support.xbox.com/en-US/help/games-apps/game-setup-and-play/troubleshoot-pc-games",
  steam: "https://help.steampowered.com",
};

export function openUrl(url: string): void {
  window.open(url, "_blank", "width=1024,height=768,popup=yes");
}

function getSupportUrl(store: StoreId, steamAppId: number): string {
  if (SUPPORT_URLS[store]) return SUPPORT_URLS[store];
  return steamAppId
    ? `https://help.steampowered.com/en/wizard/HelpWithGame/?appid=${steamAppId}`
    : SUPPORT_URLS.steam;
}

export interface GameLink {
  key: string;
  /** i18n key under `gameInfoPanel.buttons`. */
  labelKey: string;
  url: string;
}

/**
 * The links for a game, in display order. Steam-hosted ones only when the
 * shortcut maps to a real Steam Store page (they 404 otherwise). Shared by
 * this row and the native Game info tab.
 */
export function gameLinks(meta: GameMetadata): GameLink[] {
  const steamId = meta.steam_app_id;
  const hasSteam = meta.has_steam_store_page && steamId > 0;
  const storePageUrl = hasSteam ? `steam://store/${steamId}` : meta.store_url;
  const links: GameLink[] = [];
  if (storePageUrl) {
    links.push({ key: "store", labelKey: "storePage", url: storePageUrl });
  }
  if (hasSteam) {
    const store = "https://store.steampowered.com";
    const community = "https://steamcommunity.com/app";
    links.push(
      {
        key: "dlc",
        labelKey: "dlc",
        url: `steam://openurl/${store}/dlc/${steamId}`,
      },
      {
        key: "hub",
        labelKey: "communityHub",
        url: `steam://url/GameHub/${steamId}`,
      },
      {
        key: "points",
        labelKey: "pointsShop",
        url: `steam://openurl/${store}/points/shop/app/${steamId}`,
      },
      {
        key: "discussions",
        labelKey: "discussions",
        url: `steam://openurl/${community}/${steamId}/discussions/`,
      },
      {
        key: "guides",
        labelKey: "guides",
        url: `steam://openurl/${community}/${steamId}/guides/`,
      },
    );
  }
  links.push({
    key: "support",
    labelKey: "support",
    url: getSupportUrl(meta.store, steamId),
  });
  return links;
}

const buttonStyle = {
  padding: "4px 12px",
  fontSize: 12,
  minWidth: 0,
  width: "auto",
  flex: "0 0 auto",
  display: "inline-flex",
  alignItems: "center",
} as const;

export const GameInfoNavButtons: FC<Props> = ({ meta }) => {
  const { t } = useTranslation();
  return (
    // "grid", not "row": these buttons `flex-wrap` onto several visual lines,
    // and a container that claims to be ONE logical row leaves vertical
    // movement between those lines undefined — focus entering the panel
    // jumped straight to the last button and then trapped, with neither up
    // nor down escaping. "grid" resolves geometrically in 2-D (same as
    // GameGrid), so each button is reachable from the one above it.
    //
    // No onFocus/scrollIntoView here either: React focus events bubble, so a
    // handler on the CONTAINER re-centred the whole row whenever any child
    // took focus, fighting Steam's own scroll-to-focused-element (which
    // already works). That was the scroll snapping back to the top.
    <Focusable
      flow-children="grid"
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 8,
        alignItems: "center",
      }}
    >
      {gameLinks(meta).map((link) => (
        <DialogButton
          key={link.key}
          className="unifideck-nav-button"
          style={buttonStyle}
          onClick={() => openUrl(link.url)}
        >
          {t(`gameInfoPanel.buttons.${link.labelKey}`)}
        </DialogButton>
      ))}
    </Focusable>
  );
};
