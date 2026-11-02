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
 * On the game page, a row that can also be streamed gets Steam's "Play
 * from" arrow (see `play-from.tsx`).
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
  CLOUD_TARGET_NAME,
  PLAY_FROM_CLASS,
  cloudStreamUrl,
  remotePlayTipCss,
  showPlayFromMenu,
  streamInstalled,
  usePlayTarget,
} from "./play-from";
import {
  bindActionSpec,
  bindStreamMenu,
  cx,
  withPlayTargets,
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

  // The download bar and the "Play from" arrow belong to the game page's
  // button only; the same button also renders on library surfaces.
  const buttonClass = (element.props as { className?: unknown }).className;
  const pageClass = getNativeClasses().details.AppActionButton;
  const onGamePage =
    !!pageClass &&
    typeof buttonClass === "string" &&
    buttonClass.includes(pageClass);

  const cloudUrl = onGamePage ? cloudStreamUrl(state) : null;
  // A game only xCloud carries: Steam's own Stream button, no arrow. Its
  // click is unchanged (the browser intent below).
  const cloudOnly = state.kind === "browser" && state.variant === "stream";
  const [target, setTarget] = usePlayTarget(appId);
  const cloudSelected = !!cloudUrl && target === "cloud";

  const spec = useRef<ActionSpec>({ onPrimary: () => {} });
  spec.current = {
    onPrimary: () => {
      if (!cloudSelected || !cloudUrl) return runIntent(action.intent, actions);
      if (state.kind === "installed") {
        void streamInstalled(appId, cloudUrl);
      } else {
        // Exactly what the cloud button beside Install did.
        void actions.playBrowser("stream", cloudUrl);
      }
    },
  };
  const menu = useRef<(anchor: EventTarget) => void>(() => {});
  menu.current = (anchor) => showPlayFromMenu(anchor, target, setTarget);

  const proxied = useMemo(() => {
    if (!overview || action.status == null) return null;
    const base = withDisplayStatus(overview, action.status);
    if (cloudOnly) return withPlayTargets(base, CLOUD_TARGET_NAME, true, false);
    return cloudUrl
      ? withPlayTargets(base, CLOUD_TARGET_NAME, cloudSelected)
      : base;
  }, [overview, action.status, cloudUrl, cloudSelected, cloudOnly]);
  if (proxied) {
    bindActionSpec(proxied, spec);
    if (cloudUrl) bindStreamMenu(proxied, menu);
  }

  return (
    <>
      <ProbeForRow />
      {cloudUrl && <style>{remotePlayTipCss()}</style>}
      {proxied
        ? cloneElement(element, {
            overview: proxied,
            ...(cloudUrl
              ? {
                  bShowStreamingSelector: true,
                  className: cx(buttonClass as string, PLAY_FROM_CLASS),
                }
              : {}),
          } as Partial<typeof element.props>)
        : element}
      {onGamePage && <NativeDownloadBar appId={appId} />}
    </>
  );
};

export const NativeActionHost: FC<Props> = (props) => (
  <DownloadProvider>
    <NativeActionBinding {...props} />
  </DownloadProvider>
);
