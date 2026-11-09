/**
 * Our artwork, laid out exactly as Steam lays out a Steam game's.
 *
 * Steam's App-Details header asks its library asset store how to show the
 * art. For a shortcut the answers differ from a Steam game's, and Steam
 * falls back to its shortcut layout (all measured live 2026-11-02):
 *
 *  - `GetHeroImages`: a Steam game reports `bHasHeroImage: true` and at
 *    least two images. A shortcut reports `false` and one custom image, so
 *    the header is marked `FallbackArt`: `24vw` tall (375 px at 1562 px
 *    wide) instead of `calc(80px + 20vw)` (392 px).
 *  - `GetLogoImages`: a Steam game carries its store `logoPosition` (pinned
 *    corner and size, e.g. `CenterCenter` 97% x 81%); a shortcut carries
 *    none, so its logo always sits bottom-left in a 50% x 50% box. We keep
 *    the logo bottom-left and borrow only the size, capped (logo-cap.ts).
 *
 * For our app ids the wrappers answer what Steam would for the matched
 * Steam app: the hero exists, and the logo takes that Steam app's stored
 * position. The position comes from Steam's own app details for the Steam
 * app (`RegisterForAppData` fetches them for any app, owned or not). The
 * images stay ours, and a logo position the user set with Steam's own
 * "Adjust logo position" still wins (Steam checks it first). Content only:
 * nothing about the app's identity changes.
 */
import { getMatchedSteamAppId } from "../app-store-patcher";
import { isUnifideckAppId } from "./gate";
import { capLogoPosition, type LogoPosition } from "./logo-cap";
import { patchSlot } from "./slot-patch";
import { findClassMap, findExportInModule } from "./webpack";

let heroCss: string | null = null;

/**
 * CSS that pins the header's hero art to its top edge. Steam crops the art
 * to the header around its centre. That suits Steam's own wide heroes, but
 * ours are often 16:9 key art, and a centre crop cuts off its top (the
 * subject's head). Mounted only on our game pages. Empty when Steam's
 * header classes are not found.
 */
export function heroTopCss(): string {
  if (heroCss != null) return heroCss;
  const c = findClassMap(["TopCapsule", "ImgSrc", "ImgContainer"]);
  heroCss =
    c.TopCapsule && c.ImgSrc
      ? `.${c.TopCapsule} .${c.ImgSrc}{object-position:center top}`
      : "";
  return heroCss;
}

interface HeroImages {
  rgHeroImages: string[];
  bHasHeroImage: boolean;
  appid?: number;
}

interface LogoImages {
  rgLogoImages: string[];
  logoPosition: LogoPosition | null;
}

type AppRef = { appid?: number };
type GetHeroImages = (overview: AppRef) => HeroImages;
type GetLogoImages = (overview: AppRef) => LogoImages;

interface AssetStore {
  GetHeroImages: GetHeroImages;
  GetLogoImages: GetLogoImages;
  GetLogoImagesForAppId: (
    appId: number,
    cacheVersion: number,
    mtime: number,
  ) => LogoImages;
}

function isAssetStore(v: unknown): v is AssetStore {
  const o = v as Partial<AssetStore> | null;
  return (
    !!o &&
    typeof o === "object" &&
    typeof o.GetHeroImages === "function" &&
    typeof o.GetLogoImages === "function" &&
    typeof o.GetLogoImagesForAppId === "function"
  );
}

/** Steam app ids whose details we already asked Steam for. */
const requested = new Set<number>();

/** Ask Steam once for an app's details (they carry its logo position). */
function requestAppDetails(steamAppId: number): void {
  if (requested.has(steamAppId)) return;
  requested.add(steamAppId);
  const store = (
    window as unknown as {
      appDetailsStore?: {
        RegisterForAppData?: (
          id: number,
          cb: () => void,
        ) => { unregister?: () => void } | undefined;
      };
    }
  ).appDetailsStore;
  try {
    // A holder, not a binding: Steam may call back synchronously, before
    // RegisterForAppData has returned the handle.
    const reg: { handle?: { unregister?: () => void } } = {};
    reg.handle = store?.RegisterForAppData?.(steamAppId, () => {
      reg.handle?.unregister?.();
    });
  } catch (e) {
    console.warn("[Unifideck NativeDetails] Steam app details request:", e);
  }
}

/** The matched Steam app's stored logo position, or null (requesting it
 *  for next time when Steam does not have it yet). */
function steamLogoPosition(
  store: AssetStore,
  appId: number,
): LogoPosition | null {
  const steamAppId = getMatchedSteamAppId(appId);
  if (steamAppId <= 0) return null;
  const position = store.GetLogoImagesForAppId(steamAppId, 0, 0)?.logoPosition;
  if (position) return capLogoPosition(position);
  requestAppDetails(steamAppId);
  return null;
}

/** Install the patches. Returns their disposer, or null when not found. */
export function patchArtwork(): (() => void) | null {
  const store = findExportInModule(
    ["GetHeroImagesForAppId", "GetCustomHeroImageURLs"],
    isAssetStore,
  );
  if (!store) return null;
  const unpatchHero = patchSlot<GetHeroImages>(
    store,
    "GetHeroImages",
    (original) =>
      function getHeroImages(this: unknown, overview) {
        const result = original.call(this, overview);
        try {
          const custom = result?.rgHeroImages ?? [];
          if (
            result.bHasHeroImage ||
            custom.length === 0 ||
            !isUnifideckAppId(overview?.appid)
          ) {
            return result;
          }
          // Listed twice so Steam does not treat a lone image as fallback
          // art; the second entry is the first's own fallback.
          return {
            ...result,
            bHasHeroImage: true,
            rgHeroImages: custom.length === 1 ? [custom[0], custom[0]] : custom,
          };
        } catch {
          return result;
        }
      },
  );
  const unpatchLogo = patchSlot<GetLogoImages>(
    store,
    "GetLogoImages",
    (original) =>
      function getLogoImages(this: unknown, overview) {
        const result = original.call(this, overview);
        try {
          const appId = overview?.appid;
          if (
            result.logoPosition ||
            !result.rgLogoImages?.length ||
            !isUnifideckAppId(appId)
          ) {
            return result;
          }
          const position = steamLogoPosition(store, appId);
          return position ? { ...result, logoPosition: position } : result;
        } catch {
          return result;
        }
      },
  );
  return () => {
    unpatchLogo();
    unpatchHero();
  };
}
