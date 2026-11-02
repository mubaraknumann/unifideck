/**
 * NativeActionHost: Steam's own action button, driven by our state.
 *
 * Rendered by the action-button patch in place of the native element for
 * our games. It renders that same native element, with:
 *  - a proxied overview whose display status is our state's
 *    (`resolveNativeAction`), so Steam picks the label, icon and tone;
 *  - a click spec bound to the proxy, which the patched `OnClick` runs.
 *
 * While Steam itself is launching or running the game, the proxy steps
 * aside and Steam's own Cancel / Resume + ✕ work as for any game.
 *
 * It also renders a hidden probe once, so the play-bar row above it can be
 * found and patched (see `action-row-patch.ts`).
 */
import {
  cloneElement,
  FC,
  ReactElement,
  useEffect,
  useMemo,
  useRef,
} from "react";
import { DownloadProvider } from "../../contexts/DownloadContext";
import { useGameInfo } from "../../hooks/useGameInfo";
import { usePlayActions, type PlayActions } from "../../hooks/usePlayActions";
import { usePlaySection } from "../../hooks/usePlaySection";
import { NativeDownloadBar } from "./NativeDownloadBar";
import {
  bindActionSpec,
  getNativeClasses,
  patchActionRowFrom,
  resolveNativeAction,
  withDisplayStatus,
  type ActionSpec,
  type NativeIntent,
} from "../../lib/steam-bridge/native-app-details";

interface Props {
  element: ReactElement<{ overview?: object }>;
  appId: number;
}

function realStatusOf(overview: unknown): number | undefined {
  const status = (
    overview as {
      selected_per_client_data?: { display_status?: unknown };
    } | null
  )?.selected_per_client_data?.display_status;
  return typeof status === "number" ? status : undefined;
}

function runIntent(intent: NativeIntent, actions: PlayActions): void {
  switch (intent.kind) {
    case "install":
      void actions.install();
      return;
    case "cancel":
      if (!actions.isWorking) actions.cancelDownload(intent.download);
      return;
    case "update":
      if (!actions.isWorking) void actions.update();
      return;
    case "play":
      actions.play();
      return;
    case "browser":
      void actions.playBrowser(intent.variant, intent.url);
      return;
    default:
      // "none": a click that must not reach Steam (its Install would open
      // Steam's install wizard for a shortcut). "native" never gets here.
      return;
  }
}

const ProbeForRow: FC = () => {
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (ref.current) patchActionRowFrom(ref.current);
  }, []);
  return <span ref={ref} style={{ display: "none" }} />;
};

const NativeActionBinding: FC<Props> = ({ element, appId }) => {
  const state = usePlaySection(appId);
  const info = useGameInfo(appId);
  const actions = usePlayActions(appId);
  const overview = element.props.overview;
  const action = resolveNativeAction(
    state,
    info.status,
    realStatusOf(overview),
    actions.hasUpdate,
  );

  const spec = useRef<ActionSpec>({ onPrimary: () => {} });
  spec.current = { onPrimary: () => runIntent(action.intent, actions) };

  const proxied = useMemo(
    () =>
      overview && action.status != null
        ? withDisplayStatus(overview, action.status)
        : null,
    [overview, action.status],
  );
  if (proxied) bindActionSpec(proxied, spec);
  // The download bar belongs under the game page's button only; the same
  // button also renders on library surfaces, which have no room for it.
  const buttonClass = (element.props as { className?: unknown }).className;
  const pageClass = getNativeClasses().details.AppActionButton;
  const onGamePage =
    !!pageClass &&
    typeof buttonClass === "string" &&
    buttonClass.includes(pageClass);

  return (
    <>
      <ProbeForRow />
      {proxied ? cloneElement(element, { overview: proxied }) : element}
      {onGamePage && <NativeDownloadBar appId={appId} />}
    </>
  );
};

export const NativeActionHost: FC<Props> = (props) => (
  <DownloadProvider>
    <NativeActionBinding {...props} />
  </DownloadProvider>
);
