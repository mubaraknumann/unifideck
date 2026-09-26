/**
 * Steam Client API surface — handcrafted ambient declarations.
 *
 * These types describe Steam's CEF-injected globals
 * (`window.SteamClient`, `window.collectionStore`, etc.).
 * They are NOT documented by Valve and may change with any
 * Steam update; the SteamBridge layer is the only consumer
 * so a breakage is contained to one file.
 *
 * When Steam changes a signature, update this file FIRST,
 * then propagate the type error fixes through SteamBridge.
 * Never bypass the bridge to call Steam APIs directly from
 * components — the type error wouldn't surface until runtime.
 */
export interface Unregisterable {
  unregister(): void;
}

/**
 * Lightweight projection of the runtime
 * `window.appStore.allApps` entries we actually
 * read. Steam adds and renames fields between
 * client versions ; only the fields listed here
 * are guaranteed by the SteamBridge facade.
 *
 * @see SteamBridge.getApp() for the safe getter
 */
export interface SteamApp {
  appid: number;
  display_name: string;
  sort_as: string;
  installed: boolean;
  is_shortcuts_app: boolean;
  // Steam types these as numbers on the live AppOverview (verified via
  // CEF debugger; `size_on_disk` is often `undefined` for shortcuts).
  // The native library Sort menu reads them numerically — a string
  // here silently collapses every non-alpha sort to alphabetical.
  size_on_disk?: number;
  minutes_playtime_forever: number;
  minutes_playtime_last_two_weeks: number;
  rt_last_time_played: number;
  rt_last_time_locally_played?: number;
  rt_original_release_date?: number;
  rt_steam_release_date?: number;
  rt_purchased_time?: number;
  metacritic_score?: number;
  review_score_with_bombs?: number;
  review_percentage_with_bombs?: number;
  steam_hw_compat_category_packed?: number;
  steam_deck_compat_category?: number;
  store_category: number[];
  /** MobX set backing the `store_category` getter; mutate this to
   *  populate Steam's native Players filter for spoofed shortcuts. */
  m_setStoreCategories?: Set<number>;
  m_setStoreTags?: Set<number>;
  app_type: number;
  canonicalAppType: number;
  local_per_client_data?: {
    is_hidden?: boolean;
    installed?: boolean;
    display_status?: number;
  };
  BIsShortcut(): boolean;
}

/**
 * Subset of Steam's `AppOverview` we read at runtime.
 * Every field is sourced through SteamBridge so a missing
 * one is logged and replaced by a default rather than
 * throwing on access.
 */
export interface SteamAppOverview extends SteamApp {
  icon_hash: string;
  review_score: number;
  review_percentage: number;
  steam_deck_compat_category?: number;
  visible_in_game_list?: boolean;
  GameID(): string;
  GetCapsuleImageURL(): string;
  GetHeaderImageURL(): string;
  GetLibraryImageURL(): string;
}

/**
 * A Steam library collection ("All Games", "Family
 * Sharing", custom user collections). Used by the
 * tab spoofing layer to inject the Unifideck tab
 * next to native Steam tabs.
 */
export interface SteamCollection {
  id: string;
  name: string;
  added_timestamp: number;
  bIsDynamic: boolean;
  visibleApps: number[];
}

/**
 * Description of a connected controller, as
 * delivered by `SteamClient.System.UI` events.
 * `vid` / `pid` discriminate hardware ; `nativeId`
 * is what we feed back to Steam to bind a config.
 */
export interface ControllerInfo {
  strName: string;
  eControllerType: number;
  nControllerIndex: number;
  bWireless: boolean;
}

/**
 * First message of the controller config stream :
 * Steam sends the list of available templates for
 * the active controller. Followed by a Done message.
 */
export interface ControllerConfigInfoMessageList {
  appID: number;
  nControllerType: number;
  Title: string;
  URL: string;
  eExportType: number;
  bUsesGamepad: boolean;
  bSelected: boolean;
  bOfficial: boolean;
  bUsesMouse: boolean;
  bUsesKeyboard: boolean;
  publishedFileID: string;
}

/**
 * Terminator message of the controller config stream.
 * Receiving it without a matching `List` means the
 * controller has no templates yet.
 */
export interface ControllerConfigInfoMessageDone {
  appID: number;
  bGameQueryDone?: boolean;
  bPersonalQueryDone?: boolean;
  bCloudQueryDone?: boolean;
}

/**
 * Sum type of the two messages Steam emits on the
 * controller config channel. Discriminated by the
 * `nMessageType` field.
 */
export type ControllerConfigInfoMessage =
  | ControllerConfigInfoMessageList
  | ControllerConfigInfoMessageDone;
declare global {
  /** Window. */
  interface Window {
    SteamClient?: {
      Apps?: {
        GetOwnedApps(): SteamApp[];
        GetNonSteamApps(): SteamApp[];
        GetAppOverview(appId: number): SteamAppOverview | null;
        RegisterForGameActionStart(
          callback: (
            gameActionId: number,
            appId: string,
            action: string,
            launchSource: number,
          ) => void,
        ): Unregisterable;
        CancelGameAction(gameActionId: number): void;
        /** Takes the 64-bit gameID, never the appid (getShortcutRunGameId). */
        RunGame(
          gameId: string,
          launchOptions: string,
          a: number,
          b: number,
        ): void;
        /** Takes the 64-bit gameID, never the appid (getShortcutRunGameId). */
        TerminateApp(gameId: string, force: boolean): void;
        ShowControllerConfigurator(appId: number): void;
        OpenAppSettingsDialog(appId: number, section: string): void;
        AddShortcut(
          name: string,
          exe: string,
          startDir: string,
          launchOpts: string,
        ): Promise<number>;
        RemoveShortcut(appId: number): void;
        SpecifyCompatTool(appId: number, tool: string): void;
        SetShortcutLaunchOptions(appId: number, opts: string): void;
        GetPlaytime(appId: number): Promise<{
          nPlaytimeForever: number;
          rtLastTimePlayed: number;
        }>;
        // "Verify integrity of game files" for an app/compat tool — the
        // live client's repair-in-place call. Confirmed on-device: it
        // re-downloads mismatched/missing files without a Steam restart
        // (~8s on an intact tool). Not currently called anywhere — kept
        // typed because it's a proven capability worth having on hand if
        // a genuinely-corrupt-Proton repair flow is built later. See
        // memory: install-hang-orphaned-wineserver-lock.md for why the
        // install-warmup hang this was built for turned out to need a
        // different fix (missing session env, not a corrupt Proton).
        VerifyApp?(appId: number): unknown;
      };
      GameSessions?: {
        RegisterForAppLifetimeNotifications(
          callback: (n: {
            unAppID: number;
            bRunning: boolean;
            nInstanceID: number;
          }) => void,
        ): Unregisterable;
      };
      // Steam Input — controller-config selection. Method names + the
      // ``SetSelectedConfigForApp`` signature were verified against the
      // Steam client UI bundle (steamui/*.js). Used to apply the
      // official "Web Browser" template to the auth-window shortcut.
      Input?: {
        // Streams controller-config messages for EVERY app as arrays of
        // ``List``/``Done`` messages (see ControllerConfigInfoMessage);
        // each carries its ``appID``. Takes the callback only: Steam's own
        // bundle calls it with one argument, and ``(appId, cb)`` is rejected
        // by the backend. Populated after ``QueryControllerConfigsForApp``.
        RegisterForControllerConfigInfoMessages(
          callback: (messages: ControllerConfigInfoMessage[]) => void,
        ): Unregisterable;
        // Triggers Steam to emit the config-info messages for the app.
        QueryControllerConfigsForApp(
          appId: number,
          controllerIndex: number,
          filterOtherControllerTypes: boolean,
        ): void;
        // Selects ``configUrl`` (a template/config ``URL`` from the
        // info messages) as the active config for the app. The 4th arg
        // is a boolean Steam passes as ``false`` at the template-pick
        // call site; the 5th applies it to all controllers of the type.
        SetSelectedConfigForApp(
          appId: number,
          controllerIndex: number,
          configUrl: string,
          unused: boolean,
          applyToAllOfType: boolean,
        ): void;
      };
    };
    collectionStore?: {
      userCollections?: Map<string, SteamCollection>;
    };
    // NOTE: `window.App` (with `m_CurrentUser.strSteamID` — the authoritative
    // live logged-in SteamID64, the only 100%-correct source of the active
    // user) is already declared globally elsewhere; `active-user.ts` reads it
    // via that existing type. Only `loginStore` (a fallback) is added here.
    loginStore?: {
      m_strAccountName?: string;
      m_strSteamID?: string;
    };
  }
}
export {};
