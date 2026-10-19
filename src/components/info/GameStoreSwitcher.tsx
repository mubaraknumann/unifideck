/**
 * GameStoreSwitcher — detail-page toggle between a duplicate game's
 * cross-store copies.
 *
 * Injected by `AppDetailsPatch` for ANY app-details page (native Steam
 * or an Unifideck shortcut) whose appId is part of a duplicate group —
 * independent of the `appId > 2_000_000_000` gate that scopes the rest
 * of Unifideck's app-details overrides, since a duplicate group can, in
 * principle, include a real Steam entry. Renders nothing when there is
 * no group (the overwhelming majority of games), so it's a safe no-op
 * to mount unconditionally.
 *
 * Each store's copy stays its own real Steam shortcut/appId — switching
 * just navigates to that shortcut's own app-details route, it does not
 * change what's installed or launch anything.
 *
 * A one-line `Dropdown` rather than a row of always-visible buttons —
 * the earlier row rendered flush against the top of the page and,
 * confirmed live on a real device, sat close enough to SteamOS's global
 * search bar (fixed, ~40px tall, measured live) that taps landed on the
 * search bar instead of the switcher.
 *
 * `position: fixed` (not a normal-flow element with a top margin) —
 * overlays the hero art at a fixed screen position instead of pushing
 * the rest of the page's content down by its own height. Confirmed live
 * that this is safe: every ancestor up to `InnerContainer` computes
 * `position: static` with no `transform`/`filter`/`willChange`, so
 * nothing hijacks `fixed`'s containing block into something other than
 * the viewport. `OVERLAY_LEFT_PX` matches `InnerContainer`'s own
 * measured left inset (~34px) so the control lines up with the rest of
 * the page's content instead of floating at a different indent.
 *
 * Each row shows the store's own NAME next to its icon (via `deckTabs.
 * <store>`, the same i18n keys the library tabs use), not just the
 * icon — three copies that all carry the same edition text (e.g. three
 * "Standard Edition" rows for Among Us) used to be indistinguishable
 * except for a 14px glyph.
 *
 * When a store lists several copies of one game whose store name and
 * edition text are still identical, a trailing suffix built from the
 * store's own `disambiguators` (see `lib/variant-labels`) tells them
 * apart. Copies no entry separates keep identical labels.
 */
import { FC, type ReactElement } from "react";
import i18n from "i18next";
import { Dropdown, type SingleDropdownOption } from "@decky/ui";
import { StoreIcon } from "../shared/StoreIcon";
import { navigateToApp } from "../../lib/steam-bridge";
import { appIdsMatch, type GroupSibling } from "../../lib/library-filters";
import { sortByStoreAndRank } from "../../lib/game-grouping";
import {
  disambiguationSuffixes,
  labelCandidates,
} from "../../lib/variant-labels";
import type { StoreId } from "../../types/api";

/** SteamOS's global top bar (search field, notifications, battery, clock,
 *  avatar) is fixed and renders on top of the page — confirmed live via
 *  `getBoundingClientRect()` on a real device at 40px tall, on both the
 *  library and app-details routes. Clearing it is what makes the
 *  dropdown tappable instead of swallowed by the search bar underneath. */
const HEADER_OFFSET_PX = 40;

/** `InnerContainer`'s own measured left inset on a real device — lines
 *  the overlay up with the rest of the page's content. */
const OVERLAY_LEFT_PX = 24;

const t = (key: string): string => i18n.t(key);

/** Store display name, reusing the exact `deckTabs.<store>` keys the
 *  library tabs are titled with (`tab-container.ts`) — one translated
 *  name per store, not a second copy of the same strings. */
function storeName(store: StoreId): string {
  return t(`deckTabs.${store}`);
}

/** Shown in place of an edition name when a sibling has none — the icon
 *  and store name alone don't say what makes this copy distinct from a
 *  sibling with an actual edition tag. i18n-backed (not a hardcoded
 *  English literal) like every other user-facing string in this panel's
 *  siblings (see `GameInfoCompatRow`, `CompatBadge`). */
function defaultEditionLabel(): string {
  return t("gameStoreSwitcher.defaultEdition");
}

interface Props {
  appId: number;
  siblings: GroupSibling[];
}

/** Steam sizes the popup from its items, then centers it in a column limited
 *  to 70% of the screen, so an item wider than that loses the start of its
 *  text. The cap has to be a fixed length: a percentage is ignored while the
 *  popup is being sized. */
const LABEL_MAX_WIDTH_PX = 480;

/** The icon, store name and edition never shrink or wrap. Only the suffix is
 *  cut, with an ellipsis, so every row stays on one line and the most telling
 *  entries stay visible. */
const FIXED_PART_STYLE = { flexShrink: 0, whiteSpace: "nowrap" } as const;

function optionLabel(sibling: GroupSibling, suffix: string): ReactElement {
  return (
    <span
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        minWidth: 0,
        maxWidth: LABEL_MAX_WIDTH_PX,
        overflow: "hidden",
      }}
    >
      <span style={{ ...FIXED_PART_STYLE, display: "flex" }}>
        <StoreIcon store={sibling.store} size={14} />
      </span>
      <span style={FIXED_PART_STYLE}>{storeName(sibling.store)}</span>
      <span style={{ ...FIXED_PART_STYLE, opacity: 0.7 }}>
        {sibling.editionLabel ?? defaultEditionLabel()}
      </span>
      {suffix !== "" && (
        <span
          title={suffix}
          style={{
            opacity: 0.7,
            minWidth: 0,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {suffix}
        </span>
      )}
    </span>
  );
}

export const GameStoreSwitcher: FC<Props> = ({ appId, siblings }) => {
  if (siblings.length < 2) return null;

  // Dropdown matches `selectedOption` against each option's `data` by
  // strict equality — it has no notion of the signed/unsigned appid
  // ambiguity `appIdsMatch` accounts for. Resolve to whichever sibling
  // *is* the current page (by that same appIdsMatch logic) and use its
  // own appId value as the selected option, so the closed dropdown
  // shows the right row even though the incoming `appId` prop may be a
  // different variant of the same number.
  const current = siblings.find((s) => appIdsMatch(s.appId, appId));
  const selectedOption = current?.appId ?? appId;

  const suffixes = disambiguationSuffixes(
    labelCandidates(siblings, defaultEditionLabel()),
  );

  const options: SingleDropdownOption[] = sortByStoreAndRank(siblings).map(
    (sibling) => ({
      data: sibling.appId,
      label: optionLabel(sibling, suffixes.get(sibling.appId) ?? ""),
    }),
  );

  return (
    <div
      style={{
        position: "fixed",
        top: HEADER_OFFSET_PX + 4,
        left: OVERLAY_LEFT_PX,
        zIndex: 5,
        width: "fit-content",
        background: "rgba(0, 0, 0, 0.6)",
      }}
    >
      <Dropdown
        rgOptions={options}
        selectedOption={selectedOption}
        onChange={(opt) => {
          const target = opt.data as number;
          // Replace, not push: switching copies must not stack pages
          // that B then walks back through one by one.
          if (!appIdsMatch(target, appId)) {
            navigateToApp(target, { replace: true });
          }
        }}
      />
    </div>
  );
};
