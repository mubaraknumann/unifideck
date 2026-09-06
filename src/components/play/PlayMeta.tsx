/**
 * Layout primitives + shared styles for the Play section
 * variants, matched 1:1 with the staging
 * ``PlayButtonOverride`` reference so the deck UX stays
 * identical across the refactor.
 *
 * Structure (single horizontal row, direct children of
 * ``<Focusable>``):
 *
 *   [ primary action ]   [ meta block ]   [ icon group → ]
 *
 * Meta is inline (label+value pairs side by side), icons
 * float to the right via ``marginLeft: auto``.
 */
import {
  CSSProperties,
  FC,
  ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";
import { DialogButton, Focusable } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useCircuitState } from "../../hooks/useCircuitState";
import { useGameSize } from "../../hooks/useGameSize";
import { useGamePlaytime } from "../../hooks/useGamePlaytime";
import { useGameMetadata } from "../../hooks/useGameMetadata";
import { getThemeableClasses } from "../../lib/steam-bridge";
import { PLAY_FOCUS_CSS } from "./play.css";
import { useStoreCapability } from "../../hooks/useStoreCapability";

/**
 * Class-name builders that make our buttons visible to CSS Loader
 * themes. See `getThemeableClasses()` for how theme selectors reach
 * them; the short version is that a theme can only style what wears
 * Steam's own class, so we wear it and stop hardcoding the properties
 * themes want to change (radius, idle fill).
 *
 * Called during render rather than memoised at module scope: the
 * `@decky/ui` class exports are only populated once Steam's webpack
 * chunks have loaded, which is not guaranteed at plugin import time.
 */

/** Square icon button (cloud / controller / gear / trash / stop). */
export function iconBtnClass(
  marker = "unifideck-icon-btn",
  ...extra: string[]
): string {
  return [marker, getThemeableClasses().menuButton, ...extra]
    .filter(Boolean)
    .join(" ");
}

/** Controller-config button — Steam gives this one its own variant. */
export function controllerBtnClass(): string {
  return iconBtnClass(
    "unifideck-icon-btn",
    getThemeableClasses().controllerConfigButton,
  );
}

/** Primary action button. Both Steam names must land together — the
 *  theme rule is a compound selector, not a descendant one. */
export function actionBtnClass(marker: string): string {
  const c = getThemeableClasses();
  return [marker, c.actionButton, c.playButtonContainer]
    .filter(Boolean)
    .join(" ");
}

/** Inline style shared by every primary action button
 *  (Install / Play / Resume / Update / Cancel).
 *
 *  Deliberately sets no `borderRadius`: Steam's own
 *  `AppActionButton`+`PlayButtonContainer` classes supply it
 *  (2px vanilla), and an inline value would beat any CSS Loader
 *  theme rule. See `iconBtnStyle` below. */
export const actionBtnStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "flex-start",
  gap: 8,
  flex: "0 0 auto",
  width: "auto",
  minWidth: 200,
  height: 48,
  padding: "0 24px",
  color: "#fff",
  fontSize: 16,
  fontWeight: 500,
  border: "none",
  textTransform: "uppercase",
  letterSpacing: "0.05em",
};

/** Inline style for square icon buttons (cloud / controller /
 *  settings / trash / X).
 *
 *  Sets NO `borderRadius` and NO `background` on purpose. Steam's
 *  `MenuButton` class already supplies both (2px radius,
 *  rgba(172,178,201,0.14) fill — verified identical to what we used
 *  to hardcode), and an inline value here would outrank any CSS
 *  Loader theme rule, which is exactly what used to make our row
 *  stay square while a theme rounded Steam's own buttons.
 *
 *  Corollary for anyone measuring vanilla values to hardcode: do it
 *  with CSS Loader DISABLED. An enabled theme rounds these to ~10px,
 *  which reads convincingly native. Better still, don't hardcode —
 *  let the class supply it, as here.
 *
 *  Width/height stay pinned so the row cannot collapse if Steam
 *  renames `MenuButton` and the class resolves to "". */
export const iconBtnStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  width: 48,
  height: 48,
  minWidth: 48,
};

/**
 * Outer Focusable shell for every play variant. Styling matches
 * staging: full-width flex row with the translucent dark background
 * and 16 px inner padding.
 *
 * `autoFocus` is a real Steam `Focusable` nav option (consumed into
 * navOptions, NOT the React DOM attribute) — when true, gamepad focus
 * lands on the shell's first focusable child (the primary action
 * button) as the nav node mounts. The variants pass it so the page
 * loads with our Install / Play / Resume / Cancel button focused
 * instead of Steam's (now hidden) native Play button. The loading
 * skeleton omits it.
 */
export const PlayShell: FC<{ children: ReactNode; autoFocus?: boolean }> = ({
  children,
  autoFocus,
}) => {
  const ref = useRef<HTMLDivElement>(null);
  // The `autoFocus` navOption only wins if the Focusable exists during
  // Steam's initial app-page focus pass — but our variant mounts *late*
  // (after `get_game_info` resolves, past the loading skeleton), so it
  // often misses. Imperatively focus the primary button on mount, with
  // one delayed retry to beat that pass. Bails once focus has landed so
  // it never steals focus the user moved away.
  useEffect(() => {
    if (!autoFocus) return;
    let raf = 0;
    let timer = 0;
    let retried = false;
    const grab = (): void => {
      const root = ref.current;
      if (!root) return;
      const target = root.querySelector<HTMLElement>("button") ?? root;
      if (document.activeElement === target) return;
      target.focus?.();
      if (document.activeElement !== target && !retried) {
        retried = true;
        timer = window.setTimeout(grab, 140);
      }
    };
    raf = requestAnimationFrame(grab);
    return () => {
      cancelAnimationFrame(raf);
      window.clearTimeout(timer);
    };
  }, [autoFocus]);

  return (
    <Focusable
      ref={ref}
      flow-children="row"
      // autoFocus is intentional: claims gamepad focus for the primary action
      // eslint-disable-next-line jsx-a11y/no-autofocus
      autoFocus={autoFocus}
      style={{
        display: "flex",
        alignItems: "center",
        width: "100%",
        padding: "16px",
        boxSizing: "border-box",
        background: "rgba(14, 20, 27, 0.33)",
        position: "relative",
        zIndex: 2,
      }}
    >
      {/* Render the focus CSS inline so it lands in THIS (App-Details)
          CEF document — a document.head injection from elsewhere does
          not reach it. This is what makes Install→blue / Play→green /
          Cancel→red actually apply on focus (staging's pattern). */}
      <style>{PLAY_FOCUS_CSS}</style>
      {children}
    </Focusable>
  );
};

/**
 * Placeholder shown in the Play section while `get_game_info` is still
 * resolving for a shortcut. The native Play row is already hidden by
 * CSS at that point, so this keeps the area from collapsing (no layout
 * jump) until the real variant renders. A muted button-sized block —
 * no text, so it needs no i18n.
 */
export const PlayLoadingSkeleton: FC = () => (
  <PlayShell>
    <div
      style={{
        minWidth: 200,
        height: 48,
        borderRadius: 4,
        background: "rgba(255, 255, 255, 0.06)",
      }}
    />
  </PlayShell>
);

/** Right-floated icon group. ``marginLeft: auto`` pushes it
 *  to the far right of the row.
 *
 *  Wears Steam's ``AppButtons`` class because the theme rule for the
 *  icon buttons is `.AppButtons .MenuButton` — without this ancestor
 *  the buttons' own class is not enough. Steam's own AppButtons is
 *  just `display:flex; row; align-items:center`, so it does not fight
 *  the layout below. */
export const IconGroup: FC<{ children: ReactNode }> = ({ children }) => (
  <div
    className={getThemeableClasses().appButtons}
    style={{
      display: "flex",
      gap: 8,
      marginInlineStart: "auto",
      flex: "0 0 auto",
    }}
  >
    {children}
  </div>
);

/** Format bytes as a human-readable size string. */
export function formatBytes(bytes: number | undefined | null): string {
  if (!bytes || bytes <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = bytes;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

/** Format a Date as a short localized date string. */
function formatDate(d: Date): string {
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/** Format a Unix timestamp (seconds) as a date string. */
export function formatLastPlayed(
  rtLastTimePlayed: number | undefined | null,
): string {
  if (!rtLastTimePlayed || rtLastTimePlayed <= 0) return "—";
  return formatDate(new Date(rtLastTimePlayed * 1000));
}

/** Format an ISO-8601 timestamp (our DB's ``last_played_at``) as a
 *  date string, or ``null`` when absent/unparseable so callers can
 *  fall back to Steam's value. */
export function formatLastPlayedISO(
  iso: string | null | undefined,
): string | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return null;
  return formatDate(new Date(ms));
}

/** Format a playtime duration (seconds) as a compact "12.5 h" / "45 min"
 *  string. Returns "—" for zero/unknown. Units are i18n-supplied so the
 *  abbreviation can be localized. */
export function formatPlaytime(
  secs: number | undefined | null,
  hoursShort: string,
  minutesShort: string,
): string {
  if (!secs || secs <= 0) return "—";
  if (secs < 3600)
    return `${Math.max(1, Math.round(secs / 60))} ${minutesShort}`;
  const hours = secs / 3600;
  return `${hours >= 100 ? Math.round(hours) : hours.toFixed(1)} ${hoursShort}`;
}

/** Stores with cloud-save sync — mirrors `useCloudSaveStatus`. */

/**
 * `cloud_saves` tri-state → i18n key, indexed by `String(value)`.
 *
 * "Unknown" is shown rather than hidden: for a store that HAS cloud-save
 * sync, "we don't know" is itself the honest answer, and silently omitting
 * the column would read as "no cloud saves".
 */
const CLOUD_SAVE_VALUE_KEY: Record<string, string> = {
  true: "playMeta.cloudSupported",
  false: "playMeta.cloudNotSupported",
  null: "playMeta.cloudUnknown",
  undefined: "playMeta.cloudUnknown",
};

/** One label/value column inside {@link MetaInline}. */
const MetaItem: FC<{ label: string; value: string }> = ({ label, value }) => (
  <div>
    <div
      style={{
        fontSize: 11,
        fontWeight: 600,
        textTransform: "uppercase",
        color: "#8f98a0",
        letterSpacing: "0.08em",
        lineHeight: 1,
        marginBottom: 5,
      }}
    >
      {label}
    </div>
    <div style={{ fontSize: 14, color: "#dcdedf" }}>{value}</div>
  </div>
);

interface MetaInlineProps {
  /** Total install size in bytes (Space Required column). */
  sizeBytes?: number;
  /** Whether to render the Played / Last Played columns (installed games only). */
  showLastPlayed?: boolean;
  /** Steam appId — used to look up Steam's rtLastTimePlayed (fallback). */
  appId?: number | null;
  /** Store id — together with {@link gameId}, enables the "Played" total
   *  and an accurate "Last Played" sourced from our own tracking DB. */
  store?: string | null;
  /** Store-native game id (``Game.id`` / ``store_game_id``). */
  gameId?: string | null;
  /** True for installed games. Switches the size label to
   *  "Installed Size" and makes {@link useGameSize} report the
   *  on-disk size instead of the (stale) pre-install download size. */
  installed?: boolean;
}

/**
 * Inline metadata block between the primary action button
 * and the icon group. Side-by-side "Space Required" /
 * "Last Played" pairs, with ``marginLeft: 20`` to space it
 * off the action button (matches staging spacing).
 */
export const MetaInline: FC<MetaInlineProps> = ({
  sizeBytes,
  showLastPlayed = false,
  appId,
  store,
  gameId,
  installed = false,
}) => {
  const { t } = useTranslation();
  const [steamLastPlayed, setSteamLastPlayed] = useState<number | null>(null);
  // Cloud-save availability sits with the other at-a-glance facts rather than
  // in the info panel below: it is a *decision* input (which storefront's copy
  // to install), so it belongs next to Space Required / Last Played.
  //
  // Free to read here — `useGameMetadata` is appId-keyed with a 60 s TTL and
  // in-flight de-duplication, and GameInfoPanel already requests the same
  // appId, so both consumers share one fetch. It resolves to null first and
  // never blocks this row.
  const metadata = useGameMetadata(appId ?? null);
  // Only for a NOT-installed game. Once it is installed the cloud-save icon
  // button sits right there in the same row and reports the live state
  // (syncing / in sync / no support / unresolved), so a static
  // "Supported/Unknown" beside it is redundant at best and contradictory at
  // worst — an enabled button next to the word "Unknown" reads like a bug.
  // Second copy of the same set, removed — its own comment said it mirrored
  // useCloudSaveStatus. Both now read the backend-derived capability.
  const supportsCloudSaves = useStoreCapability(store, "supports_cloud_saves");
  const showCloudSaves = !installed && !!store && supportsCloudSaves;
  // Circuit-breaker state. Rendered only while the breaker is actually open,
  // so an ordinary game never carries a diagnostic badge — but when it IS
  // open the user previously got nothing at all: no message, no badge, and
  // no way to reset short of waiting out the window (audit item 4a).
  const circuit = useCircuitState(store, gameId);
  // Size is fetched out-of-band (see useGameSize) so a slow store
  // lookup never blocks this row from rendering. Keyed on `installed`
  // so the on-disk size replaces the pre-install download size once
  // the game finishes installing. Prefer the fetched value; fall back
  // to any size the caller already had.
  const fetchedSize = useGameSize(appId ?? null, installed);
  const resolvedSize = fetchedSize && fetchedSize > 0 ? fetchedSize : sizeBytes;

  // Our own tracking DB (set when the caller knows the store/game pair).
  // Preferred source for both the total and last-played; Steam's value
  // is only used as a fallback when we have no local record.
  const playtime = useGamePlaytime(
    showLastPlayed ? store : null,
    showLastPlayed ? gameId : null,
  );

  useEffect(() => {
    if (!showLastPlayed || appId == null) return;
    let cancelled = false;
    const apps = (
      window as unknown as {
        SteamClient?: {
          Apps?: {
            GetPlaytime?: (
              id: number,
            ) => Promise<{ rtLastTimePlayed?: number }>;
          };
        };
      }
    ).SteamClient?.Apps;
    if (!apps?.GetPlaytime) return;
    apps
      .GetPlaytime(appId)
      .then((res) => {
        if (cancelled) return;
        setSteamLastPlayed(res?.rtLastTimePlayed ?? null);
      })
      .catch(() => {
        /* ignore */
      });
    return () => {
      cancelled = true;
    };
  }, [appId, showLastPlayed]);

  // Total played: store's cross-device total (GOG/Epic) when present,
  // else our local total. Only rendered once we have a tracked record.
  const totalSecs =
    playtime != null
      ? playtime.store_total_secs ?? playtime.total_seconds
      : undefined;
  const showPlayed = showLastPlayed && totalSecs != null && totalSecs > 0;

  // Last played: prefer our DB's timestamp; fall back to Steam's only
  // when we have no local record (e.g. a Steam-native game we don't track).
  const lastPlayedValue =
    formatLastPlayedISO(playtime?.last_played) ??
    (steamLastPlayed ? formatLastPlayed(steamLastPlayed) : null);

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 32,
        marginInlineStart: 20,
        flex: "0 1 auto",
      }}
    >
      <MetaItem
        label={
          installed ? t("playMeta.installedSize") : t("playMeta.spaceRequired")
        }
        value={formatBytes(resolvedSize)}
      />
      {showPlayed && (
        <MetaItem
          label={t("playMeta.played")}
          value={formatPlaytime(
            totalSecs,
            t("playMeta.hoursShort"),
            t("playMeta.minutesShort"),
          )}
        />
      )}
      {showLastPlayed && (
        <MetaItem
          label={t("playMeta.lastPlayed")}
          value={lastPlayedValue ?? t("playMeta.neverPlayed")}
        />
      )}
      {showCloudSaves && (
        <MetaItem
          label={t("playMeta.cloudSaves")}
          value={t(CLOUD_SAVE_VALUE_KEY[String(metadata.data?.cloud_saves)])}
        />
      )}
      {circuit.open && (
        <CircuitBadge
          failureCount={circuit.failureCount}
          onReset={() => void circuit.reset()}
          onForceLaunch={() => void circuit.forceLaunch()}
        />
      )}
    </div>
  );
};

/**
 * "N recent launch failures" + the two ways out.
 *
 * Every string here already existed, translated, in all 16 locales
 * (`library.circuitBreaker.*`) with nothing rendering them — the exact shape
 * audit §1.1.2 describes, where the material shipped and only the delivery
 * was missing. Zero new strings.
 *
 * Shown only when the breaker is open. A permanent failure counter would be
 * noise on a healthy game and would make the badge easy to ignore on an
 * unhealthy one.
 */
const CircuitBadge: FC<{
  failureCount: number;
  onReset: () => void;
  onForceLaunch: () => void;
}> = ({ failureCount, onReset, onForceLaunch }) => {
  const { t } = useTranslation();
  return (
    <Focusable
      style={{ display: "flex", alignItems: "center", gap: 8 }}
      title={t("library.circuitBreaker.badgeTooltip", { count: failureCount })}
    >
      <MetaItem
        label={t("library.circuitBreaker.badgeRecent", { count: failureCount })}
        value=""
      />
      <DialogButton
        style={{ minWidth: 0, padding: "4px 10px" }}
        onClick={onForceLaunch}
      >
        {t("library.circuitBreaker.forceLaunchButton")}
      </DialogButton>
      <DialogButton
        style={{ minWidth: 0, padding: "4px 10px" }}
        onClick={onReset}
      >
        {t("library.circuitBreaker.resetButton")}
      </DialogButton>
    </Focusable>
  );
};
