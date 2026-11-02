/**
 * Native App-Details: Steam renders the game page for our shortcuts, fed
 * with our data and our actions. See `docs/feasibility/native-app-details.md`.
 *
 * This package holds the Steam-facing primitives (finding components by
 * shape, reload-safe patches, the overview proxy). The React pieces and
 * the wiring live in `src/views/native-app-details/`.
 */
export {
  patchActionButton,
  type ActionHostRenderer,
} from "./action-button-patch";
export {
  patchActionRowFrom,
  setActionRowDecorator,
  setPlaySectionDecorator,
  isPlaySectionPatched,
  unpatchActionRow,
  type ActionRowDecorator,
  type ActionRowProps,
} from "./action-row-patch";
export {
  patchTabbedPage,
  appOverviewOfTabs,
  type NativeTab,
  type TabbedPageProps,
  type TabsDecorator,
} from "./tabs-patch";
export {
  withDisplayStatus,
  withOurFeatures,
  StoreCategory,
  type OurFeatureFacts,
  bindActionSpec,
  actionSpecFor,
  isOverviewLive,
  DisplayStatus,
  LIVE_DISPLAY_STATUSES,
  type ActionSpec,
} from "./overview-proxy";
export {
  resolveNativeAction,
  type NativeAction,
  type NativeIntent,
} from "./native-action";
export { getNativeClasses, cx, type NativeClasses } from "./classes";
export { getAppImage, AppAssetType, type AppImageProps } from "./app-image";
export { isUnifideckAppId } from "./gate";
export {
  getAppDetailsSection,
  getAchievementIcon,
  getFeatureRow,
  getFeatureIds,
  getCompatParts,
  getCollectionsParts,
  steamText,
  fetchSteamTopTags,
  type CompatParts,
  type CollectionsParts,
} from "./steam-parts";
export { isNativeAppDetailsActive, setNativeAppDetailsActive } from "./state";
